from copy import deepcopy
from dataclasses import replace
import json

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.engine import extract_facts
from graphify.salesforce.field_definitions import FIELDS, MAX_BYTES, VALUE_TYPES


def record(**changes):
    return {"DurableId": "Contact.Account", "QualifiedApiName": "AccountId",
            "EntityDefinition": {"QualifiedApiName": "Contact"}, "NamespacePrefix": None,
            "DataType": "Lookup(Account)", "ValueTypeId": "id", "IsCalculated": False, "IsCompound": False,
            "RelationshipName": "Account", "ReferenceTo": {"referenceTo": ["Account"]},
            "ReferenceTargetField": None, "FullName": "Contact.AccountId", "Metadata": {"trackHistory": False},
            "IsPolymorphicForeignKey": False, "ControllingFieldDefinitionId": None,
            "ControllingFieldDefinition": None, **changes}


def captured(row=None, *, name="Contact.AccountId", durable="Contact.Account", kind="FieldDefinition", **changes):
    data = {"formatVersion": 1, "apiVersion": "v67.0", "kind": kind, "fullName": name, "durableId": durable,
            "record": row if row is not None else record(), **changes}
    return Source(f"salesforce-api/fieldDefinitions/{name}/definition.json", json.dumps(data, indent=2),
                  "CustomField", name, source_kind="api", salesforce_id=durable)


def catalog(kind, name):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog")


def codes(fact):
    return {d["code"] for d in fact["diagnostics"]}


def test_lookup_binds_only_real_target_and_parent_with_exact_json_evidence():
    source = captured()
    graph = build_graph([source, catalog("CustomObject", "Account"), catalog("CustomObject", "Contact")])
    edges = {e["relation"]: e for e in graph["edges"]}
    assert edges["references_object"]["target"] == node_id("CustomObject", "Account")
    assert edges["belongs_to"]["target"] == node_id("CustomObject", "Contact")
    for edge in edges.values():
        assert edge["resolution"] == "resolved" and edge["source_sha"] == source.actual_sha
        assert edge["source_location"] == edge["json_path"] and edge["json_path"].startswith("$.record.")
        assert edge["target_name"] in source.content.splitlines()[edge["line"] - 1]
    fact = extract_facts(source)
    assert fact["coverage"]["level"] == "semantic"
    assert fact["nodes"][0]["parent_relationship_name"] == "Account"
    assert fact["nodes"][0]["reference_to"] == ["Account"]
    assert not fact["diagnostics"]


@pytest.mark.parametrize("target", ["User", "Case", "pkg__Thing__c"])
def test_schema_target_never_causes_a_record_lookup_or_synthetic_declaration(target):
    source = captured(record(ReferenceTo={"referenceTo": [target]}))
    graph = build_graph([source])
    edge = next(e for e in graph["edges"] if e["relation"] == "references_object")
    assert edge["target_name"] == target and edge["resolution"] == "unresolved"
    assert not any(n["kind"] == "CustomObject" and not n["external"] for n in graph["nodes"])


@pytest.mark.parametrize("value_type", sorted(set(VALUE_TYPES) - {"address"}))
def test_primitive_schema_uses_value_type_not_display_label(value_type):
    row = record(QualifiedApiName="Id" if value_type == "id" else "Value", FullName="Contact." + ("Id" if value_type == "id" else "Value"),
                 ValueTypeId=value_type, DataType="Arbitrary display label", ReferenceTo={"referenceTo": None}, RelationshipName=None)
    fact = extract_facts(captured(row, name=row["FullName"]))
    assert fact["coverage"]["level"] == "semantic"
    assert fact["nodes"][0]["data_type"] == VALUE_TYPES[value_type]
    assert {r["relation"] for r in fact["references"]} == {"belongs_to"}


def test_missing_record_visibility_target_is_not_guessed_from_label_or_id():
    row = record(DurableId="Account.RecordVisibility", QualifiedApiName="RecordVisibilityId", FullName="Account.RecordVisibilityId",
                 EntityDefinition={"QualifiedApiName": "Account"}, ReferenceTo={"referenceTo": None}, DataType="Lookup(Record Visibility)")
    fact = extract_facts(captured(row, name=row["FullName"], durable=row["DurableId"]))
    assert fact["coverage"]["level"] == "partial"
    assert "field_definition_reference_targets_unavailable" in codes(fact)
    assert [r["target_name"] for r in fact["references"]] == ["Account"]


def test_polymorphic_targets_and_indirect_match_field_have_typed_edges():
    fact = extract_facts(captured(record(ReferenceTo={"referenceTo": ["Account", "Contact"]}, IsPolymorphicForeignKey=True,
                                         ReferenceTargetField="Account.ExternalId__c")))
    edges = [r for r in fact["references"] if r["relation"] == "references_object"]
    assert len(edges) == 2 and all(r["polymorphic"] for r in edges)
    assert any(r["target_name"] == "Account.ExternalId__c" for r in fact["references"])
    assert fact["coverage"]["level"] == "semantic"


def test_controlling_field_uses_qualified_name_not_opaque_durable_suffix():
    fact = extract_facts(captured(record(ControllingFieldDefinitionId="Contact.00N000000000001",
                                         ControllingFieldDefinition={"QualifiedApiName": "Mode__c"})))
    edge = next(r for r in fact["references"] if r["relation"] == "controlled_by")
    assert edge["target_name"] == "Contact.Mode__c"
    assert edge["json_path"] == "$.record.ControllingFieldDefinition.QualifiedApiName"


def particle():
    row = record(DurableId="Place.Coord.Lat", QualifiedApiName="Latitude", EntityDefinition={"QualifiedApiName": "Place"},
                 FieldDefinitionId="Place.Coord", FieldDefinition={"QualifiedApiName": "Location"},
                 IsComponent=True, IsNamePointing=False, IsDependentPicklist=False, DefaultValueFormula=None,
                 ValueTypeId="double", ReferenceTo={"referenceTo": None}, RelationshipName=None)
    for key in ("FullName", "Metadata", "IsPolymorphicForeignKey", "ControllingFieldDefinitionId", "ControllingFieldDefinition"):
        del row[key]
    return captured(row, name="Place.Latitude", durable="Place.Coord.Lat", kind="EntityParticle")


def test_particle_owner_is_explicit_and_independently_bound():
    source = particle()
    graph = build_graph([source, catalog("CustomObject", "Place"), catalog("CustomField", "Place.Location")])
    edge = next(e for e in graph["edges"] if e["relation"] == "component_of")
    assert edge["target"] == node_id("CustomField", "Place.Location") and edge["resolution"] == "resolved"
    assert edge["target_name"] in source.content.splitlines()[edge["line"] - 1] or "Location" in source.content.splitlines()[edge["line"] - 1]
    assert extract_facts(source)["coverage"]["level"] == "semantic"


@pytest.mark.parametrize("changes", [
    {"kind": {}}, {"kind": "Unknown"}, {"formatVersion": True}, {"formatVersion": 2}, {"apiVersion": "v66.0"},
    {"fullName": "Contact.Wrong"}, {"durableId": "Other.Account"}, {"extra": "Contact.Email"},
])
def test_capture_envelope_rejects_unsupported_or_wrong_identity(changes):
    fact = extract_facts(captured(**changes))
    assert fact["coverage"]["level"] == "partial" and not fact["references"]


@pytest.mark.parametrize("changes", [
    {"DurableId": "Other.Account"}, {"QualifiedApiName": "Wrong"}, {"EntityDefinition": None},
    {"EntityDefinition": {"QualifiedApiName": "Wrong"}}, {"NamespacePrefix": "pkg"},
    {"FullName": "Contact.Wrong"}, {"NamespacePrefix": []},
])
def test_wrong_row_identity_cannot_emit_dependencies(changes):
    fact = extract_facts(captured(record(**changes)))
    assert fact["coverage"]["level"] == "partial" and not fact["references"]


@pytest.mark.parametrize("changes", [
    {"source_kind": "source"}, {"path": "salesforce-api/fieldDefinitions/Contact.Wrong/definition.json"},
    {"path": "salesforce-api/fieldDefinitions/../definition.json"}, {"salesforce_id": None},
])
def test_wrong_source_context_fails_closed(changes):
    fact = extract_facts(replace(captured(), **changes))
    assert fact["coverage"]["level"] == "partial" and not fact["references"]


@pytest.mark.parametrize("changes,code", [
    ({"ReferenceTo": {"referenceTo": ["Account", "ACCOUNT"]}}, "field_definition_reference_targets_invalid"),
    ({"ReferenceTo": {"referenceTo": ["../User"]}}, "field_definition_reference_targets_invalid"),
    ({"ReferenceTo": {"referenceTo": [None]}}, "field_definition_reference_targets_invalid"),
    ({"ReferenceTo": {"referenceTo": "Account"}}, "field_definition_reference_targets_invalid"),
    ({"ReferenceTo": ["Account"]}, "field_definition_reference_targets_invalid"),
    ({"ReferenceTo": {"referenceTo": ["Account"], "extra": "User"}}, "field_definition_reference_targets_invalid"),
    ({"ValueTypeId": "unknown"}, "field_definition_value_type_unknown"),
    ({"ValueTypeId": None}, "field_definition_value_type_unknown"),
    ({"ValueTypeId": "string"}, "field_definition_reference_type_conflict"),
    ({"IsCompound": True}, "field_definition_compound_members_unavailable"),
    ({"IsCalculated": True}, "field_definition_calculation_unavailable"),
    ({"IsCalculated": "false"}, "field_definition_boolean_invalid"),
    ({"IsPolymorphicForeignKey": True}, "field_definition_polymorphic_targets_incomplete"),
    ({"ControllingFieldDefinitionId": "Other.Field", "ControllingFieldDefinition": {"QualifiedApiName": "Name"}}, "field_definition_controller_invalid"),
    ({"ReferenceTargetField": "Other.Id"}, "field_definition_reference_target_field_invalid"),
    ({"RelationshipName": "../Wrong"}, "field_definition_relationship_invalid"),
    ({"Metadata": {"formula": "Name + 'x'"}}, "field_definition_metadata_property_unsupported"),
    ({"Metadata": {"lookupFilter": {"field": "Contact.Email"}}}, "field_definition_metadata_property_unsupported"),
    ({"Metadata": {"trackHistory": "true"}}, "field_definition_metadata_property_unsupported"),
    ({"UnknownReference": "Other.Id"}, "field_definition_property_unsupported"),
])
def test_incomplete_semantics_stay_partial_without_hiding_known_metadata(changes, code):
    fact = extract_facts(captured(record(**changes)))
    assert fact["coverage"]["level"] == "partial" and code in codes(fact)
    assert any(r["relation"] == "belongs_to" for r in fact["references"])


@pytest.mark.parametrize("text", ["[]", "null", "{", "{\"x\":NaN}", "{\"x\":1e999}", "{\"x\":1,\"x\":2}",
                                 "[" * 100 + "0" + "]" * 100, json.dumps({"x": list(range(5000))}), " " * (MAX_BYTES + 1)])
def test_malformed_duplicate_nonfinite_deep_or_oversized_json_is_bounded(text):
    fact = extract_facts(replace(captured(), content=text))
    assert fact["coverage"]["level"] == "partial" and not fact["references"]


def test_compact_and_escaped_json_keys_retain_true_locations():
    source = captured()
    source = replace(source, content=json.dumps(json.loads(source.content)).replace('"ReferenceTo"', '"Reference\\u0054o"'))
    refs = extract_facts(source)["references"]
    assert all(r["line"] == 1 for r in refs)
    assert any(r["json_path"] == "$.record.ReferenceTo.referenceTo[0]" for r in refs)


def test_cached_lookup_facts_rebind_as_independent_targets_disappear_and_return():
    source = captured()
    initial = build_graph([source, catalog("CustomObject", "Account")], include_facts=True)
    original = deepcopy(initial["facts"])
    missing = build_graph([source], previous_facts=initial["facts"], include_facts=True)
    assert next(e for e in missing["edges"] if e["relation"] == "references_object")["resolution"] == "unresolved"
    restored = build_graph([source, catalog("CustomObject", "Account")], previous_facts=missing["facts"])
    assert next(e for e in restored["edges"] if e["relation"] == "references_object")["resolution"] == "resolved"
    assert initial["facts"] == original


def test_field_lookup_declaration_enables_relationship_path_binding():
    apex = Source("classes/Reader.cls", "class Reader { void read() { List<Contact> xs = [SELECT Account.Name FROM Contact]; } }", "ApexClass", "Reader")
    graph = build_graph([captured(), apex, catalog("CustomObject", "Contact"), catalog("CustomObject", "Account"), catalog("CustomField", "Account.Name")])
    assert any(e["target"] == node_id("CustomField", "Account.Name") and e["resolution"] == "resolved" for e in graph["edges"])


def test_package_field_without_namespace_flag_requires_effective_catalog_namespace():
    row = record(QualifiedApiName="pkg__Account__c", FullName="Contact.pkg__Account__c")
    source = captured(row, name=row["FullName"])
    assert not extract_facts(source)["references"]
    assert extract_facts(replace(source, namespace="pkg"))["coverage"]["level"] == "semantic"


@pytest.mark.parametrize("key", sorted(FIELDS["FieldDefinition"] - {"attributes"}))
def test_every_selected_field_definition_property_is_required(key):
    row = record()
    row.pop(key)
    assert extract_facts(captured(row))["coverage"]["level"] == "partial"


def test_particle_owner_from_another_object_is_not_guessed_into_local_scope():
    source = particle()
    data = json.loads(source.content)
    data["record"]["FieldDefinitionId"] = "Other.Location"
    fact = extract_facts(replace(source, content=json.dumps(data)))
    assert "field_definition_particle_owner_invalid" in codes(fact)
    assert not any(r["relation"] == "component_of" for r in fact["references"])


def test_nested_unknown_reference_and_address_members_remain_partial():
    fact = extract_facts(captured(record(EntityDefinition={"QualifiedApiName": "Contact", "Unknown": "Account.Id"})))
    assert "field_definition_property_unsupported" in codes(fact)
    fact = extract_facts(captured(record(ValueTypeId="address", ReferenceTo=None)))
    assert "field_definition_compound_members_unavailable" in codes(fact)
