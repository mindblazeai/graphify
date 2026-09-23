"""Bounded Tooling schema declarations, never User or business-record data.

The caller captures a single verified FieldDefinition/EntityParticle row. Its
DurableId is an opaque identity, not an API name. Display DataType labels are
never used to guess missing lookup targets. Every reference names its JSON slot.
"""
from __future__ import annotations

from bisect import bisect_right
import json
import math
import re

from .model import Facts

MAX_BYTES = 128 * 1024
API_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
DURABLE_ID = re.compile(r"[A-Za-z0-9_.]{1,255}\Z")
VALUE_TYPES = {"string": "string", "boolean": "boolean", "integer": "int",
               "long": "long", "double": "double", "date": "date", "datetime": "datetime",
               "time": "time", "id": "id", "base64binary": "base64", "address": "address"}
COMMON = {"attributes", "DurableId", "QualifiedApiName", "EntityDefinition", "NamespacePrefix",
          "DataType", "ValueTypeId", "IsCalculated", "IsCompound", "RelationshipName", "ReferenceTo", "ReferenceTargetField"}
FIELDS = {
    "FieldDefinition": COMMON | {"FullName", "Metadata", "IsPolymorphicForeignKey",
                                  "ControllingFieldDefinitionId", "ControllingFieldDefinition"},
    "EntityParticle": COMMON | {"FieldDefinitionId", "FieldDefinition", "IsComponent", "IsNamePointing",
                               "IsDependentPicklist", "DefaultValueFormula"},
}
# Non-reference metadata slots. Everything else with a value stays partial,
# including lookup filters, rollups, value sets and unavailable formula bodies.
BOOLS = set("caseSensitive deprecated displayLocationInDecimal escapeMarkup externalId isAIPredictionField isConvertLeadDisabled isFilteringDisabled isNameField isSortingDisabled populateExistingRows readOnlyProxy reparentableMasterDetail required restrictedAdminField stripMarkup trackFeedHistory trackHistory trackTrending translateData unique writeRequiresMasterRead".split())
TEXT = set("description displayFormat externalDeveloperName inlineHelpText label relationshipLabel".split())
NUMBERS = set("length precision scale relationshipOrder startingNumber visibleLines".split())
ENUMS = {
    "type": set("Address AutoNumber Checkbox Currency Date DateTime Email EncryptedText ExternalLookup Geolocation Hierarchy Html IndirectLookup Location LongTextArea Lookup MasterDetail MetadataRelationship MultiselectPicklist Number Percent Phone Picklist Summary Text TextArea Time Url".split()),
    "deleteConstraint": {"Cascade", "Restrict", "SetNull"},
    "formulaTreatBlanksAs": {"BlankAsBlank", "BlankAsZero"},
    "businessStatus": {"Active", "DeprecateCandidate", "Hidden"},
    "securityClassification": {"Public", "Internal", "Confidential", "Restricted", "MissionCritical"},
}


def _name(value):
    return isinstance(value, str) and len(value) <= 255 and bool(API_NAME.fullmatch(value))


def _durable(value):
    return isinstance(value, str) and bool(DURABLE_ID.fullmatch(value))


def _namespace(name):
    parts = name.split("__")
    return parts[0] if len(parts) >= 3 else ""


def _document(text):
    """Strict JSON plus real value offsets; escaped keys and compact JSON work."""
    def unique(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ValueError("duplicate JSON key")
            out[key] = value
        return out

    def invalid(_):
        raise ValueError("non-finite JSON value")

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON value")
        return number

    decoder = json.JSONDecoder(object_pairs_hook=unique, parse_constant=invalid, parse_float=finite)
    data = decoder.decode(text)
    offsets = {}

    def space(i):
        while i < len(text) and text[i] in " \r\n\t":
            i += 1
        return i

    def visit(i, path, depth):
        if depth > 16 or len(offsets) >= 4096:
            raise ValueError("JSON structure limit")
        i = space(i)
        offsets[path] = i
        if text[i] == "{":
            i = space(i + 1)
            while text[i] != "}":
                key, i = decoder.raw_decode(text, i)
                i = visit(space(i) + 1, path + (key,), depth + 1)
                i = space(i)
                if text[i] == ",":
                    i = space(i + 1)
                else:
                    break
            return i + 1
        if text[i] == "[":
            i, index = space(i + 1), 0
            while text[i] != "]":
                i = space(visit(i, path + (index,), depth + 1))
                index += 1
                if text[i] == ",":
                    i = space(i + 1)
                else:
                    break
            return i + 1
        return decoder.raw_decode(text, i)[1]

    visit(0, (), 0)
    return data, offsets


def parse_field_definition(facts: Facts) -> None:
    source = facts.source
    facts.level = "partial"
    if len(source.content.encode()) > MAX_BYTES:
        facts.issue("field_definition_size_limit", max_bytes=MAX_BYTES)
        return
    try:
        data, offsets = _document(source.content)
    except (ValueError, RecursionError, IndexError):
        facts.issue("field_definition_json_invalid")
        return
    parts = source.full_name.split(".")
    if (source.metadata_type != "CustomField" or source.source_kind != "api"
            or len(parts) != 2 or not all(_name(p) for p in parts)
            or source.path != f"salesforce-api/fieldDefinitions/{source.full_name}/definition.json"
            or not _durable(source.salesforce_id) or not isinstance(data, dict)
            or set(data) != {"formatVersion", "apiVersion", "kind", "fullName", "durableId", "record"}
            or type(data.get("formatVersion")) is not int or data["formatVersion"] != 1
            or data.get("apiVersion") != "v67.0" or not isinstance(data.get("kind"), str) or data["kind"] not in FIELDS
            or data.get("fullName") != source.full_name or data.get("durableId") != source.salesforce_id):
        facts.issue("field_definition_capture_identity_invalid")
        return
    obj, field = parts
    kind, row = data["kind"], data.get("record")
    namespace = row.get("NamespacePrefix") if isinstance(row, dict) else None
    if (not isinstance(row, dict) or row.get("DurableId") != source.salesforce_id
            or row.get("QualifiedApiName") != field or not isinstance(row.get("EntityDefinition"), dict)
            or row["EntityDefinition"].get("QualifiedApiName") != obj or "NamespacePrefix" not in row
            or namespace is not None and not _name(namespace)
            or (namespace or _namespace(field) or _namespace(obj)) != (source.namespace or "")
            or kind == "FieldDefinition" and row.get("FullName") != source.full_name
            or kind == "EntityParticle" and (row.get("IsComponent") is not True
                or not _durable(row.get("FieldDefinitionId")) or not isinstance(row.get("FieldDefinition"), dict)
                or not _name(row["FieldDefinition"].get("QualifiedApiName")))):
        facts.issue("field_definition_row_identity_invalid")
        return
    facts.level = "semantic"
    line_starts = [0] + [match.end() for match in re.finditer("\n", source.content)]

    def location(path):
        key = ("record",) + tuple(path)
        offset = offsets.get(key, offsets[("record",)])
        return {"line": bisect_right(line_starts, offset), "json_path": "$" + "".join(
            f"[{p}]" if isinstance(p, int) else "." + p for p in key)}

    def issue(code, *path, **details):
        facts.level = "partial"
        facts.issue(code, **location(path), **details)

    def ref(target_kind, name, relation, *path, **attrs):
        loc = location(path)
        facts.ref(source.component_id, target_kind, name, relation, **loc,
                  source_location=loc["json_path"], **attrs)

    component = facts.nodes[source.component_id]
    loc = location(("QualifiedApiName",))
    component.update(line=loc["line"], source_location=loc["json_path"], json_path=loc["json_path"])
    ref("CustomObject", obj, "belongs_to", "EntityDefinition", "QualifiedApiName", structural_parent=True)
    if kind == "EntityParticle":
        owner = row["FieldDefinition"]["QualifiedApiName"]
        if (owner == field or row["FieldDefinitionId"] == row["DurableId"]
                or row["FieldDefinitionId"].split(".", 1)[0] != row["DurableId"].split(".", 1)[0]):
            issue("field_definition_particle_owner_invalid", "FieldDefinition")
        else:
            ref("CustomField", obj + "." + owner, "component_of", "FieldDefinition", "QualifiedApiName", structural_parent=True)
    for key in ("EntityDefinition", "FieldDefinition", "ControllingFieldDefinition"):
        nested = row.get(key)
        if isinstance(nested, dict):
            for extra in sorted(set(nested) - {"attributes", "QualifiedApiName"}):
                if nested[extra] is not None:
                    issue("field_definition_property_unsupported", key, extra, property=extra)
    for key in sorted(FIELDS[kind] - {"attributes"} - set(row)):
        issue("field_definition_property_missing", key, property=key)
    for key in sorted(set(row) - FIELDS[kind]):
        if row[key] is not None:
            issue("field_definition_property_unsupported", key, property=key)
    for key in sorted({"IsCalculated", "IsCompound"} | (
            {"IsPolymorphicForeignKey"} if kind == "FieldDefinition" else {"IsNamePointing", "IsDependentPicklist"})):
        if type(row.get(key)) is not bool:
            issue("field_definition_boolean_invalid", key)
    value_type = row.get("ValueTypeId")
    if not isinstance(value_type, str) or value_type not in VALUE_TYPES:
        issue("field_definition_value_type_unknown", "ValueTypeId")
    else:
        component["data_type"] = VALUE_TYPES[value_type]
    if not isinstance(row.get("DataType"), str) or not row["DataType"]:
        issue("field_definition_data_type_invalid", "DataType")
    relation = row.get("RelationshipName")
    relation_valid = relation is None or _name(relation)
    if not relation_valid:
        issue("field_definition_relationship_invalid", "RelationshipName")
    container = row.get("ReferenceTo")
    targets = container.get("referenceTo") if isinstance(container, dict) and set(container) == {"referenceTo"} else None
    targets_valid = (container is None or isinstance(container, dict) and set(container) == {"referenceTo"}) and (
        targets is None or isinstance(targets, list) and len(targets) <= 256 and all(_name(t) for t in targets)
        and len({t.casefold() for t in targets}) == len(targets))
    if not targets_valid:
        issue("field_definition_reference_targets_invalid", "ReferenceTo")
        targets = []
    targets = targets or []
    if targets:
        if value_type != "id":
            issue("field_definition_reference_type_conflict", "ReferenceTo")
        else:
            component.update(data_type="reference", reference_to=targets)
            if relation_valid and relation:
                component["parent_relationship_name"] = relation
            for index, target in enumerate(targets):
                ref("CustomObject", target, "references_object", "ReferenceTo", "referenceTo", index,
                    polymorphic=row.get("IsPolymorphicForeignKey") is True)
    elif value_type == "id" and field != "Id":
        issue("field_definition_reference_targets_unavailable", "ReferenceTo")
    if row.get("IsPolymorphicForeignKey") is True and len(targets) < 2:
        issue("field_definition_polymorphic_targets_incomplete", "ReferenceTo")
    target_field = row.get("ReferenceTargetField")
    if target_field is not None:
        name = target_field if isinstance(target_field, str) else ""
        if _name(name) and len(targets) == 1:
            name = targets[0] + "." + name
        bits = name.split(".")
        if value_type != "id" or len(bits) != 2 or not all(_name(b) for b in bits) or bits[0] not in targets:
            issue("field_definition_reference_target_field_invalid", "ReferenceTargetField")
        else:
            ref("CustomField", name, "references_field", "ReferenceTargetField")
    controller, controller_id = row.get("ControllingFieldDefinition"), row.get("ControllingFieldDefinitionId")
    if controller is not None or controller_id is not None:
        if (not _durable(controller_id) or not isinstance(controller, dict)
                or not _name(controller.get("QualifiedApiName"))
                or controller_id.split(".", 1)[0] != row["DurableId"].split(".", 1)[0]
                or controller["QualifiedApiName"] == field):
            issue("field_definition_controller_invalid", "ControllingFieldDefinition")
        else:
            ref("CustomField", obj + "." + controller["QualifiedApiName"], "controlled_by",
                "ControllingFieldDefinition", "QualifiedApiName")
    if row.get("IsCompound") is True or value_type == "address":
        issue("field_definition_compound_members_unavailable", "IsCompound")
    if row.get("IsCalculated") is True:
        issue("field_definition_calculation_unavailable", "IsCalculated")
    if row.get("IsDependentPicklist") is True:
        issue("field_definition_particle_controller_unavailable", "IsDependentPicklist")
    if row.get("DefaultValueFormula") is not None:
        issue("field_definition_formula_unsupported", "DefaultValueFormula")
    metadata = row.get("Metadata")
    if metadata is not None and not isinstance(metadata, dict):
        issue("field_definition_metadata_invalid", "Metadata")
    elif isinstance(metadata, dict):
        for key, value in metadata.items():
            if value is None:
                continue
            valid = (key in BOOLS and type(value) is bool or key in TEXT and isinstance(value, str)
                     or key in NUMBERS and type(value) in {int, float} and 0 <= value <= 2147483647
                     or key in ENUMS and isinstance(value, str) and value in ENUMS[key])
            if not valid:
                issue("field_definition_metadata_property_unsupported", "Metadata", key, property=key)
