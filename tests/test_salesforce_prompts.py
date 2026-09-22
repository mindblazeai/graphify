"""Synthetic, independently declared Prompt dependencies; no org fixture data."""
import copy
import hashlib

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.prompts import ENUMS, MAX_VERSIONS, REQUIRED


DEFAULTS = {"body": "Message", "displayType": "DockedComposer", "masterLabel": "Welcome", "title": "Welcome", "versionNumber": "1"}


def source(extra="", **values):
    fields = {**DEFAULTS, **values}
    xml = "".join(f"<{tag}>{value}</{tag}>" for tag, value in fields.items() if value is not None)
    return Source("prompts/Welcome.prompt-meta.xml", "<Prompt>\n<promptVersions>\n" + extra + "\n" + xml + "\n</promptVersions>\n</Prompt>", "Prompt", "Welcome")


def catalog(name="Service_Console", kind="CustomApplication", **attrs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **attrs)


def coverage(graph):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == "Prompt")


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


def test_modern_application_foreign_key_has_exact_type_hash_and_source_line():
    src = source("<customApplication>Service_Console</customApplication><experience>Lightning</experience>")
    graph = build_graph([src, catalog(), catalog(kind="ConnectedApp"), catalog(kind="CustomObject")])
    edge, = graph["edges"]
    assert coverage(graph) == "semantic"
    assert edge["target"] == node_id("CustomApplication", "Service_Console")
    assert edge["resolution"] == "resolved" and edge["identity_contract"] == "prompt_app"
    assert edge["schema_contract"] == "MetadataAPI.PromptVersion.customApplication"
    assert edge["line"] == 3 and edge["source_file"] == src.path
    assert edge["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()


@pytest.mark.parametrize("experience", ["Lightning", "Site"])
def test_known_experience_literals_are_not_site_or_app_references(experience):
    graph = build_graph([source(experience=experience), catalog(experience), catalog(experience, "CustomSite")])
    assert coverage(graph) == "semantic" and not graph["edges"]


@pytest.mark.parametrize("name", ["Service_Console", "service_console", "ApplicationName", "pkg__Console", "standard__Sales"])
def test_names_bind_only_by_catalog_api_identity_including_15_character_names(name):
    graph = build_graph([source(customApplication=name), catalog(name)])
    assert graph["edges"][0]["resolution"] == "resolved" and coverage(graph) == "semantic"


@pytest.mark.parametrize("value", ["02u000000000AbC", "02u000000000AbCXYZ"])
def test_provider_id_binds_exact_case_and_type_to_independent_catalog(value):
    graph = build_graph([source(customApplication=value), catalog(salesforce_id="02u000000000AbCXYZ"),
                         catalog("Wrong_Case", salesforce_id="02u000000000abcXYZ"),
                         catalog("Wrong_Type", "ConnectedApp", salesforce_id="02u000000000AbCXYZ")])
    edge, = graph["edges"]
    assert edge["target"] == node_id("CustomApplication", "Service_Console")
    assert edge["target_name"] == "Service_Console" and coverage(graph) == "semantic"


@pytest.mark.parametrize("targets", [[], [catalog(kind="ConnectedApp")], [catalog("Other")]])
def test_missing_or_wrong_type_target_stays_unresolved_and_partial(targets):
    graph = build_graph([source(customApplication="Service_Console"), *targets])
    assert graph["edges"][0]["resolution"] == "unresolved"
    assert coverage(graph) == "partial" and "metadata_identity_unverified" in codes(graph)


def test_id_ambiguity_and_case_mismatch_cannot_close_coverage():
    src = source(customApplication="02u000000000AbC")
    for targets, resolution in [([catalog(salesforce_id="02u000000000abc")], "unresolved"),
                                ([catalog(salesforce_id="02u000000000AbC"), catalog("Other", salesforce_id="02u000000000AbC")], "ambiguous")]:
        graph = build_graph([src, *targets])
        assert graph["edges"][0]["resolution"] == resolution and coverage(graph) == "partial"


def test_scope_filter_excludes_package_app_without_losing_unresolved_evidence():
    graph = build_graph([source(customApplication="pkg__Console"), catalog("pkg__Console", namespace="pkg")],
                        node_filter=lambda n: not n.get("namespace"))
    assert coverage(graph) == "partial" and graph["edges"][0]["resolution"] == "unresolved"


@pytest.mark.parametrize("value", ["Console Display Label", "Other.App", "0", "02u-invalid", "{!Some.App}", "https://example.test", "-Console"])
def test_invalid_app_identities_are_not_guessed(value):
    graph = build_graph([source(customApplication=value), catalog(value)])
    assert not graph["edges"] and "prompt_application_identity_invalid" in codes(graph)


@pytest.mark.parametrize("legacy,namespace", [("Other", None), ("Service_Console", "pkg"), ("Other", "pkg")])
def test_conflicting_modern_and_legacy_app_names_emit_no_arbitrary_winner(legacy, namespace):
    graph = build_graph([source(customApplication="Service_Console", targetAppDeveloperName=legacy, targetAppNamespacePrefix=namespace),
                         catalog(), catalog(legacy), catalog("pkg__" + legacy)])
    assert not graph["edges"] and "prompt_application_context_conflict" in codes(graph)


def test_matching_modern_and_legacy_app_names_preserve_both_evidence_slots():
    graph = build_graph([source(customApplication="pkg__Console", targetAppDeveloperName="Console", targetAppNamespacePrefix="pkg"), catalog("pkg__Console")])
    assert coverage(graph) == "semantic" and len(graph["edges"]) == 2
    assert all(e["target"] == node_id("CustomApplication", "pkg__Console") for e in graph["edges"])


@pytest.mark.parametrize("tag", sorted(REQUIRED))
def test_required_version_values_cannot_be_omitted(tag):
    graph = build_graph([source(**{tag: None})])
    assert coverage(graph) == "partial" and "metadata_reference_value_missing" in codes(graph)


@pytest.mark.parametrize("tag", sorted(ENUMS))
def test_unknown_enum_values_stay_partial(tag):
    graph = build_graph([source(**{tag: "FutureValue"})])
    assert coverage(graph) == "partial" and "prompt_value_unsupported" in codes(graph)


@pytest.mark.parametrize("tag,value", [(tag, value) for tag, values in ENUMS.items() for value in values
                                      if value not in {"SpecificPermissions", "SpecificProfiles"}])
def test_documented_enum_values_are_literals(tag, value):
    graph = build_graph([source(**{tag: value})])
    assert coverage(graph) == "semantic" and not graph["edges"]


@pytest.mark.parametrize("tag,value", [("isPublished", "yes"), ("shouldIgnoreGlobalDelay", "TRUE"),
    ("timesToDisplay", "3.2"), ("versionNumber", "2147483648"), ("stepNumber", "-2147483649"), ("delayDays", "1e3"),
    ("startDate", "2026-02-30"), ("endDate", "20260922"), ("publishedDate", "2026-09-22+14:30"),
    ("startDate", "2026-09-22+02:60"), ("experience", "lightning")])
def test_invalid_scalar_literals_are_not_semantic(tag, value):
    graph = build_graph([source(**{tag: value})])
    assert coverage(graph) == "partial" and "prompt_value_unsupported" in codes(graph)


@pytest.mark.parametrize("tag,value", [("isPublished", "1"), ("shouldIgnoreGlobalDelay", "0"), ("shouldDisplayActionButton", "false"),
    ("timesToDisplay", "+12"), ("versionNumber", "2147483647"), ("delayDays", "-2147483648"),
    ("startDate", "2024-02-29"), ("endDate", "2026-09-22Z"), ("publishedDate", "2026-09-22+14:00"),
    ("publishedDate", "2026-09-22-04:30")])
def test_valid_scalar_lexical_forms_remain_literals(tag, value):
    graph = build_graph([source(**{tag: value})])
    assert coverage(graph) == "semantic" and not graph["edges"]


@pytest.mark.parametrize("extra", ["<customApplication></customApplication>",
    "<customApplication><name>Service_Console</name></customApplication>",
    "<customApplication>Service_Console</customApplication><customApplication>Other</customApplication>",
    "<experience></experience>", "<experience>Lightning</experience><experience>Site</experience>",
    "<futureProperty>Service_Console</futureProperty>"])
def test_empty_duplicate_nested_and_unknown_slots_stay_partial(extra):
    graph = build_graph([source(extra), catalog(), catalog("Other")])
    assert coverage(graph) == "partial" and not graph["edges"]


@pytest.mark.parametrize("tag", ["experienceContext", "publishedByUser", "referenceElementContext", "targetRecordType", "targetPageKey1", "targetPageType"])
def test_unimplemented_context_is_explicit_even_when_text_matches_catalog(tag):
    graph = build_graph([source(**{tag: "Service_Console"}), catalog(), catalog(kind="CustomSite"), catalog(kind="User")])
    assert coverage(graph) == "partial" and not graph["edges"]


def test_version_count_is_bounded_before_creating_references():
    src = Source("prompts/Large.xml", "<Prompt>" + "<promptVersions><customApplication>Service_Console</customApplication></promptVersions>" * (MAX_VERSIONS + 1) + "</Prompt>", "Prompt", "Large")
    graph = build_graph([src, catalog()])
    assert not graph["edges"] and "prompt_version_limit" in codes(graph)


def test_incremental_binding_recovers_without_mutating_cached_facts():
    src = source(customApplication="Service_Console", experience="Lightning")
    first = build_graph([src, catalog()], include_facts=True)
    saved = copy.deepcopy(first["facts"])
    missing = build_graph([src], previous_facts=first["facts"], include_facts=True)
    assert coverage(missing) == "partial" and first["facts"] == saved
    restored = build_graph([src, catalog()], previous_facts=missing["facts"])
    assert coverage(restored) == "semantic" and restored["stats"]["reused"] >= 1
    assert restored["edges"] == first["edges"]
