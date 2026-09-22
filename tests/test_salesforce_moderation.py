"""Synthetic, contract-based moderation fixtures; no customer records."""
import copy
import hashlib

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.moderation import CONTENT_SELECTORS, MAX_CRITERIA, MAX_TARGETS


def catalog(kind, name):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog")


def rule(body="", **values):
    fields = {"masterLabel": "Example rule", "action": "Block", "active": "false", **values}
    xml = "<ModerationRule>\n" + "\n".join(f"<{tag}>{value}</{tag}>" for tag, value in fields.items() if value is not None)
    return Source("moderation/Example Site.Rule.rule-meta.xml", xml + "\n" + body + "\n</ModerationRule>", "ModerationRule", "Example Site.Rule")


def entry(obj, field=None):
    return f"<entitiesAndFields><entityName>{obj}</entityName>" + (f"<fieldName>{field}</fieldName>" if field is not None else "") + "</entitiesAndFields>"


def build(src, *others, **kwargs):
    return build_graph([src, catalog("Network", "Example Site"), *others], **kwargs)


def level(graph):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == "ModerationRule")


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


@pytest.mark.parametrize("obj,field", sorted(CONTENT_SELECTORS))
def test_internal_content_selector_is_evidenced_entity_scope_not_a_custom_field(obj, field):
    src = rule(entry(obj, field))
    graph = build(src, catalog("CustomObject", obj), catalog("CustomField", obj + ".Body"),
                  catalog("CustomField", obj + ".CommentBody"), catalog("CustomField", obj + "." + field))
    selector, = [e for e in graph["edges"] if e["relation"] == "moderates_content"]
    assert selector["target"] == node_id("CustomObject", obj) and selector["resolution"] == "resolved"
    assert selector["metadata_selector"] == field
    assert selector["selector_contract"] == "MetadataAPI.ModeratedEntityField"
    assert selector["source_file"] == src.path and selector["line"] == 5
    assert selector["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()
    assert not any(e["target_kind"] in {"CustomField", "FieldPath"} for e in graph["edges"])
    assert level(graph) == "semantic"
    node = next(n for n in graph["nodes"] if n["kind"] == "ModerationRule")
    assert node["moderation_active"] is False  # Disabled is still configuration.


@pytest.mark.parametrize("obj,field", [("FeedComment", "RawBody"), ("FeedItem", "RawCommentBody"),
                                       ("Case", "RawBody"), ("FeedItem", "rawbody"), ("feeditem", "RawBody")])
def test_internal_tokens_in_unsupported_context_never_bind_lookalike_fields(obj, field):
    graph = build(rule(entry(obj, field)), catalog("CustomObject", obj), catalog("CustomField", obj + "." + field))
    assert level(graph) == "partial" and "moderation_selector_context_unsupported" in codes(graph)
    assert not any(e["relation"] == "moderates_content" or e["target_kind"] == "FieldPath" for e in graph["edges"])


def test_ordinary_field_still_requires_its_real_declaration_and_unknown_fields_stay_partial():
    src = rule(entry("FeedItem", "Title") + entry("FeedItem", "FutureContent"))
    graph = build(src, catalog("CustomObject", "FeedItem"), catalog("CustomField", "FeedItem.Title"))
    fields = {e["target_name"]: e for e in graph["edges"] if e["target_kind"] == "FieldPath"}
    assert fields["FeedItem.Title"]["target"] == node_id("CustomField", "FeedItem.Title")
    assert fields["FeedItem.FutureContent"]["resolution"] == "unresolved" and level(graph) == "partial"
    complete = build(src, catalog("CustomObject", "FeedItem"), catalog("CustomField", "FeedItem.Title"), catalog("CustomField", "FeedItem.FutureContent"))
    assert level(complete) == "semantic"


def test_multiple_criteria_and_keywords_bind_exact_independent_identities():
    body = "<userCriteria>Example Site.New</userCriteria><userCriteria>Example Site.Recent</userCriteria>" + entry("FeedItem", "RawBody").replace("</entitiesAndFields>", "<keywordList>Example Site.Words</keywordList></entitiesAndFields>")
    graph = build(rule(body), catalog("CustomObject", "FeedItem"),
                  catalog("UserCriteria", "Example Site.New"), catalog("UserCriteria", "Example Site.Recent"), catalog("KeywordList", "Example Site.Words"))
    assert level(graph) == "semantic" and all(e["resolution"] == "resolved" for e in graph["edges"])
    assert {e["target_name"] for e in graph["edges"] if e["target_kind"] == "UserCriteria"} == {"Example Site.New", "Example Site.Recent"}


@pytest.mark.parametrize("missing", ["CustomObject", "UserCriteria", "KeywordList", "Network"])
def test_missing_independent_targets_keep_coverage_partial(missing):
    body = "<userCriteria>Example Site.New</userCriteria>" + entry("FeedItem", "RawBody").replace("</entitiesAndFields>", "<keywordList>Example Site.Words</keywordList></entitiesAndFields>")
    declarations = [("CustomObject", "FeedItem"), ("UserCriteria", "Example Site.New"), ("KeywordList", "Example Site.Words"), ("Network", "Example Site")]
    graph = build_graph([rule(body), *[catalog(k, n) for k, n in declarations if k != missing]])
    assert level(graph) == "partial" and "metadata_identity_unverified" in codes(graph)


@pytest.mark.parametrize("tag,value", [("action", "Future"), ("action", "block"), ("type", "Any"),
                                       ("type", ""), ("timePeriod", "Long"), ("timePeriod", ""),
                                       ("active", "False"), ("active", "yes"),
                                       ("actionLimit", "1e2"), ("actionLimit", "2147483648"),
                                       ("notifyLimit", "-2147483649"), ("notifyLimit", ""),
                                       ("actionLimit", "9" * 1000)])
def test_invalid_typed_literals_never_claim_full_coverage(tag, value):
    graph = build(rule(**{tag: value}))
    assert level(graph) == "partial"
    assert codes(graph) & {"moderation_value_unsupported", "metadata_reference_value_missing"}


@pytest.mark.parametrize("tag", ["masterLabel", "action", "active"])
@pytest.mark.parametrize("value", [None, "", "<nested>true</nested>"])
def test_required_values_cannot_be_absent_empty_or_nested(tag, value):
    graph = build(rule(**{tag: value}))
    assert level(graph) == "partial" and "metadata_reference_value_missing" in codes(graph)


@pytest.mark.parametrize("tag,value", [("action", "Block"), ("active", "true"), ("type", "Rate"),
                                       ("timePeriod", "Short"), ("actionLimit", "1"), ("masterLabel", "Example")])
def test_duplicate_singletons_remain_partial(tag, value):
    graph = build(rule(f"<{tag}>{value}</{tag}>", **{tag: value}))
    assert level(graph) == "partial" and "metadata_reference_ambiguous_scalar" in codes(graph)


@pytest.mark.parametrize("action", ["Block", "Review", "Replace", "Flag", "FreezeAndNotify"])
@pytest.mark.parametrize("active", ["true", "false", "0", "1"])
def test_documented_action_and_boolean_literals_are_configuration_only(action, active):
    graph = build(rule(entry("FeedItem"), action=action, active=active, type="Rate", timePeriod="Short", actionLimit="+10", notifyLimit="0"), catalog("CustomObject", "FeedItem"))
    assert level(graph) == "semantic"
    assert {e["target_kind"] for e in graph["edges"]} == {"CustomObject", "Network"}
    assert not any(n["kind"] == "SalesforceUser" for n in graph["nodes"])


@pytest.mark.parametrize("body,code", [
    ("<future><field>Case.Status</field></future>", "metadata_xml_property_unsupported"),
    ("<userCriteria/>", "metadata_reference_value_missing"),
    ("<userCriteria><name>Example Site.New</name></userCriteria>", "metadata_reference_value_missing"),
    ("<entitiesAndFields><fieldName>RawBody</fieldName></entitiesAndFields>", "metadata_reference_value_missing"),
    ("<entitiesAndFields><entityName>FeedItem</entityName><entityName>Case</entityName><fieldName>RawBody</fieldName></entitiesAndFields>", "metadata_reference_ambiguous_scalar"),
    (entry("FeedItem", "RawBody") * (MAX_TARGETS + 1), "moderation_entry_limit"),
    ("<userCriteria>Example Site.New</userCriteria>" * (MAX_CRITERIA + 1), "moderation_entry_limit"),
])
def test_unsupported_shapes_and_bounded_arrays_fail_closed(body, code):
    graph = build(rule(body), catalog("CustomObject", "FeedItem"), catalog("UserCriteria", "Example Site.New"))
    assert level(graph) == "partial" and code in codes(graph)


def test_labels_and_user_messages_are_not_email_templates():
    graph = build(rule("<description>{!Case.Secret__c}</description><userMessage>Case.Secret__c %BLOCKED_KEYWORD%</userMessage>", masterLabel="Case.Secret__c"), catalog("CustomField", "Case.Secret__c"))
    assert len(graph["edges"]) == 1 and graph["edges"][0]["target_kind"] == "Network"
    assert level(graph) == "semantic"


def test_incremental_selector_binding_recovers_without_poisoning_reusable_facts():
    src = rule(entry("FeedItem", "RawBody"))
    first = build(src, catalog("CustomObject", "FeedItem"), include_facts=True)
    saved = copy.deepcopy(first["facts"])
    missing = build(src, previous_facts=first["facts"], include_facts=True)
    assert level(missing) == "partial" and missing["stats"]["parsed"] == 0
    assert first["facts"] == saved
    restored = build(src, catalog("CustomObject", "FeedItem"), previous_facts=missing["facts"])
    assert level(restored) == "semantic"
    assert first["edges"] == restored["edges"]
