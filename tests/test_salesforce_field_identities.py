"""Tooling FieldDefinition identities survive richer parent XML declarations."""
import pytest

from graphify.salesforce import Source, build_graph, node_id

FIELD_ID = "00N000000000001"
OBJECT_ID = "01I000000000001"


def catalog(kind, name, sfid=None, path=None):
    return Source(path or f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", salesforce_id=sfid)


def forecast(fid=FIELD_ID):
    return Source("settings/ForecastingObjectList.settings", f"<ForecastingObjectListSettings><forecastingTypeObjectListSettings><forecastingTypeDeveloperName>Revenue</forecastingTypeDeveloperName><forecastingObjectListSelectedSettings><field>{fid}</field></forecastingObjectListSelectedSettings></forecastingTypeObjectListSettings></ForecastingObjectListSettings>", "Settings", "ForecastingObjectList")


def field_edge(graph):
    return next(e for e in graph["edges"] if e["target_kind"] == "CustomField" and e["relation"] == "references_field")


@pytest.mark.parametrize("obj,parent", [("Opportunity", "Opportunity"), ("Departure__c", OBJECT_ID)])
def test_durable_field_id_binds_with_independently_declared_matching_parent(obj, parent):
    graph = build_graph([forecast(), catalog("ForecastingType", "Revenue"), catalog("CustomObject", obj, OBJECT_ID if obj.endswith("__c") else None), catalog("CustomField", obj + ".Score__c", parent + "." + FIELD_ID)])
    edge = field_edge(graph)
    assert edge["resolution"] == "resolved" and edge["target"] == node_id("CustomField", obj + ".Score__c")
    assert edge["target_salesforce_id"] == FIELD_ID


@pytest.mark.parametrize("field_id", [FIELD_ID, "Opportunity." + FIELD_ID])
def test_catalog_field_identity_survives_embedded_parent_declaration(field_id):
    object_source = Source("objects/Opportunity.object", "<CustomObject><fields><fullName>Score__c</fullName><type>Number</type></fields></CustomObject>", "CustomObject", "Opportunity")
    sources = [forecast(), catalog("ForecastingType", "Revenue"), object_source, catalog("CustomField", "Opportunity.Score__c", field_id)]
    for ordered in (sources, list(reversed(sources))):
        graph = build_graph(ordered)
        assert field_edge(graph)["resolution"] == "resolved"
        node = next(n for n in graph["nodes"] if n["id"] == node_id("CustomField", "Opportunity.Score__c"))
        assert node["source_file"] == object_source.path and node["coverage"] == "semantic"
        assert node["salesforce_id"] == FIELD_ID


@pytest.mark.parametrize("durable,object_id", [
    ("Case." + FIELD_ID, None),
    ("Opportunity." + FIELD_ID + ".extra", None),
    (OBJECT_ID + "." + FIELD_ID, "01I000000000002"),
    (OBJECT_ID + "." + FIELD_ID, None),
    ("opportunity." + FIELD_ID.lower(), None),
    ("Opportunity.003000000000001", None),
])
def test_malformed_wrong_parent_wrong_case_or_non_field_id_does_not_bind(durable, object_id):
    graph = build_graph([forecast(), catalog("CustomObject", "Opportunity", object_id), catalog("CustomField", "Opportunity.Score__c", durable)])
    assert field_edge(graph)["resolution"] == "unresolved"


def test_custom_object_parent_id_is_case_sensitive():
    object_id = "01Ia00000000001"
    graph = build_graph([forecast(), catalog("CustomObject", "Departure__c", object_id), catalog("CustomField", "Departure__c.Score__c", object_id.upper() + "." + FIELD_ID)])
    assert field_edge(graph)["resolution"] == "unresolved"


def test_missing_or_excluded_catalog_cannot_be_reconstructed_from_reference_id():
    graph = build_graph([forecast(), catalog("CustomObject", "Opportunity"), catalog("CustomField", "Opportunity.Score__c")])
    assert field_edge(graph)["resolution"] == "unresolved"
    assert not any(n.get("salesforce_id") == FIELD_ID for n in graph["nodes"])


def test_duplicate_field_id_is_ambiguous_not_arbitrarily_bound():
    graph = build_graph([forecast(), catalog("CustomObject", "Opportunity"),
                         catalog("CustomField", "Opportunity.First__c", "Opportunity." + FIELD_ID),
                         catalog("CustomField", "Opportunity.Second__c", "Opportunity." + FIELD_ID)])
    assert field_edge(graph)["resolution"] == "ambiguous"


def test_conflicting_same_component_ids_do_not_resolve_either_catalog_identity():
    graph = build_graph([forecast(), catalog("CustomField", "Opportunity.Score__c", FIELD_ID, "a/catalog"),
                         catalog("CustomField", "Opportunity.Score__c", "00N000000000002", "b/catalog")])
    assert field_edge(graph)["resolution"] == "unresolved"
    assert "metadata_salesforce_identity_conflict" in {d["code"] for d in graph["diagnostics"]}


def test_conflict_marks_richer_source_partial_but_does_not_poison_cached_facts():
    object_source = Source("objects/Opportunity.object", "<CustomObject><fields><fullName>Score__c</fullName><type>Number</type></fields></CustomObject>", "CustomObject", "Opportunity")
    sources = [forecast(), object_source, catalog("CustomField", "Opportunity.Score__c", FIELD_ID, "a/catalog")]
    first = build_graph([*sources, catalog("CustomField", "Opportunity.Score__c", "00N000000000002", "b/catalog")], include_facts=True)
    assert field_edge(first)["resolution"] == "unresolved"
    assert next(n for n in first["nodes"] if n["id"] == node_id("CustomField", "Opportunity.Score__c"))["coverage"] == "partial"
    second = build_graph(sources, previous_facts=first["facts"])
    assert field_edge(second)["resolution"] == "resolved"
    assert next(n for n in second["nodes"] if n["id"] == node_id("CustomField", "Opportunity.Score__c"))["coverage"] == "semantic"


def test_rebind_does_not_cache_a_removed_or_changed_parent_identity():
    fixed = [forecast(), catalog("CustomField", "Departure__c.Score__c", OBJECT_ID + "." + FIELD_ID)]
    parent = catalog("CustomObject", "Departure__c", OBJECT_ID)
    first = build_graph([*fixed, parent], include_facts=True)
    assert field_edge(first)["resolution"] == "resolved"
    second = build_graph(fixed, previous_facts=first["facts"], include_facts=True)
    assert field_edge(second)["resolution"] == "unresolved"
    assert second["stats"]["reused"] == len(fixed)
    third = build_graph([*fixed, parent], previous_facts=second["facts"])
    assert field_edge(third)["resolution"] == "resolved"


def test_declaration_filter_runs_before_id_binding_and_does_not_poison_facts():
    sources = [forecast(), catalog("CustomObject", "Opportunity"),
               catalog("CustomField", "Opportunity.pkg__Score__c", "Opportunity." + FIELD_ID),
               catalog("ForecastingType", "Revenue")]
    first = build_graph(sources, include_facts=True)
    assert field_edge(first)["resolution"] == "resolved"
    second = build_graph(sources, previous_facts=first["facts"], include_facts=True,
                         node_filter=lambda n: n["name"] != "Opportunity.pkg__Score__c")
    assert field_edge(second)["resolution"] == "unresolved"
    assert second["stats"]["reused"] == len(sources)
    assert not any(n["name"] == "Opportunity.pkg__Score__c" for n in second["nodes"])
    third = build_graph(sources, previous_facts=second["facts"])
    assert field_edge(third)["resolution"] == "resolved"


def test_filter_removes_outgoing_references_from_excluded_embedded_members():
    source = Source("objects/Opportunity.object", "<CustomObject><fields><fullName>pkg__Lookup__c</fullName><type>Lookup</type><referenceTo>Account</referenceTo></fields></CustomObject>", "CustomObject", "Opportunity")
    graph = build_graph([source, catalog("CustomObject", "Account")], node_filter=lambda n: n["name"] != "Opportunity.pkg__Lookup__c")
    assert not any(e["source"] == node_id("CustomField", "Opportunity.pkg__Lookup__c") for e in graph["edges"])
