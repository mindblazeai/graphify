"""Typed translation identities; translated prose is never executable syntax."""
from __future__ import annotations

import re

CASE_FIELDS = "article caseType plural possessive value"
FIELD_FIELDS = "caseValues description gender help label lookupFilter name picklistValues relationshipLabel startsWith"
SHAPES = {
    "CustomObjectTranslation": {
        "": "caseValues fields fieldSets gender layouts nameFieldLabel namedFilters quickActions recordTypes sharingReasons startsWith validationRules webLinks workflowTasks",
        "caseValues": CASE_FIELDS,
        "fields": FIELD_FIELDS,
        "fields/caseValues": CASE_FIELDS,
        "fields/lookupFilter": "errorMessage informationalMessage",
        "fields/picklistValues": "masterLabel translation",
        "fieldSets": "label name",
        "layouts": "layout layoutType sections",
        "layouts/sections": "label section",
        "namedFilters": "errorMessage informationalMessage name",
        "quickActions": "aspect label name",
        "recordTypes": "description label name",
        "sharingReasons": "label name",
        "validationRules": "errorMessage name",
        "webLinks": "label name",
        "workflowTasks": "description name subject",
    },
    "CustomFieldTranslation": {
        "": FIELD_FIELDS, "caseValues": CASE_FIELDS,
        "lookupFilter": "errorMessage informationalMessage", "picklistValues": "masterLabel translation",
    },
    "GlobalValueSetTranslation": {"": "valueTranslation", "valueTranslation": "masterLabel translation"},
    "StandardValueSetTranslation": {"": "valueTranslation", "valueTranslation": "masterLabel translation"},
}


def parse_translation(facts, root, kind, *, issue, scalar, ref, children):
    owner = facts.source.component_id
    source_name = facts.source.full_name
    field_key = None
    if kind == "CustomFieldTranslation":
        source_name, dot, field_key = source_name.rpartition(".")
        if not dot:
            issue("translation_parent_context_missing")
            return
    base, separator, language = source_name.rpartition("-")
    if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", base) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", language):
        # Legacy packaged object spellings cannot safely be reordered into an
        # API identity. A locale is context, never another component declaration.
        issue("translation_identity_context_unverified")
        return
    declared_name = scalar(root, "fullName")
    if declared_name and declared_name.text.strip().casefold() != facts.source.full_name.casefold():
        issue("translation_source_identity_mismatch", declared_name)
        return
    target_kind = {"GlobalValueSetTranslation": "GlobalValueSet", "StandardValueSetTranslation": "StandardValueSet"}.get(kind, "CustomObject")
    if kind != "CustomFieldTranslation":
        ref(root, target_kind, "translates", name=base, identity_contract="translation")
    if target_kind != "CustomObject":
        return

    def member(n, target_kind, source=owner, separator="."):
        if n is None:
            return
        value = n.text.strip()
        if separator == ".":
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                issue("translation_member_identity_unverified", n)
                return
            name = base + "." + value
        else:
            # Layout names are object-qualified with '-', not '.', and labels
            # may contain spaces or further hyphens. Never search by basename.
            name = value if value.casefold().startswith(base.casefold() + "-") else base + "-" + value
        ref(n, target_kind, "translates", name=name, source=source, identity_contract="translation")

    if kind == "CustomFieldTranslation":
        field = scalar(root, "name", required=True)
        if field and field.text.strip().casefold() != field_key.casefold():
            issue("translation_source_identity_mismatch", field)
        else:
            member(field, "CustomField")
        return

    for field in children(root, "fields"):
        name = scalar(field, "name", required=True)
        if not name:
            continue
        # Preserve the registry's existing embedded translation identity and
        # containment edge. Binding must not relabel an opaque standard alias.
        full_name = facts.source.full_name + "." + name.text.strip()
        child = facts.declare("CustomFieldTranslation", full_name, field.line)
        facts.ref(owner, "CustomFieldTranslation", full_name, "contains", field.line)
        member(name, "CustomField", child)
    for tag, target in (("fieldSets", "FieldSet"), ("quickActions", "QuickAction"),
                        ("recordTypes", "RecordType"), ("sharingReasons", "SharingReason"),
                        ("validationRules", "ValidationRule"), ("webLinks", "WebLink"),
                        ("workflowTasks", "WorkflowTask")):
        for entry in children(root, tag):
            member(scalar(entry, "name", required=True), target)
    for layout in children(root, "layouts"):
        member(scalar(layout, "layout", required=True), "Layout", separator="-")
    label = scalar(root, "nameFieldLabel")
    if label:
        # Require the independently declared Name field, never a display-label
        # match or a guess that CaseNumber/Subject is the equivalent field.
        ref(label, "CustomField", "translates", name=base + ".Name", identity_contract="translation")
    for legacy in children(root, "namedFilters"):
        issue("translation_legacy_filter_identity_unverified", legacy)
