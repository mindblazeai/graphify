"""Apex syntax facts from the sfapex Tree-sitter grammar (including SOQL/SOSL)."""
from __future__ import annotations

import re

from .model import Facts

BUILTINS = {x.casefold() for x in (
    "void Object String Id Boolean Integer Long Double Decimal Date Datetime Time Blob "
    "List Set Map SObject System Database Schema Trigger Test Math Type JSON Http "
    "HttpRequest HttpResponse PageReference Exception Savepoint Iterator Iterable "
    "Queueable QueueableContext Schedulable SchedulableContext Batchable BatchableContext"
    " AggregateResult QueryLocator SaveResult DeleteResult UpsertResult RecordTypeInfo "
    "RestRequest RestResponse RestContext ApexPages Messaging LoggingLevel Comparable"
).split()}
DECLARATIONS = {"class_declaration": "ApexClass", "interface_declaration": "ApexInterface",
                "enum_declaration": "ApexEnum", "trigger_declaration": "ApexTrigger"}


def walk(node):
    yield node
    for child in node.named_children:
        yield from walk(child)


def parse_apex(facts: Facts) -> None:
    from tree_sitter_language_pack import get_parser

    src = facts.source
    standalone = src.metadata_type in {"SOQL", "SOSL"}
    prefix = "class Query { void run() { Object result = [\n" if standalone else ""
    suffix = "\n]; } }" if standalone else ""
    code = (prefix + src.content + suffix).encode()
    tree = get_parser("apex").parse(code)
    facts.level = "semantic"

    def text(n):
        return code[n.start_byte:n.end_byte].decode() if n else ""

    def field(n, name):
        return n.child_by_field_name(name)

    def line(n):
        return max(1, n.start_point.row + 1 - prefix.count("\n"))

    if tree.root_node.has_error:
        facts.level = "partial"
        for n in walk(tree.root_node):
            if n.type == "ERROR" or n.is_missing:
                facts.issue("syntax_error", line(n), language="apex", end_line=line(n) +
                            n.end_point.row - n.start_point.row)

    def type_name(n):
        value = text(n).strip()
        if "<" in value:
            # A DML receiver's List<Account> refers to Account. Preserve the
            # full spelling separately on method signatures.
            value = value[value.index("<") + 1:value.rfind(">")].split(",")[-1].strip()
        return value.removesuffix("[]")

    def type_refs(n, owner):
        if not n:
            return
        for t in walk(n):
            if t.type == "type_identifier" and text(t).casefold() not in BUILTINS:
                facts.ref(owner, "Type", text(t), "references_type", line(t))

    def receiver_type(n, variables, class_name, trigger_object):
        if not n:
            return class_name
        value = text(n)
        if value in {"this", "super"}:
            return class_name if value == "this" else variables.get("__super", "")
        if value.casefold() in {"trigger.new", "trigger.old", "trigger.newmap", "trigger.oldmap"}:
            return trigger_object
        if n.type == "array_access":
            return receiver_type(field(n, "array"), variables, class_name, trigger_object)
        if n.type == "object_creation_expression":
            return type_name(field(n, "type"))
        if n.type == "field_access":
            receiver = receiver_type(field(n, "object"), variables, class_name, trigger_object)
            return receiver + "." + text(field(n, "field")) if receiver else ""
        return variables.get(value.casefold(), value)

    def query(n, owner, parent_object=""):
        if n.type == "sosl_query_body":
            for child in walk(n):
                if child.type == "sobject_return":
                    object_node = next((x for x in child.named_children if x.type == "identifier"), None)
                    obj = text(object_node)
                    if obj:
                        facts.ref(owner, "CustomObject", obj, "queries", line(child))
                        for f in walk(child):
                            if f.type == "field_identifier":
                                facts.ref(owner, "FieldPath", obj + "." + text(f), "reads", line(f))
            return
        from_clause = field(n, "from_clause")
        storage = next((x for x in walk(from_clause) if x.type == "storage_identifier"), None) if from_clause else None
        obj = text(storage)
        if not obj:
            facts.issue("unresolved_query_object", line(n))
            return
        if parent_object:
            # FROM Contacts inside Account is a child relationship, not a
            # CustomObject named Contacts. Resolve against field metadata later.
            facts.ref(owner, "ChildRelationship", parent_object + "." + obj, "queries", line(storage))
            obj = parent_object + "." + obj
        else:
            facts.ref(owner, "CustomObject", obj, "queries", line(storage))
        aliases = [text(x) for x in walk(from_clause) if x.type == "storage_alias"] if from_clause else []

        def visit(q):
            if q != n and q.type == "soql_query_body":
                query(q, owner, obj)
                return
            if q.type == "bound_apex_expression":
                return
            if q.type == "field_identifier":
                name = text(q)
                if aliases and name.startswith(aliases[0] + "."):
                    name = name[len(aliases[0]) + 1:]
                facts.ref(owner, "FieldPath", obj + "." + name, "reads", line(q))
                return
            for child in q.named_children:
                visit(child)
        visit(n)

    def constant_string(n, constants, depth=0):
        if not n or depth > 20:
            return None
        if n.type == "string_literal":
            value = text(n)[1:-1]
            escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "\\": "\\", "'": "'", '"': '"'}
            return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", lambda m: chr(int(m[1][1:], 16)) if m[1].startswith("u") else escapes.get(m[1], m[0]), value)
        if n.type == "identifier":
            return constants.get(text(n).casefold())
        if n.type == "parenthesized_expression" and len(n.named_children) == 1:
            return constant_string(n.named_children[0], constants, depth + 1)
        if n.type == "binary_expression":
            left, right = field(n, "left"), field(n, "right")
            if left and right and code[left.end_byte:right.start_byte].strip() == b"+":
                a, b = constant_string(left, constants, depth + 1), constant_string(right, constants, depth + 1)
                if a is not None and b is not None and len(a) + len(b) <= 65536:
                    return a + b
        return None

    def assigned_names(n):
        return {text(field(child, "left") if child.type == "assignment_expression" else field(child, "name")).casefold()
                for child in walk(n) if child.type in {"assignment_expression", "variable_declarator"}}

    def visit(n, owner, class_name, variables, trigger_object="", constants=None):
        if constants is None:
            constants = {}
        kind = n.type
        if kind in DECLARATIONS and not standalone:
            name = text(field(n, "name"))
            full = (class_name + "." + name) if class_name else name
            # An org snapshot carries the package namespace in the component
            # identity even when Apex declares a bare class name.
            if not class_name and src.namespace and not full.startswith(src.namespace + "."):
                full = src.namespace + "." + full
            modifiers = next((c for c in n.named_children if c.type == "modifiers"), None)
            annotations = [text(field(c, "name")) for c in walk(modifiers) if c.type == "annotation"] if modifiers else []
            nid = facts.declare(DECLARATIONS[kind], full, line(n), label=name,
                                annotations=annotations,
                                is_test=any(a.casefold() == "istest" for a in annotations))
            if nid != owner:
                facts.ref(owner, DECLARATIONS[kind], full, "contains", line(n))
                if not class_name and src.metadata_type == "ApexClass":
                    facts.nodes[owner]["container_only"] = True
            inherited = dict(variables)
            for clause_name, relation in (("superclass", "extends"), ("interfaces", "implements")):
                clause = field(n, clause_name)
                if clause is None and clause_name == "superclass":
                    clause = next((c for c in n.named_children if c.type == "extends_interfaces"), None)
                if clause:
                    for t in walk(clause):
                        if t.type == "type_identifier":
                            if text(t).casefold() not in BUILTINS:
                                facts.ref(nid, "Type", text(t), relation, line(t))
                            if relation == "extends":
                                inherited["__super"] = text(t)
            obj = text(field(n, "object")) or trigger_object
            if kind == "trigger_declaration":
                events = [text(x) for x in n.named_children if x.type == "trigger_event"]
                facts.ref(nid, "CustomObject", obj, "triggers_on", line(n), events=events)
            body = field(n, "body")
            if body:
                class_constants = {}
                # Class fields may be declared below methods that use them.
                for child in body.named_children:
                    if child.type == "field_declaration":
                        typ = type_name(field(child, "type"))
                        for decl in child.named_children:
                            if decl.type == "variable_declarator":
                                inherited[text(field(decl, "name")).casefold()] = typ
                                if "final" in text(next((c for c in child.named_children if c.type == "modifiers"), None)).casefold().split():
                                    value = constant_string(field(decl, "value"), class_constants)
                                    if value is not None:
                                        class_constants[text(field(decl, "name")).casefold()] = value
                visit(body, nid, full, inherited, obj, class_constants)
            return
        if kind in {"method_declaration", "constructor_declaration"} and not standalone:
            name = text(field(n, "name"))
            params_node = field(n, "parameters")
            params = [x for x in params_node.named_children if x.type == "formal_parameter"] if params_node else []
            param_types = [text(field(x, "type")) for x in params]
            full = f"{class_name}.{name}({','.join(param_types)})"
            modifiers = next((c for c in n.named_children if c.type == "modifiers"), None)
            is_test = facts.nodes.get(owner, {}).get("is_test", False) or bool(
                modifiers and ("testmethod" in text(modifiers).casefold() or
                               any(c.type == "annotation" and text(field(c, "name")).casefold() == "istest" for c in walk(modifiers))))
            nid = facts.declare("ApexMethod", full, line(n), label=f".{name}()", is_test=is_test,
                                owner_type=class_name, member_name=name,
                                parameters=param_types, return_type=text(field(n, "type")))
            facts.ref(owner, "ApexMethod", full, "method", line(n))
            scoped = dict(variables)
            scoped_constants = dict(constants)
            for p in params:
                scoped[text(field(p, "name")).casefold()] = type_name(field(p, "type"))
                type_refs(field(p, "type"), nid)
                scoped_constants.pop(text(field(p, "name")).casefold(), None)
            type_refs(field(n, "type"), nid)
            for child in n.named_children:
                visit(child, nid, class_name, scoped, trigger_object, scoped_constants)
            return
        if kind in {"local_variable_declaration", "field_declaration", "formal_parameter", "enhanced_for_statement"}:
            typ = type_name(field(n, "type"))
            type_refs(field(n, "type"), owner)
            name_node = field(n, "name")
            if name_node:
                variables[text(name_node).casefold()] = typ
            for child in n.named_children:
                if child.type == "variable_declarator":
                    variables[text(field(child, "name")).casefold()] = typ
                    value = constant_string(field(child, "value"), constants)
                    if kind == "field_declaration" and "final" not in text(next((c for c in n.named_children if c.type == "modifiers"), None)).casefold().split():
                        value = None  # Mutable fields can change between calls.
                    key = text(field(child, "name")).casefold()
                    if value is None:
                        constants.pop(key, None)
                    else:
                        constants[key] = value
        if kind == "assignment_expression" and field(n, "left") and field(n, "left").type == "identifier":
            key = text(field(n, "left")).casefold()
            value = constant_string(field(n, "right"), constants)
            operator = text(field(n, "operator"))
            if operator == "+=" and value is not None and key in constants:
                value = constants[key] + value
            elif operator != "=":
                value = None
            if value is None or len(value) > 65536:
                constants.pop(key, None)
            else:
                constants[key] = value
        if kind in {"soql_query_body", "sosl_query_body"}:
            query(n, owner)
            return
        if kind == "annotation":
            name = text(field(n, "name"))
            if owner in facts.nodes:
                facts.nodes[owner].setdefault("annotations", []).append(name)
            return
        if kind == "method_invocation":
            method = text(field(n, "name"))
            obj_node = field(n, "object")
            obj = receiver_type(obj_node, variables, class_name, trigger_object)
            args_node = field(n, "arguments")
            args = list(args_node.named_children) if args_node else []
            if obj.casefold() == "database" and method.casefold() in {"query", "countquery", "getquerylocator", "querywithbinds", "countquerywithbinds", "getquerylocatorwithbinds"}:
                literal = constant_string(args[0], constants) if args else None
                if literal is not None:
                    wrapped = ("class Q {void q(){Object v=[" + literal + "];}}").encode()
                    dynamic_tree = get_parser("apex").parse(wrapped)
                    if not dynamic_tree.root_node.has_error:
                        # Reuse the normal visitor with the new source bytes,
                        # then anchor the resulting evidence at the literal.
                        from .model import Source
                        other = Facts(Source(src.path, literal, "SOQL", src.full_name))
                        parse_apex(other)
                        for ref in other.references:
                            ref.update(source=owner, line=line(n), source_location=f"L{line(n)}", source_sha=facts.source_sha, source_file=src.path)
                            facts.references.append(ref)
                    else:
                        facts.level = "partial"
                        facts.issue("dynamic_query_unresolved", line(n))
                else:
                    facts.level = "partial"
                    facts.issue("dynamic_query_unresolved", line(n))
            elif obj.casefold() == "database" and method.casefold() in {"insert", "update", "delete", "upsert", "merge", "undelete"}:
                target = receiver_type(args[0], variables, class_name, trigger_object) if args else ""
                if target:
                    facts.ref(owner, "CustomObject", target, "writes", line(n), operation=method.casefold())
            elif obj.casefold() == "type" and method.casefold() == "forname":
                name = constant_string(args[-1], constants) if args else None
                namespace = constant_string(args[0], constants) if len(args) > 1 else ""
                if name is not None and namespace is not None:
                    if namespace:
                        name = namespace + "." + name
                    facts.ref(owner, "ApexClass", name, "reflects", line(n))
                else:
                    facts.level = "partial"
                    facts.issue("dynamic_type_unresolved", line(n))
            elif obj and obj.casefold() not in BUILTINS:
                arg_types = []
                for a in args:
                    arg_types.append({"string_literal": "String", "int": "Integer",
                                      "boolean": "Boolean"}.get(a.type) or
                                     variables.get(text(a).casefold(), ""))
                facts.ref(owner, "ApexMethod", obj + "." + method, "calls", line(n),
                          arity=len(args), argument_types=arg_types)
        if kind == "object_creation_expression":
            target = type_name(field(n, "type"))
            if target and target.casefold() not in BUILTINS:
                facts.ref(owner, "Type", target, "constructs", line(n))
        if kind == "dml_expression":
            target = receiver_type(field(n, "target"), variables, class_name, trigger_object)
            op_node = next((x for x in n.named_children if x.type == "dml_type"), None)
            if target:
                facts.ref(owner, "CustomObject", target, "writes", line(n), operation=text(op_node).casefold())
            else:
                facts.issue("dml_target_unresolved", line(n))
        if kind == "field_access":
            receiver = receiver_type(field(n, "object"), variables, class_name, trigger_object)
            member = text(field(n, "field"))
            label_path = receiver + "." + member
            if label_path.casefold().startswith(("system.label.", "label.")):
                label_name = label_path.split(".", 2)[2] if label_path.casefold().startswith("system.") else label_path.split(".", 1)[1]
                # The outer field_access captures the whole namespaced label.
                if not n.parent or n.parent.type != "field_access":
                    facts.ref(owner, "CustomLabel", label_name, "references", line(n))
            elif receiver and receiver.casefold() not in BUILTINS:
                relation = "writes" if n.parent and n.parent.type == "assignment_expression" and field(n.parent, "left") == n else "reads"
                facts.ref(owner, "FieldPath", receiver + "." + member, relation, line(n))
        # Branches/loops/nested blocks can mutate an outer variable. Do not
        # carry a guessed branch value into a later query. No Apex is executed.
        control = kind in {"if_statement", "for_statement", "enhanced_for_statement", "while_statement", "do_statement", "switch_expression", "try_statement"}
        if control or (kind == "block" and n.parent and n.parent.type not in {"method_declaration", "constructor_declaration"}):
            for name in assigned_names(n):
                constants.pop(name, None)
            constants = dict(constants)
        if kind in {"block", "enhanced_for_statement"}:
            variables = dict(variables)
        for child in n.named_children:
            visit(child, owner, class_name, variables, trigger_object, dict(constants) if control else constants)

    visit(tree.root_node, src.component_id, "", {})
