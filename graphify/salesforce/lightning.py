from __future__ import annotations

import re
from html.parser import HTMLParser

from .apex import walk
from .metadata import merge_refs
from .model import Facts


def parse_javascript(facts: Facts) -> None:
    from tree_sitter_language_pack import get_parser
    code = facts.source.content.encode()
    parser = get_parser("typescript" if facts.source.path.endswith(".ts") else "javascript")
    root = parser.parse(code).root_node
    facts.level = "semantic"

    def text(n):
        return code[n.start_byte:n.end_byte].decode() if n else ""

    if root.has_error:
        facts.level = "partial"
        facts.issue("syntax_error", language="javascript")
    functions = []
    is_aura = facts.source.metadata_type == "AuraDefinitionBundle"
    method_kind = "AuraClientMethod" if is_aura else "LightningMethod"
    helper = is_aura and facts.source.path.endswith("Helper.js")
    method_prefix = facts.source.full_name + (".helper" if helper else "")
    for n in walk(root):
        name_node = None
        if n.type in {"method_definition", "function_declaration"}:
            name_node = n.child_by_field_name("name")
        elif n.type == "pair" and n.child_by_field_name("value") and n.child_by_field_name("value").type in {"function_expression", "arrow_function"}:
            name_node = n.child_by_field_name("key")
        if name_node:
            name = method_prefix + "." + text(name_node).strip("'\"")
            nid = facts.declare(method_kind, name, n.start_point.row + 1)
            facts.ref(facts.source.component_id, method_kind, name, "method", n.start_point.row + 1)
            functions.append((n.start_byte, n.end_byte, nid))
    imports = {}
    for n in walk(root):
        line = n.start_point.row + 1
        candidates = [(end - start, nid) for start, end, nid in functions
                      if start <= n.start_byte and n.end_byte <= end]
        owner = min(candidates)[1] if candidates else facts.source.component_id
        if n.type in {"import_statement", "export_statement"}:
            source = n.child_by_field_name("source")
            value = text(source).strip("'\"")
            mapping = {"apex": "ApexMethod", "schema": "FieldPath", "label": "CustomLabel",
                       "resourceUrl": "StaticResource", "contentAssetUrl": "ContentAsset",
                       "messageChannel": "LightningMessageChannel", "customPermission": "CustomPermission"}
            if value.startswith("@salesforce/"):
                module = value.removeprefix("@salesforce/")
                category, _, target = module.partition("/")
                typ = mapping.get(category)
                if target and typ:
                    if category == "schema" and "." not in target:
                        typ = "CustomObject"
                    if category == "label" and target.startswith("c."):
                        target = target[2:]
                    facts.ref(facts.source.component_id, typ, target, "imports", line)
                    clause = next((c for c in n.named_children if c.type == "import_clause"), None)
                    if clause:
                        binding = next((c for c in clause.named_children if c.type == "identifier"), None)
                        if binding:
                            imports[text(binding)] = (typ, target)
            elif value.startswith("c/"):
                facts.ref(facts.source.component_id, "LightningComponentBundle", value[2:], "imports", line)
        # Aura's component.get('c.serverMethod') resolves via its bundle's
        # controller attribute; preserve the dependency until cross-file binding.
        if n.type == "call_expression":
            function = text(n.child_by_field_name("function"))
            args = n.child_by_field_name("arguments")
            values = list(args.named_children) if args else []
            if function.endswith(".get") and values and values[0].type == "string":
                value = text(values[0])[1:-1]
                if value.startswith("c.") and is_aura:
                    facts.ref(owner, "AuraAction", value[2:], "calls", line)
            if function in imports:
                typ, name = imports[function]
                facts.ref(owner, typ, name, "calls", line)
            elif function.startswith("this."):
                facts.ref(owner, method_kind, method_prefix + "." + function[5:], "calls", line)
            elif is_aura and function.startswith("helper."):
                facts.ref(owner, method_kind, facts.source.full_name + "." + function, "calls", line)


def parse_markup(facts: Facts) -> None:
    facts.level = "semantic"
    owner = facts.source.component_id

    class MarkupParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.object_name = ""
            self.variables = {}
            self.scopes = []

        def handle_starttag(self, tag, attrs):
            line = self.getpos()[0]
            attributes = dict(attrs)
            self.scopes.append((tag, dict(self.variables), self.object_name))
            if tag == "messaging:emailtemplate":
                for attribute, alias, property_name in (
                    ("relatedtotype", "relatedTo", "related_object"),
                    ("recipienttype", "recipient", "recipient_object"),
                ):
                    obj = attributes.get(attribute)
                    if obj:
                        self.variables[alias] = obj
                        facts.nodes[owner][property_name] = obj
                        facts.ref(owner, "CustomObject", obj, "references_object", line)
            if tag in {"apex:repeat", "apex:datatable", "apex:pageblocktable"}:
                value = re.fullmatch(r"\{!\s*([\w.]+)\s*\}", attributes.get("value") or "")
                variable = attributes.get("var")
                if value and variable:
                    root, sep, path = value[1].partition(".")
                    base = next((v for k, v in self.variables.items() if k.casefold() == root.casefold()), root)
                    self.variables[variable] = base + (sep + path if sep else "")
            controller = attributes.get("controller")
            if controller:
                facts.ref(owner, "ApexClass", controller, "controller", line)
            for extension in (attributes.get("extensions") or "").split(","):
                if extension.strip():
                    facts.ref(owner, "ApexClass", extension.strip(), "controller", line)
            standard = attributes.get("standardcontroller")
            if standard:
                facts.ref(owner, "CustomObject", standard, "controller", line)
                self.object_name = standard
            if tag.startswith("c-"):
                name = re.sub(r"-([a-z])", lambda m: m[1].upper(), tag[2:])
                facts.ref(owner, "LightningComponentBundle", name, "renders", line)
            elif tag.startswith("c:"):
                typ = "AuraDefinitionBundle" if facts.source.metadata_type == "AuraDefinitionBundle" else "ApexComponent"
                facts.ref(owner, typ, tag[2:], "renders", line)
            for key, value in attrs:
                if not value:
                    continue
                for expr in re.findall(r"\{[!#]([^}]+)\}", value):
                    if expr.startswith("$Resource."):
                        facts.ref(owner, "StaticResource", expr.split(".")[1], "references", line)
                    elif expr.startswith("c.") and facts.source.metadata_type == "AuraDefinitionBundle":
                        # Markup c.foo references a JS controller method, not
                        # necessarily the Apex action of the same name.
                        facts.ref(owner, "AuraClientMethod", facts.source.full_name + "." + expr[2:], "handles", line)
                merge_refs(facts, owner, value, self.object_name, line, self.variables)

        def handle_endtag(self, tag):
            for i in range(len(self.scopes) - 1, -1, -1):
                if self.scopes[i][0] == tag:
                    _, self.variables, self.object_name = self.scopes[i]
                    del self.scopes[i:]
                    break

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

        def handle_data(self, data):
            merge_refs(facts, owner, data, self.object_name, self.getpos()[0], self.variables)

    try:
        MarkupParser().feed(facts.source.content)
    except (ValueError, AssertionError):
        facts.level = "partial"
        facts.issue("markup_parse_error")
