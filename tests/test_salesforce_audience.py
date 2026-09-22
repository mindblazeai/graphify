"""Synthetic audience fixtures: explicit identities, scoped criteria and literals."""
import hashlib

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.audience import _valid_filter_logic


def catalog(kind, name, **kwargs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **kwargs)


def audience(body="", name="Example", container="Customer Site", default=True):
    return Source(f"audience/{name}.audience-meta.xml", f"""<Audience>
<audienceName>{name}</audienceName>
<container>{container}</container>
<formulaFilterType>AllCriteriaMatch</formulaFilterType>
<isDefaultAudience>{str(default).lower()}</isDefaultAudience>
{body}
</Audience>""", "Audience", name)


def criterion(typ, value, number=1):
    return f"<criterion><criteriaNumber>{number}</criteriaNumber><type>{typ}</type><operator>Equal</operator><criterionValue>{value}</criterionValue></criterion>"


def level(graph, name="Example"):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == "Audience" and c["full_name"] == name)


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


def test_default_audience_links_exact_site_with_original_source_evidence():
    src = audience("<criteria/>")
    graph = build_graph([src, catalog("Network", "Customer Site"), catalog("CustomSite", "Customer_Site")])
    edge, = graph["edges"]
    assert edge["target"] == node_id("Network", "Customer Site")
    assert edge["relation"] == "belongs_to" and edge["resolution"] == "resolved"
    assert edge["line"] == 3 and edge["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()
    assert level(graph) == "semantic"


def test_profile_object_field_and_custom_permission_slots_exclude_literal_values():
    entries = [criterion("Profile", "<profile>Readers</profile>", 1),
               criterion("FieldBased", "<entityType>Case</entityType><entityField>Status</entityField><fieldValue>Case.Fake__c</fieldValue>", 2),
               criterion("Permission", "<isEnabled>true</isEnabled><permissionType>Custom</permissionType><permissionName>Review</permissionName>", 3),
               criterion("Permission", "<isEnabled>false</isEnabled><permissionType>Standard</permissionType><permissionName>ManageUsers</permissionName>", 4)]
    graph = build_graph([audience("<criteria>" + "".join(entries) + "</criteria>", default=False),
                         catalog("Network", "Customer Site"), catalog("Profile", "Readers"),
                         catalog("CustomObject", "Case"), catalog("CustomField", "Case.Status"),
                         catalog("CustomField", "Case.Fake__c"), catalog("CustomPermission", "Review"),
                         catalog("CustomPermission", "ManageUsers")])
    assert {e["target_name"] for e in graph["edges"]} == {"Customer Site", "Readers", "Case", "Case.Status", "Review"}
    assert all(e["resolution"] == "resolved" for e in graph["edges"])
    assert level(graph) == "semantic"


def test_location_domain_labels_and_description_are_not_references():
    body = "<criteria>" + criterion("GeoLocation", "<city>Case.Status</city><country>USA</country>") + criterion("Domain", "<domain>https://example.test/Case.Status</domain>", 2) + "</criteria><description>{!Case.Status}</description>"
    graph = build_graph([audience(body), catalog("Network", "Customer Site"), catalog("CustomField", "Case.Status")])
    assert len(graph["edges"]) == 1 and level(graph) == "semantic"


@pytest.mark.parametrize("target,expected", [("CollaborationGroup.Group_RT2", "semantic"), ("Case.Group_RT2", "partial"), ("Group_RT2", "partial")])
def test_record_type_criterion_uses_typed_value_not_a_name_guess(target, expected):
    body = "<criteria>" + criterion("FieldBased", f"<entityType>CollaborationGroup</entityType><entityField>RecordTypeId</entityField><fieldValue>{target}</fieldValue>") + "</criteria>"
    graph = build_graph([audience(body), catalog("Network", "Customer Site"),
                         catalog("CustomObject", "CollaborationGroup"), catalog("CustomField", "CollaborationGroup.RecordTypeId"),
                         catalog("RecordType", "CollaborationGroup.Group_RT2"), catalog("RecordType", "Case.Group_RT2")])
    assert level(graph) == expected
    refs = [e for e in graph["edges"] if e["relation"] == "checks_record_type"]
    assert [e["target_name"] for e in refs] == ([target] if expected == "semantic" else [])


def test_record_type_id_requires_independent_declaration():
    body = "<criteria>" + criterion("FieldBased", "<entityType>Case</entityType><entityField>RecordTypeId</entityField><fieldValue>012000000000001</fieldValue>") + "</criteria>"
    sources = [audience(body), catalog("Network", "Customer Site"), catalog("CustomObject", "Case"), catalog("CustomField", "Case.RecordTypeId")]
    assert level(build_graph(sources)) == "partial"
    graph = build_graph([*sources, catalog("RecordType", "Case.Support", salesforce_id="012000000000001")])
    assert level(graph) == "semantic"
    assert next(e for e in graph["edges"] if e["relation"] == "checks_record_type")["target"] == node_id("RecordType", "Case.Support")


@pytest.mark.parametrize("container,expected", [("Customer Site", "resolved"), ("Other Site", "unresolved")])
def test_nested_audience_requires_matching_independent_container(container, expected):
    src = audience("<criteria>" + criterion("Audience", "<audienceDeveloperName>VIP</audienceDeveloperName>") + "</criteria>")
    graph = build_graph([src, audience("<criteria/>", name="VIP", container=container),
                         catalog("Network", "Customer Site"), catalog("Network", "Other Site")])
    edge, = [e for e in graph["edges"] if e["target_kind"] == "Audience"]
    assert edge["resolution"] == expected
    assert level(graph) == ("semantic" if expected == "resolved" else "partial")


def test_catalog_only_audience_is_not_assumed_to_share_container():
    src = audience("<criteria>" + criterion("Audience", "<audienceDeveloperName>VIP</audienceDeveloperName>") + "</criteria>")
    graph = build_graph([src, catalog("Audience", "VIP"), catalog("Network", "Customer Site")])
    assert next(e for e in graph["edges"] if e["target_kind"] == "Audience")["resolution"] == "unresolved"
    assert level(graph) == "partial"


@pytest.mark.parametrize("expression", ["1", "1 AND (2 OR 3)", "NOT 1 OR 2", "((1))", "1 and 2"])
def test_filter_logic_accepts_only_declared_criterion_numbers(expression):
    assert _valid_filter_logic(expression, {1, 2, 3})


@pytest.mark.parametrize("expression", ["", "4", "1 AND", "1 2", "(1", "1)", "()", "1=1", "1 OR Case.Status", "1;2", "1.0", "1e2", "1 AND ()", "(" * 65 + "1" + ")" * 65, "NOT " * 1024 + "1"])
def test_unverified_filter_logic_is_rejected(expression):
    assert not _valid_filter_logic(expression, {1, 2, 3})


def test_invalid_filter_logic_keeps_verified_field_links_but_marks_partial():
    src = audience("<criteria>" + criterion("Profile", "<profile>Readers</profile>") + "</criteria><formula>1 AND 2</formula>")
    src = Source(src.path, src.content.replace("AllCriteriaMatch", "CustomLogicMatches"), src.metadata_type, src.full_name)
    graph = build_graph([src, catalog("Network", "Customer Site"), catalog("Profile", "Readers")])
    assert "audience_filter_logic_unverified" in codes(graph) and level(graph) == "partial"
    assert {e["target_name"] for e in graph["edges"]} == {"Customer Site", "Readers"}


@pytest.mark.parametrize("body,code", [
    ("<futureProperty>Case.Status</futureProperty>", "metadata_xml_property_unsupported"),
    ("<container>Other Site</container>", "metadata_reference_ambiguous_scalar"),
    ("<criteria/><criteria/>", "audience_container_ambiguous"),
    ("<criteria>" + criterion("Profile", "<profile>Readers</profile><entityType>Case</entityType>") + "</criteria>", "audience_criterion_value_context_invalid"),
    ("<criteria>" + criterion("Future", "<profile>Readers</profile>") + "</criteria>", "audience_value_unsupported"),
    ("<criteria>" + criterion("Profile", "<profile>Readers</profile>") * 101 + "</criteria>", "audience_criteria_limit"),
    ("<criteria>" + criterion("Default", "") * 2 + "</criteria>", "audience_criterion_number_duplicate"),
    ("<formula>Case.Status</formula>", "audience_filter_logic_context_conflict"),
])
def test_unknown_ambiguous_or_out_of_contract_shapes_remain_partial(body, code):
    graph = build_graph([audience(body), catalog("Network", "Customer Site"), catalog("Profile", "Readers"), catalog("CustomField", "Case.Status")])
    assert level(graph) == "partial" and code in codes(graph)
    assert not any(e["target_name"] in {"Case.Status", "Readers"} for e in graph["edges"])


def test_non_default_without_criteria_is_not_complete():
    graph = build_graph([audience("<criteria/>", default=False), catalog("Network", "Customer Site")])
    assert level(graph) == "partial" and "audience_criteria_missing" in codes(graph)


def test_legacy_criteria_supported_but_mixed_api_shapes_flagged():
    item = criterion("Profile", "<profile>Readers</profile>")
    for body, expected in [(item, "semantic"), ("<criteria/>" + item, "partial")]:
        graph = build_graph([audience(body), catalog("Network", "Customer Site"), catalog("Profile", "Readers")])
        assert level(graph) == expected


@pytest.mark.parametrize("typ,kind,name", [("Report", "Report", "Public/Activity"), ("Dashboard", "Dashboard", "Public/Usage"), ("NavigationLinkSet", "NavigationMenu", "SiteMenu")])
def test_typed_targets_bind_exact_metadata_identity(typ, kind, name):
    src = audience(f"<targets><target><groupName>Case.Fake__c</groupName><priority>1</priority><targetType>{typ}</targetType><targetValue>{name}</targetValue></target></targets>")
    graph = build_graph([src, catalog("Network", "Customer Site"), catalog(kind, name)])
    edge, = [e for e in graph["edges"] if e["relation"] == "targets"]
    assert edge["target"] == node_id(kind, name) and edge["resolution"] == "resolved"
    assert level(graph) == "semantic"


def test_experience_variation_is_not_a_guessed_branding_set_or_component():
    src = audience("<targets><target><groupName>some-uuid$#$Branding</groupName><targetType>ExperienceVariation</targetType><targetValue>Brand</targetValue></target></targets>")
    graph = build_graph([src, catalog("Network", "Customer Site"), catalog("BrandingSet", "Brand")])
    assert len(graph["edges"]) == 1 and level(graph) == "partial"
    assert "audience_experience_variation_not_indexed" in codes(graph)


def test_missing_site_can_recover_and_regress_on_rebind_without_reparse():
    src = audience("<criteria/>")
    first = build_graph([src], include_facts=True)
    assert level(first) == "partial"
    second = build_graph([src, catalog("Network", "Customer Site")], previous_facts=first["facts"], include_facts=True)
    assert level(second) == "semantic" and second["stats"]["reused"] == 1
    third = build_graph([src], previous_facts=second["facts"])
    assert level(third) == "partial" and third["edges"][0]["resolution"] == "unresolved"
