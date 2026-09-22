"""Synthetic CTI contracts: no customer settings, User data, or network calls."""
import hashlib
import json
from xml.sax.saxutils import escape

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.call_centers import MAX_JSON_BYTES, MAX_SETTINGS, MAX_VALUE


def defaults(**overrides):
    return {"reqUseApi": "true", "reqSoftphoneHeight": "300", "reqSoftphoneWidth": "300",
            "reqSalesforceCompatibilityMode": "Classic_and_Lightning", **overrides}


def section(settings, name="reqGeneralInfo"):
    pairs = settings.items() if isinstance(settings, dict) else settings
    return (f"<sections><name>{name}</name><label>Settings</label>\n" +
            "\n".join(f"<items><name>{key}</name><label>Setting</label><value>{escape(value)}</value></items>"
                      for key, value in pairs) + "\n</sections>")


def source(settings=None, adapter="/apex/Phone", *, sections="", raw=None, extra="", name="OpenCTI"):
    body = ["<CallCenter>", "<displayName>Phone</displayName>", "<displayNameLabel>Name</displayNameLabel>",
            "<internalNameLabel>Internal name</internalNameLabel>"]
    if adapter is not None:
        body.append(f"<adapterUrl>{escape(adapter)}</adapterUrl>")
    if raw is not None or settings is not None:
        body.append(f"<customSettings>{escape(raw if raw is not None else json.dumps(settings))}</customSettings>")
    body += [sections, extra, "</CallCenter>"]
    return Source(f"callCenters/{name}.callCenter-meta.xml", "\n".join(body), "CallCenter", name)


def catalog(kind="ApexPage", name="Phone", **kwargs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **kwargs)


def coverage(graph):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == "CallCenter")


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


def test_mirrored_adapter_has_one_exact_page_edge_with_all_original_evidence():
    settings = defaults(reqAdapterUrl="/apex/Phone")
    src = source(settings, sections=section(settings))
    graph = build_graph([src, catalog(), catalog("CustomObject")])
    edge, = graph["edges"]
    assert edge["target"] == node_id("ApexPage", "Phone")
    assert edge["cti_role"] == "primary" and edge["cti_setting"] == "reqAdapterUrl"
    assert edge["relation"] == "references" and coverage(graph) == "semantic"
    lines = {edge["line"], *(p["line"] for p in edge["binding_evidence"])}
    assert len(lines) == 3
    assert all("/apex/Phone" in src.content.splitlines()[line - 1] for line in lines)
    assert edge["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()
    assert all(p["source_file"] == src.path and p["source_sha"] == edge["source_sha"] for p in edge["binding_evidence"])


@pytest.mark.parametrize("representation", ["json", "sections"])
def test_json_and_section_only_adapter_settings_are_supported(representation):
    settings = defaults(reqAdapterUrl="/apex/Phone")
    src = source(settings if representation == "json" else None, adapter=None,
                 sections=section(settings) if representation == "sections" else "")
    graph = build_graph([src, catalog()])
    assert coverage(graph) == "semantic" and graph["edges"][0]["resolution"] == "resolved"


def test_primary_and_standby_same_page_retain_distinct_configuration_roles():
    src = source(defaults(reqStandbyUrl="/apex/Phone", reqTimeout="5000"))
    graph = build_graph([src, catalog()])
    assert len(graph["edges"]) == 2 and coverage(graph) == "semantic"
    assert {e["cti_role"] for e in graph["edges"]} == {"primary", "standby"}
    assert len({e["id"] for e in graph["edges"]}) == 2


def test_namespaced_adapter_never_binds_same_named_customer_page():
    src = source(defaults(), adapter="/apex/vendor__Phone")
    graph = build_graph([src, catalog()])
    edge, = graph["edges"]
    assert edge["target_name"] == "vendor__Phone" and edge["resolution"] == "unresolved"
    assert coverage(graph) == "partial" and "metadata_identity_unverified" in codes(graph)


def test_catalog_changes_rebind_cached_adapter_facts_without_reparsing():
    src = source(defaults())
    missing = build_graph([src], include_facts=True)
    found = build_graph([src, catalog()], previous_facts=missing["facts"], include_facts=True)
    gone = build_graph([src], previous_facts=found["facts"])
    assert found["stats"]["reused"] == 1 and coverage(found) == "semantic"
    assert coverage(missing) == coverage(gone) == "partial"
    assert gone["edges"][0]["resolution"] == "unresolved"


@pytest.mark.parametrize("url", ["https://example.test/softphone", "https://example.test/apex/Phone?token=synthetic-private-token"])
def test_external_adapter_is_not_a_local_page_and_url_is_not_serialized(url):
    graph = build_graph([source(defaults(), adapter=url), catalog()], include_facts=True)
    assert coverage(graph) == "semantic" and not graph["edges"]
    assert "example.test" not in json.dumps(graph) and "synthetic-private-token" not in json.dumps(graph)


@pytest.mark.parametrize("url", ["/apex/Phone?recordId=001000000000001", "/apex/Phone#section", "//example.test/apex/Phone",
    "/apex/%50hone", "/apex/../Phone", "/apex/Phone/extra", "/apex/{!ConfiguredPage}", "javascript:alert(1)",
    "https://user:synthetic-private-token@example.test/phone", "https://example.test/{!Setting}",
    "https://example.test/%7B!Setting%7D", "https://example.test:99999/phone", "https://example.test/has space",
    "https://example.test\\@evil.test/phone", "https://[broken", "", "https://example.test/$dynamic"])
def test_unverified_urls_remain_partial_without_speculative_or_sensitive_edges(url):
    graph = build_graph([source(defaults(), adapter=url), catalog()], include_facts=True)
    assert coverage(graph) == "partial" and not graph["edges"]
    assert "synthetic-private-token" not in json.dumps(graph)


def test_http_is_allowed_for_classic_but_not_lightning():
    classic = source(defaults(reqSalesforceCompatibilityMode="Classic"), adapter="http://localhost:11000")
    lightning = source(defaults(), adapter="http://localhost:11000")
    assert coverage(build_graph([classic])) == "semantic"
    assert "call_center_adapter_https_required" in codes(build_graph([lightning]))


@pytest.mark.parametrize("settings", [defaults(reqAdapterUrl="/apex/Other"), defaults(reqAdapterUrl="")])
def test_conflicting_root_and_json_adapters_never_choose_a_winner(settings):
    graph = build_graph([source(settings), catalog(), catalog(name="Other")])
    assert not graph["edges"] and coverage(graph) == "partial"
    assert "call_center_setting_conflict" in codes(graph)


def test_conflicting_section_adapter_and_duplicate_items_do_not_choose_a_winner():
    for pairs in [[("reqAdapterUrl", "/apex/Other")], [("reqAdapterUrl", "/apex/Phone")] * 2]:
        graph = build_graph([source(defaults(), sections=section(pairs)), catalog(), catalog(name="Other")])
        assert not graph["edges"] and coverage(graph) == "partial"


def test_duplicate_sections_cannot_override_valid_mirrored_settings():
    graph = build_graph([source(defaults(), sections=section({"reqAdapterUrl": "/apex/Phone"}) * 2), catalog()])
    assert not graph["edges"] and "call_center_section_unverified" in codes(graph)


@pytest.mark.parametrize("raw", ["[]", "null", "true", "42", '"plain"', '{"reqUseApi":true}', '{"x":{}}',
    '{"x":[]}', '{"x":null}', '{"x":1}', '{"x":NaN}', '{"x":"first","x":"second"}',
    '{"reqAdapterUrl":"/apex/Phone","reqAdapterUrl":"/apex/Other"}', "{broken", "[" * 1000,
    " " * MAX_JSON_BYTES + "{}", json.dumps({str(i): "x" for i in range(MAX_SETTINGS + 1)}),
    json.dumps({"x": "y" * (MAX_VALUE + 1)}), json.dumps({"x" * 129: "y"})])
def test_invalid_custom_settings_remain_explicit_gaps(raw):
    graph = build_graph([source(raw=raw, adapter=None)], include_facts=True)
    assert coverage(graph) == "partial" and not graph["edges"]
    assert "call_center_custom_settings_unverified" in codes(graph)


def test_custom_settings_unknown_keys_and_values_are_not_components_or_facts():
    settings = defaults(vendorSecret="synthetic-private-token", vendorField="Account.Secret__c")
    graph = build_graph([source(settings), catalog(), catalog("CustomField", "Account.Secret__c")], include_facts=True)
    assert len(graph["edges"]) == 1 and coverage(graph) == "partial"
    assert "call_center_custom_setting_unverified" in codes(graph)
    assert "synthetic-private-token" not in json.dumps(graph) and "vendorSecret" not in json.dumps(graph)


def test_settings_in_unknown_sections_do_not_acquire_standard_semantics():
    src = source(defaults(), adapter=None, sections=section({"reqAdapterUrl": "/apex/Phone"}, name="Vendor"))
    graph = build_graph([src, catalog()])
    assert not graph["edges"] and coverage(graph) == "partial"


def test_dialing_literals_including_empty_prefix_do_not_become_field_links():
    src = source(defaults(), sections=section({"reqOutsidePrefix": "", "reqLongDistPrefix": "1", "reqInternationalPrefix": "01"}, "reqDialingOptions"))
    graph = build_graph([src, catalog()])
    assert coverage(graph) == "semantic" and len(graph["edges"]) == 1


@pytest.mark.parametrize("key,value", [("reqSoftphoneHeight", "239"), ("reqSoftphoneHeight", "2561"),
    ("reqSoftphoneWidth", "199"), ("reqSoftphoneWidth", "1921"), ("reqSoftphoneWidth", "300px"),
    ("reqSoftphoneWidth", "NaN"), ("reqSoftphoneWidth", "-1"), ("reqSoftphoneWidth", "9" * 100),
    ("reqUseApi", "1"), ("reqUseApi", "false"), ("reqSalesforceCompatibilityMode", "Future"),
    ("reqInternalName", "Other"), ("reqDisplayName", "Other"), ("reqTimeout", "-1"), ("reqTimeout", "9" * 11)])
def test_invalid_standard_settings_do_not_hide_valid_independent_page_evidence(key, value):
    graph = build_graph([source(defaults(**{key: value})), catalog()])
    assert coverage(graph) == "partial" and len(graph["edges"]) == 1


@pytest.mark.parametrize("key", ["reqUseApi", "reqSoftphoneHeight", "reqSoftphoneWidth"])
def test_required_open_cti_settings_cannot_be_omitted(key):
    settings = defaults()
    del settings[key]
    assert coverage(build_graph([source(settings), catalog()])) == "partial"


@pytest.mark.parametrize("settings", [defaults(reqStandbyUrl="/apex/Backup"), defaults(reqTimeout="5000")])
def test_standby_and_timeout_must_be_present_together(settings):
    graph = build_graph([source(settings), catalog(), catalog(name="Backup")])
    assert coverage(graph) == "partial" and "call_center_standby_timeout_incomplete" in codes(graph)


def test_canvas_settings_are_not_guessed_connected_app_names():
    graph = build_graph([source(defaults(reqCanvasApiName="VendorCanvas", reqCanvasNamespace="pkg")), catalog(), catalog("ConnectedApp", "VendorCanvas")])
    assert coverage(graph) == "partial" and len(graph["edges"]) == 1
    assert "call_center_canvas_adapter_unverified" in codes(graph)


@pytest.mark.parametrize("extra", ["<adapterUrl>/apex/Other</adapterUrl>", "<customSettings>{}</customSettings>",
    "<futureSetting>Account.Secret__c</futureSetting>", "<displayName>Other</displayName>"])
def test_malformed_xml_envelopes_cannot_claim_complete(extra):
    graph = build_graph([source(defaults(), extra=extra), catalog()])
    assert coverage(graph) == "partial"


def test_section_and_item_count_limits_are_explicit():
    for sections in [section({}, "Vendor") * (MAX_SETTINGS + 1), section([("reqUseApi", "true")] * (MAX_SETTINGS + 1))]:
        graph = build_graph([source(defaults(), sections=sections), catalog()])
        assert coverage(graph) == "partial" and "call_center_settings_limit" in codes(graph)


def test_voice_only_configuration_retains_existing_semantics_without_open_cti_requirements():
    graph = build_graph([source(adapter=None)])
    assert coverage(graph) == "semantic" and not graph["edges"]
