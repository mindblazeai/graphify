"""XML structure, declarative dependencies, and embedded Salesforce expressions."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from xml.parsers import expat

from .model import Facts
from .registry import registry
from .declarative import SHAPES, parse_declarative


@dataclass
class Element:
    tag: str
    attributes: dict
    line: int
    children: list[Element] = field(default_factory=list)
    text: str = ""

    def value(self, tag: str) -> str:
        return next((c.text.strip() for c in self.children if c.tag == tag), "")


def parse_xml(content: str) -> Element:
    # Namespace processing is intentionally off: Visualforce and Aura use
    # framework prefixes without XML namespace declarations. Metadata tags
    # themselves are normalized by local name below.
    parser = expat.ParserCreate()
    stack = []
    roots = []

    def start(tag, attrs):
        node = Element(tag.split(":")[-1], attrs, parser.CurrentLineNumber)
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    def end(_):
        stack.pop()

    def data(value):
        if stack:
            stack[-1].text += value

    def reject(*_):
        raise ValueError("DTD and external entities are unsupported")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = data
    parser.StartDoctypeDeclHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.Parse(content, True)
    return roots[0]


EXPRESSION_TOKEN = re.compile(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|\$?[A-Za-z_][\w$]*(?:\.[A-Za-z_][\w$]*)*")
FORMULA_CONSTANTS = {"true", "false", "null", "and", "or", "not"}


def expression_refs(facts: Facts, owner: str, expression: str, obj: str, line: int,
                    variables: dict[str, str] | None = None, relation: str = "reads") -> None:
    """Tokenize formulas and merge expressions; strings/functions aren't fields.

    This extracts dependencies; it does not evaluate Salesforce formulas.
    Binding is deferred to the org schema, including relationship traversal.
    """
    variables = {k.casefold(): v for k, v in (variables or {}).items()}
    for match in EXPRESSION_TOKEN.finditer(expression):
        value = match.group()
        if value[0] in "'\"" or value.casefold() in FORMULA_CONSTANTS:
            continue
        if expression[match.end():].lstrip().startswith("("):
            continue
        bits = value.split(".")
        if facts.source.metadata_type == "AuraDefinitionBundle" and bits[0] in {"c", "v"}:
            continue  # Client handlers/attributes are not schema fields.
        if bits[0].casefold() == "$label" and len(bits) > 1:
            label = bits[2:] if len(bits) > 2 and bits[1] == "c" else bits[1:]
            facts.ref(owner, "CustomLabel", ".".join(label), "references", line)
        elif bits[0].casefold() == "$permission" and len(bits) > 1:
            facts.ref(owner, "CustomPermission", ".".join(bits[1:]), "checks_permission", line)
        elif bits[0].casefold() == "$custommetadata" and len(bits) > 2:
            facts.ref(owner, "CustomMetadata", bits[1].removesuffix("__mdt") + "." + bits[2], "reads", line)
            if len(bits) > 3:
                facts.ref(owner, "FieldPath", bits[1] + "." + ".".join(bits[3:]), "reads", line)
        elif bits[0].casefold() in {"$record", "$record__prior"} and len(bits) > 1 and obj:
            facts.ref(owner, "FieldPath", obj + "." + ".".join(bits[1:]), relation, line)
        elif bits[0].casefold() in variables and len(bits) > 1:
            facts.ref(owner, "FieldPath", variables[bits[0].casefold()] + "." + ".".join(bits[1:]), relation, line)
        elif bits[0].casefold() in {"relatedto", "recipient"} and len(bits) > 1:
            # Bind against the template's declared context after all bundle
            # files are parsed. Never guess Contact vs Lead vs User.
            facts.ref(owner, "TemplateField", value, relation, line)
        elif not value.startswith("$") and obj:
            facts.ref(owner, "FieldPath", value if value.casefold().startswith(obj.casefold() + ".") else obj + "." + value, relation, line)
        elif not value.startswith("$") and len(bits) > 1:
            facts.ref(owner, "FieldPath", value, relation, line)


MERGE_EXPRESSION = re.compile(r"\{\{\{?\s*(.*?)\s*\}\}\}?|\{[!#]([^}]+)\}", re.DOTALL)


def merge_refs(facts: Facts, owner: str, text: str, obj: str, line: int,
               variables: dict[str, str] | None = None) -> None:
    """Only delimited merge expressions, never arbitrary prose or API names."""
    for match in MERGE_EXPRESSION.finditer(text):
        expression = match.group(1) if match.group(1) is not None else match.group(2)
        if expression.startswith(("!", "/")):
            continue  # Handlebars comment/closing helper.
        expression_refs(facts, owner, expression, obj,
                        line + text[:match.start()].count("\n"), variables)


FLOW_ELEMENTS = {"start", "actionCalls", "apexPluginCalls", "assignments", "collectionProcessors",
                 "decisions", "loops", "recordCreates", "recordDeletes", "recordLookups",
                 "recordUpdates", "screens", "subflows", "transforms", "waits"}
TYPE_REFERENCES = {
    "apexClass": "ApexClass", "apexPage": "ApexPage", "apexComponent": "ApexComponent",
    "flowName": "Flow", "flow": "Flow", "subflowName": "Flow",
    "permissionSet": "PermissionSet", "permissionSetName": "PermissionSet",
    "permissionSets": "PermissionSet", "mutingPermissionSets": "MutingPermissionSet",
    "customPermission": "CustomPermission", "recordType": "RecordType",
    "layout": "Layout", "template": "EmailTemplate", "emailTemplate": "EmailTemplate",
    "namedCredential": "NamedCredential", "externalCredential": "ExternalCredential",
    "authProvider": "AuthProvider", "report": "Report", "reportType": "ReportType",
    "dashboard": "Dashboard", "customTab": "CustomTab", "tabs": "CustomTab",
    "application": "CustomApplication", "notificationType": "CustomNotificationType",
    "platformEvent": "CustomObject", "quickActionName": "QuickAction",
    "componentName": "LightningComponentBundle", "lightningWebComponent": "LightningComponentBundle",
    "lightningComponent": "AuraDefinitionBundle", "contentAsset": "ContentAsset",
    "letterhead": "Letterhead", "enhancedLetterhead": "EnhancedLetterhead",
    "matchingRule": "MatchingRule", "matchingRuleName": "MatchingRule",
    "externalDataSource": "ExternalDataSource", "valueSetName": "GlobalValueSet",
    "globalValueSet": "GlobalValueSet", "businessProcess": "BusinessProcess",
    "queue": "Queue", "group": "Group",
}
FORMULAS = {"formula", "errorConditionFormula", "criteriaFormula", "booleanFilter", "expression"}
FIELD_TAGS = {"field", "fields", "fieldName", "displayField", "sortField", "summarizedField",
              "summaryForeignKey", "lookupField", "controllingField",
              "externalIdField", "relatedField", "fieldItem"}
OBJECT_TAGS = {"object", "objectType", "sObjectType", "sobjectType", "sourceObject",
               "targetObject", "referenceTo", "relatedObject", "baseObject"}
SEMANTIC_TYPES = {
    "CustomObject", "CustomField", "ValidationRule", "Flow", "FlowDefinition", "Layout",
    "CompactLayout", "FieldSet", "ListView", "RecordType", "PermissionSet", "Profile",
    "PermissionSetGroup", "MutingPermissionSet", "Workflow", "WorkflowRule",
    "WorkflowFieldUpdate", "WorkflowAlert", "WorkflowTask", "WorkflowOutboundMessage",
    "ApprovalProcess", "SharingRules", "SharingCriteriaRule", "SharingOwnerRule",
    "ApexPage", "ApexComponent", "AuraDefinitionBundle", "LightningComponentBundle",
    "NamedCredential", "ExternalCredential", "AuthProvider", "ConnectedApp", "CustomMetadata",
    "CustomLabels", "CustomLabel", "CustomApplication", "CustomTab", "QuickAction",
    "FlexiPage", "Report", "ReportType", "Dashboard", "EmailTemplate", "AssignmentRules",
    "AutoResponseRules", "EscalationRules", "EntitlementProcess", "MatchingRules", "DuplicateRule",
}
CHILD_TAGS = {
    "CustomObject": {"fields": "CustomField", "validationRules": "ValidationRule",
                     "recordTypes": "RecordType", "fieldSets": "FieldSet", "listViews": "ListView",
                     "compactLayouts": "CompactLayout", "businessProcesses": "BusinessProcess",
                     "webLinks": "WebLink", "sharingReasons": "SharingReason", "indexes": "Index"},
    "Workflow": {"rules": "WorkflowRule", "fieldUpdates": "WorkflowFieldUpdate",
                 "alerts": "WorkflowAlert", "tasks": "WorkflowTask",
                 "outboundMessages": "WorkflowOutboundMessage"},
    "SharingRules": {"sharingCriteriaRules": "SharingCriteriaRule",
                     "sharingOwnerRules": "SharingOwnerRule", "sharingTerritoryRules": "SharingTerritoryRule"},
    "CustomLabels": {"labels": "CustomLabel"},
    "MatchingRules": {"matchingRules": "MatchingRule"},
}


def parse_metadata(facts: Facts) -> None:
    src = facts.source
    try:
        root = parse_xml(src.content)
    except (expat.ExpatError, ValueError, IndexError) as exc:
        facts.level = "partial"
        facts.issue("xml_parse_error", getattr(exc, "lineno", 1))
        return
    kind = root.tag if src.metadata_type == "Settings" and root.tag.endswith("Settings") and root.tag in SHAPES else src.metadata_type
    if kind in SHAPES:
        parse_declarative(facts, root, kind)
        return
    facts.level = "semantic" if src.metadata_type in SEMANTIC_TYPES else "structural"
    if src.metadata_type == "Profile":
        # Salesforce only returns some Profile permissions when the related
        # components are in the retrieve manifest. Presence is evidence;
        # absence is never an effective-permission decision.
        facts.level = "partial"
        facts.issue("profile_retrieve_manifest_limited")
    parent_object = ""
    if src.metadata_type in {"CustomObject", "Workflow", "SharingRules", "AssignmentRules",
                             "AutoResponseRules", "EscalationRules", "MatchingRules"}:
        parent_object = src.full_name
    elif src.metadata_type in {"CustomField", "ValidationRule", "RecordType", "FieldSet", "ListView",
                               "CompactLayout", "WorkflowRule", "WorkflowFieldUpdate", "ApprovalProcess"}:
        parent_object = src.full_name.split(".")[0]
    elif src.metadata_type == "Layout":
        parent_object = src.full_name.split("-", 1)[0]
    elif src.metadata_type == "QuickAction":
        parent_object = root.value("targetObject") or (src.full_name.split(".")[0] if "." in src.full_name else "")
    elif src.metadata_type == "ReportType":
        parent_object = root.value("baseObject")
    elif src.metadata_type == "FlexiPage":
        parent_object = root.value("sobjectType")
    elif src.metadata_type == "CustomMetadata":
        typ = src.full_name.split(".", 1)[0]
        parent_object = typ if typ.endswith("__mdt") else typ + "__mdt"
    if src.metadata_type == "EmailTemplate" and root.value("relatedEntityType"):
        facts.nodes[src.component_id]["related_object"] = root.value("relatedEntityType")
    if src.metadata_type == "Report":
        facts.nodes[src.component_id]["report_type"] = root.value("reportType")
    variables = {}
    if src.metadata_type == "FlexiPage" and parent_object:
        variables["Record"] = parent_object
    for child in root.children:
        obj = child.value("object") or child.value("objectType")
        if child.tag == "start" and obj:
            parent_object = obj
        if child.value("name") and obj:
            variables[child.value("name")] = obj
    child_types = dict(CHILD_TAGS.get(src.metadata_type, {}))
    # The registry extends structural child coverage when Salesforce adds types.
    for value in registry().get(src.metadata_type, {}).get("children", {}).values():
        child_types.setdefault(value.get("xml_element", value["directory"]), value["name"])

    def visit(n: Element, owner: str, obj: str, parent: Element | None = None):
        text = n.text.strip()
        if n is not root and parent is root and n.tag in child_types:
            name = n.value("fullName") or n.value("name")
            if name:
                typ = child_types[n.tag]
                full = name if typ == "CustomLabel" else src.full_name + "." + name
                owner = facts.declare(typ, full, n.line)
                facts.ref(src.component_id, typ, full, "contains", n.line)
        if src.metadata_type == "Flow" and parent is root and n.tag in FLOW_ELEMENTS:
            name = n.value("name") or n.tag
            owner = facts.declare("FlowElement", src.full_name + "." + name, n.line,
                                  element_type=n.tag)
            facts.ref(src.component_id, "FlowElement", src.full_name + "." + name, "contains", n.line)
        obj = n.value("object") or n.value("objectType") or n.value("sobjectType") or n.value("sObjectType") or obj
        if src.metadata_type == "ReportType":
            obj = n.value("table") or obj
        if n.tag == "fields" and n.value("fullName"):
            # Preserve lookup semantics for SOQL/formula relationship resolution.
            node = facts.nodes.get(owner, {})
            node["reference_to"] = [c.text.strip() for c in n.children if c.tag == "referenceTo"]
            node["relationship_name"] = n.value("relationshipName")
            node["data_type"] = n.value("type")
        if n is root and src.metadata_type == "CustomField":
            node = facts.nodes[owner]
            node["reference_to"] = [c.text.strip() for c in n.children if c.tag == "referenceTo"]
            node["relationship_name"] = n.value("relationshipName")
            node["data_type"] = n.value("type")
        if n.tag in OBJECT_TAGS and text:
            facts.ref(owner, "CustomObject", text, "references_object", n.line)
        if n.tag in FIELD_TAGS and text and not n.children:
            if n.tag == "fieldItem" and text.startswith("Record."):
                text = text.removeprefix("Record.")
            if src.metadata_type == "Report":
                facts.ref(owner, "ReportColumn", text.replace("$", "."), "references_field", n.line,
                          report_column=text)
            name = text if "." in text or not obj else obj + "." + text
            if src.metadata_type == "ReportType" and obj and not text.casefold().startswith(obj.casefold() + "."):
                name = obj + "." + text
            if "." in name and src.metadata_type != "Report":
                relation = "writes" if parent and parent.tag in {"inputAssignments", "fieldUpdates"} else "references_field"
                facts.ref(owner, "FieldPath", name, relation, n.line,
                          **({"context_object": parent_object} if src.metadata_type == "ReportType" else {}))
        if n.tag in TYPE_REFERENCES and text and not n.children:
            typ = TYPE_REFERENCES[n.tag]
            name = text
            if typ in {"LightningComponentBundle", "AuraDefinitionBundle"}:
                # c:foo and c/foo name local components; package namespaces stay.
                name = re.sub(r"^c[:/]", "", name).replace(":", ".").replace("/", ".")
            if typ in {"RecordType", "BusinessProcess", "MatchingRule"} and "." not in name and obj:
                name = obj + "." + name
            facts.ref(owner, typ, name, "references", n.line)
        if n.tag in FORMULAS and text:
            expression_refs(facts, owner, text, obj, n.line, variables)
        if n.tag in {"elementReference", "assignToReference", "leftValueReference"} and text:
            # A scalar Flow variable isn't a field. Record variables and
            # $Record paths have a declared object scope.
            if "." in text:
                expression_refs(facts, owner, text, obj, n.line, variables,
                                "writes" if n.tag == "assignToReference" else "reads")
        if not n.children and text and n.tag not in FORMULAS:
            merge_refs(facts, owner, text, obj, n.line, variables)
        for value in n.attributes.values():
            merge_refs(facts, owner, value, obj, n.line, variables)
        if src.metadata_type == "Flow":
            if n.tag == "targetReference" and text:
                facts.ref(owner, "FlowElement", src.full_name + "." + text, "flows_to", n.line)
            if n.tag in {"actionCalls", "apexPluginCalls"}:
                action_type = n.value("actionType")
                action_name = n.value("actionName") or n.value("apexClass")
                if action_name and (action_type == "apex" or n.tag == "apexPluginCalls"):
                    facts.ref(owner, "InvocableApex", action_name, "invokes", n.line)
                elif action_name and action_type in {"flow", "subflow"}:
                    facts.ref(owner, "Flow", action_name, "invokes", n.line)
            if n.tag in {"recordCreates", "recordUpdates", "recordDeletes", "recordLookups"} and obj:
                relation = "queries" if n.tag == "recordLookups" else "writes"
                facts.ref(owner, "CustomObject", obj, relation, n.line, operation=n.tag)
        if n.tag in {"objectPermissions", "fieldPermissions", "classAccesses", "pageAccesses", "customPermissions"}:
            target_map = {"objectPermissions": ("CustomObject", "object"),
                          "fieldPermissions": ("CustomField", "field"),
                          "classAccesses": ("ApexClass", "apexClass"),
                          "pageAccesses": ("ApexPage", "apexPage"),
                          "customPermissions": ("CustomPermission", "name")}
            typ, tag = target_map[n.tag]
            name = n.value(tag)
            permissions = {c.tag: c.text.strip() == "true" for c in n.children
                           if c.tag != tag and c.text.strip() in {"true", "false"}}
            if name:
                facts.ref(owner, typ, name, "grants_access" if any(permissions.values()) else "configures_access",
                          n.line, permissions=permissions)
        if n.tag == "actions" and n.value("name"):
            typ = {"FieldUpdate": "WorkflowFieldUpdate", "Alert": "WorkflowAlert",
                   "Task": "WorkflowTask", "OutboundMessage": "WorkflowOutboundMessage"}.get(n.value("type"))
            if typ:
                name = n.value("name")
                facts.ref(owner, typ, name if "." in name else obj + "." + name, "invokes", n.line)
        for child in n.children:
            visit(child, owner, obj, n)

    visit(root, src.component_id, parent_object)
    if src.metadata_type == "CustomMetadata" and "." in src.full_name:
        typ = src.full_name.split(".", 1)[0]
        facts.ref(src.component_id, "CustomObject", typ if typ.endswith("__mdt") else typ + "__mdt", "instance_of")
    if facts.level == "structural":
        facts.issue("generic_metadata_adapter", metadata_type=src.metadata_type)


def parse_json_metadata(facts: Facts) -> None:
    """Experience/Digital bundles and future JSON types retain structure.

    Only explicitly typed properties become dependencies; arbitrary strings
    and UUIDs are not guessed to be component references.
    """
    try:
        root = json.loads(facts.source.content)
    except (ValueError, RecursionError):
        facts.level = "partial"
        facts.issue("json_parse_error")
        return

    def visit(value, path="$", depth=0):
        if depth > 100:
            facts.level = "partial"
            facts.issue("json_depth_limit", json_path=path)
            return
        if isinstance(value, dict):
            for key, child in value.items():
                location = path + "." + key
                if isinstance(child, str) and key in TYPE_REFERENCES:
                    typ = TYPE_REFERENCES[key]
                    name = re.sub(r"^c[:/]", "", child).replace(":", ".")
                    facts.ref(facts.source.component_id, typ, name, "references",
                              source_location=location, json_path=location)
                visit(child, location, depth + 1)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                visit(child, f"{path}[{i}]", depth + 1)

    visit(root)
    facts.issue("generic_json_adapter", metadata_type=facts.source.metadata_type)
