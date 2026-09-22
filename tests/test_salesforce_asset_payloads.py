"""CSV payloads require exact current descriptor evidence; values are not refs."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce import asset_payloads


def body(content="code,name\nAAA,Example\n", *, name="Airports", path=None, **kwargs):
    return Source(path or f"staticresources/{name}.resource", content, "StaticResource", name, **kwargs)


def descriptor(mime="text/csv", *, name="Airports", path=None, extra="", **kwargs):
    return Source(path or f"staticresources/{name}.resource-meta.xml",
                  f"<StaticResource><cacheControl>Public</cacheControl><contentType>{mime}</contentType>{extra}</StaticResource>",
                  "StaticResource", name, **kwargs)


def owner(graph, name="Airports"):
    return next(n for n in graph["nodes"] if n["id"] == node_id("StaticResource", name))


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


@pytest.mark.parametrize("mime", ["text/csv", "application/csv", " TEXT/CSV "])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_csv_requires_mime_and_retains_both_exact_hashes(mime, newline, bom):
    payload = body(bom + f'code,name{newline}AAA,"Quoted, name"{newline}')
    xml = descriptor(mime)
    graph = build_graph([payload, xml], include_facts=True)
    node = owner(graph)
    assert node["coverage"] == "semantic"
    assert {c["level"] for c in graph["coverage"]} == {"semantic"}
    assert node["content_analysis"] == "literal_csv"
    assert node["source_file"] == xml.path
    assert node["source_sha"] == hashlib.sha256(xml.content.encode()).hexdigest()
    assert node["binding_evidence"] == [{"source_file": payload.path, "source_sha": hashlib.sha256(payload.content.encode()).hexdigest(), "line": 1, "status": "captured"}]
    assert node["payload_analysis"]["rows"] == node["payload_analysis"]["columns"] == 2
    assert node["source_files"] == [xml.path, payload.path]
    assert not graph["edges"] and not graph["diagnostics"]
    # Incremental facts deliberately remain dependent on a later pairing pass.
    assert {f["coverage"]["level"] for f in graph["facts"].values()} == {"partial"}
    assert "Quoted, name" not in json.dumps(graph["facts"])


@pytest.mark.parametrize("content", [
    "", "one\ntwo\n", "one,two\n", "a,b\n1\n", "a,b\n1,2,3\n",
    'a,b\n1,"unterminated\n', 'a,b\n1,"closed"suffix\n',
    'a,b\n1,unquoted"quote\n', 'a,b\n1,\x00\n', "a,b\n\n1,2\n",
])
def test_invalid_or_unsupported_csv_stays_partial(content):
    graph = build_graph([body(content), descriptor()])
    assert owner(graph)["coverage"] == "partial"
    assert codes(graph) >= {"asset_payload_not_analyzed", "asset_payload_csv_invalid"}
    assert not graph["edges"]


@pytest.mark.parametrize("limit,value,content,code", [
    ("MAX_CSV_BYTES", 5, "a,b\n1,2\n", "asset_payload_size_limit"),
    ("MAX_CSV_ROWS", 2, "a,b\n1,2\n3,4\n", "asset_payload_csv_invalid"),
    ("MAX_CSV_COLUMNS", 1, "a,b\n1,2\n", "asset_payload_csv_invalid"),
    ("MAX_CSV_CELL_CHARS", 3, 'a,b\n1,"long"\n', "asset_payload_csv_invalid"),
])
def test_bounds_fail_closed(monkeypatch, limit, value, content, code):
    monkeypatch.setattr(asset_payloads, limit, value)
    graph = build_graph([body(content), descriptor()])
    assert owner(graph)["coverage"] == "partial" and code in codes(graph)


@pytest.mark.parametrize("mime", ["text/plain", "application/json", "application/zip", "text/csv; charset=unknown", ""])
def test_a_table_without_a_reviewed_mime_contract_is_not_complete(mime):
    graph = build_graph([body(), descriptor(mime)])
    assert owner(graph)["coverage"] == "partial"
    assert "asset_payload_not_analyzed" in codes(graph)
    assert "payload_analysis" not in owner(graph)


@pytest.mark.parametrize("wrong", [
    {"name": "Other"}, {"path": "other/Airports.resource-meta.xml"}, {"namespace": "pkg"},
])
def test_wrong_owner_path_or_namespace_cannot_prove_a_payload(wrong):
    graph = build_graph([body(), descriptor(**wrong)])
    assert owner(graph)["coverage"] == "partial"
    assert "payload_analysis" not in owner(graph)


def test_unknown_descriptor_properties_still_prevent_complete_component_coverage():
    graph = build_graph([body(), descriptor(extra="<future>Case.Status</future>")])
    assert owner(graph)["coverage"] == "partial"
    assert "metadata_xml_property_unsupported" in codes(graph)
    assert not graph["edges"]


def test_ambiguous_content_type_does_not_pick_the_first_declaration():
    graph = build_graph([body(), descriptor(extra="<contentType>application/zip</contentType>")])
    assert owner(graph)["coverage"] == "partial"
    assert "metadata_reference_ambiguous_scalar" in codes(graph)
    assert "asset_payload_not_analyzed" in codes(graph)


def test_cell_values_and_formula_like_literals_never_become_metadata_dependencies():
    payload = body('kind,value\nfield,Case.Status\ntemplate,{!Contact.Secret__c}\nformula,=SomeFunction()\nquoted,"line one\nline two ""quoted"""\n')
    graph = build_graph([payload, descriptor()])
    assert owner(graph)["coverage"] == "semantic"
    assert owner(graph)["payload_analysis"]["rows"] == 5
    assert not graph["edges"] and "Secret__c" not in json.dumps(graph)


def test_payload_change_descriptor_change_deletion_and_restore_rebind_cached_facts():
    payload, xml = body(), descriptor()
    before = build_graph([payload, xml], include_facts=True)
    saved = deepcopy(before["facts"])
    same = build_graph([replace(payload, content="", content_sha=hashlib.sha256(payload.content.encode()).hexdigest()), xml],
                       previous_facts=saved, include_facts=True)
    assert same["stats"]["reused"] == 2 and owner(same)["coverage"] == "semantic"
    for sources in ([payload], [payload, descriptor("application/zip")], [body("invalid"), xml]):
        changed = build_graph(sources, previous_facts=saved, include_facts=True)
        assert owner(changed)["coverage"] == "partial"
        restored = build_graph([payload, xml], previous_facts=changed["facts"])
        assert owner(restored)["coverage"] == "semantic"
    assert saved == before["facts"]


def test_incoming_actual_resource_usage_is_preserved_and_package_filter_still_applies():
    caller = Source("lwc/trip/trip.js", "import data from '@salesforce/resourceUrl/Airports';", "LightningComponentBundle", "trip")
    graph = build_graph([body(), descriptor(), caller], node_filter=lambda n: n["kind"] != "StaticResource")
    edge, = graph["edges"]
    assert edge["target_kind"] == "StaticResource" and edge["resolution"] == "unresolved"
    assert all(n.get("external") or n["kind"] != "StaticResource" for n in graph["nodes"])
    graph = build_graph([body(), descriptor(), caller])
    edge, = graph["edges"]
    assert edge["target"] == node_id("StaticResource", "Airports") and edge["resolution"] == "resolved"


def test_wrong_root_or_binary_source_cannot_claim_csv_coverage():
    xml = replace(descriptor(), content="<CustomObject><contentType>text/csv</contentType></CustomObject>")
    graph = build_graph([body(), xml])
    assert owner(graph)["coverage"] == "partial" and "metadata_root_type_mismatch" in codes(graph)
    graph = build_graph([body(source_kind="binary"), descriptor()])
    assert owner(graph)["coverage"] == "partial" and "binary_content_not_parsed" in codes(graph)


@pytest.mark.parametrize("duplicate", [body(), descriptor()])
def test_duplicate_source_identity_is_not_an_independent_current_proof(duplicate):
    graph = build_graph([body(), descriptor(), duplicate])
    assert owner(graph)["coverage"] == "partial"
    assert "payload_analysis" not in owner(graph)
