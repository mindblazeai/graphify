"""Experience audience contracts from Metadata API v68, Audience, pp. 443-449.

Criteria values are data unless their criterion type explicitly identifies a
metadata slot. Numbered filter logic is not a Salesforce field expression.
Experience-variation targets still require an independently indexed bundle.
"""
from __future__ import annotations

from collections import Counter
import re

from .model import salesforce_id
from .setup import validate_literals


CRITERION_VALUES = {
    "GeoLocation": "city country subdivision",
    "Domain": "domain",
    "Profile": "profile",
    "FieldBased": "entityField entityType fieldValue",
    "Permission": "isEnabled permissionName permissionType",
    "Default": "",
    "Audience": "audienceDeveloperName",
}
CRITERION_FIELDS = "criteriaNumber criterionValue operator type"
VALUE_FIELDS = " ".join(dict.fromkeys(" ".join(CRITERION_VALUES.values()).split()))
SHAPES = {
    "Audience": {
        "": "audienceName container criteria criterion description formula formulaFilterType isDefaultAudience targets",
        "criteria": "criterion",
        "criteria/criterion": CRITERION_FIELDS,
        "criteria/criterion/criterionValue": VALUE_FIELDS,
        "criterion": CRITERION_FIELDS,
        "criterion/criterionValue": VALUE_FIELDS,
        "targets": "target",
        "targets/target": "groupName priority targetType targetValue",
    },
}
OPERATORS = frozenset("Equal NotEqual GreaterThan GreaterThanOrEqual LessThan LessThanOrEqual Contains StartsWith Includes NotIncludes".split())


def _valid_filter_logic(expression: str, criterion_numbers: set[int]) -> bool:
    """Bounded, fully consumed boolean grammar over existing criterion IDs."""
    if not expression or len(expression) > 4096:
        return False
    parts = re.findall(r"\d+|[A-Za-z_]+|[^\s]", expression)
    expecting_operand, depth = True, 0
    for token in parts:
        token = token.upper()
        if expecting_operand:
            if token == "NOT":
                continue
            if token == "(":
                depth += 1
                if depth > 64:
                    return False
            elif token.isascii() and token.isdigit() and len(token) <= 3 and int(token) in criterion_numbers:
                expecting_operand = False
            else:
                return False
        elif token in {"AND", "OR"}:
            expecting_operand = True
        elif token == ")" and depth:
            depth -= 1
        else:
            return False
    return not expecting_operand and depth == 0


def parse_audience(facts, root, *, issue, scalar, ref, children):
    owner = facts.source.component_id

    def value(n):
        return n.text.strip() if n else ""

    def enum(n, allowed):
        if n is not None and value(n) not in allowed:
            issue("audience_value_unsupported", n, property=n.tag)
            return False
        return n is not None

    def named(n, kind, relation="references", name=None, **attrs):
        if n is not None:
            target = value(n) if name is None else name
            if target and len(target) <= 1024:
                ref(n, kind, relation, name=target, identity_contract="audience", **attrs)
            else:
                issue("metadata_reference_value_unsupported", n, property=n.tag)

    def singleton_container(n, tag):
        found = children(n, tag)
        if len(found) > 1:
            issue("audience_container_ambiguous", n, property=tag)
            return None
        return found[0] if found else None

    validate_literals(root, SHAPES["Audience"], issue=issue, scalar=scalar)
    scalar(root, "fullName")
    scalar(root, "audienceName", required=True)
    container = scalar(root, "container", required=True)
    if container:
        facts.nodes[owner]["audience_container"] = value(container)
        # The container is a site or org name. An org-only container cannot
        # resolve without its own declaration; never normalize it to a site.
        named(container, "Network", "belongs_to")
    default = scalar(root, "isDefaultAudience")
    enum(default, {"true", "false", "1", "0"})
    filter_type = scalar(root, "formulaFilterType", required=True)
    enum(filter_type, {"AllCriteriaMatch", "AnyCriterionMatches", "CustomLogicMatches"})
    group = singleton_container(root, "criteria")
    legacy = children(root, "criterion")
    if group is not None and legacy:
        issue("audience_criteria_version_conflict", group)
    criteria = children(group, "criterion") if group is not None else legacy
    if len(criteria) > 100:
        issue("audience_criteria_limit", group or root)
        criteria = []
    if not criteria and value(default) not in {"true", "1"}:
        issue("audience_criteria_missing", group or root)
    numbers = []
    for criterion in criteria:
        number = scalar(criterion, "criteriaNumber", required=value(filter_type) == "CustomLogicMatches")
        if number is not None:
            if not re.fullmatch(r"[1-9][0-9]{0,2}", value(number)):
                issue("audience_criterion_number_invalid", number)
            else:
                numbers.append(int(value(number)))
        typ = scalar(criterion, "type", required=True)
        mode = value(typ)
        if not enum(typ, CRITERION_VALUES):
            continue
        enum(scalar(criterion, "operator"), OPERATORS)
        entry = singleton_container(criterion, "criterionValue")
        if entry is None:
            if mode != "Default":
                issue("audience_criterion_value_missing", criterion)
            continue
        if any(c.tag not in CRITERION_VALUES[mode].split() for c in entry.children):
            issue("audience_criterion_value_context_invalid", entry, criterion_type=mode)
            continue
        if mode == "Profile":
            named(scalar(entry, "profile", required=True), "Profile", "checks_profile")
        elif mode == "FieldBased":
            obj = scalar(entry, "entityType", required=True)
            field = scalar(entry, "entityField", required=True)
            field_value = scalar(entry, "fieldValue", required=True)
            if obj and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value(obj)):
                issue("audience_field_context_invalid", obj)
                continue
            named(obj, "CustomObject", "references_object")
            if obj and field:
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", value(field)):
                    issue("audience_field_context_invalid", field)
                else:
                    name = value(field) if value(field).casefold().startswith(value(obj).casefold() + ".") else value(obj) + "." + value(field)
                    named(field, "FieldPath", "reads", name=name)
                    # The API's RecordTypeId example uses Object.DeveloperName
                    # as its field value. Other criterion values remain data,
                    # even when they happen to resemble a metadata API name.
                    if value(field).rsplit(".", 1)[-1].casefold() == "recordtypeid" and field_value:
                        target = value(field_value)
                        if target.startswith("012") and salesforce_id(target):
                            named(field_value, "RecordType", "checks_record_type", target_salesforce_id=target)
                        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*", target):
                            direct = value(field).casefold() in {"recordtypeid", value(obj).casefold() + ".recordtypeid"}
                            if direct and target.split(".", 1)[0].casefold() != value(obj).casefold():
                                issue("audience_record_type_context_invalid", field_value)
                            else:
                                named(field_value, "RecordType", "checks_record_type")
                        else:
                            issue("audience_record_type_identity_unverified", field_value)
        elif mode == "Permission":
            enabled = scalar(entry, "isEnabled", required=True)
            enum(enabled, {"true", "false"})
            permission_type = scalar(entry, "permissionType", required=True)
            permission = scalar(entry, "permissionName", required=True)
            if enum(permission_type, {"Custom", "Standard"}) and permission:
                if value(permission_type) == "Custom":
                    named(permission, "CustomPermission", "checks_permission")
                else:
                    facts.nodes[owner].setdefault("standard_permission_filters", []).append(value(permission))
        elif mode == "Audience":
            if container:
                named(scalar(entry, "audienceDeveloperName", required=True), "Audience", "checks_audience",
                      target_audience_container=value(container))
            else:
                issue("audience_reference_scope_missing", entry)
        elif mode == "Domain":
            scalar(entry, "domain", required=True)
        elif mode == "GeoLocation" and not any(value(scalar(entry, tag)) for tag in CRITERION_VALUES[mode].split()):
            issue("audience_criterion_value_missing", entry)

    if any(count > 1 for count in Counter(numbers).values()):
        issue("audience_criterion_number_duplicate")
    formula = scalar(root, "formula")
    if value(filter_type) == "CustomLogicMatches":
        if not _valid_filter_logic(value(formula), set(numbers)):
            issue("audience_filter_logic_unverified", formula or root)
    elif formula:
        issue("audience_filter_logic_context_conflict", formula)

    targets = singleton_container(root, "targets")
    entries = children(targets, "target") if targets is not None else []
    if len(entries) > 25000:
        issue("audience_target_limit", targets)
        return
    for target in entries:
        scalar(target, "groupName", required=True)
        priority = scalar(target, "priority")
        if priority and not re.fullmatch(r"[0-9]{1,9}", value(priority)):
            issue("audience_value_unsupported", priority, property=priority.tag)
        typ = scalar(target, "targetType", required=True)
        target_value = scalar(target, "targetValue", required=True)
        mode = value(typ)
        if mode == "ExperienceVariation":
            issue("audience_experience_variation_not_indexed", target)
        elif enum(typ, {"NavigationLinkSet", "Report", "Dashboard"}):
            kind = "NavigationMenu" if mode == "NavigationLinkSet" else mode
            attrs = {"target_salesforce_id": value(target_value)} if salesforce_id(value(target_value)) else {}
            named(target_value, kind, "targets", **attrs)
