"""Synthetic Metadata API contracts; no customer source or recipient data."""
import hashlib
import json

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.setup import STANDARD_HOME_COMPONENTS


def source(kind, body="", name="Example", **kwargs):
    return Source(f"{kind}/{name}.xml", f"<{kind}>\n{body}\n</{kind}>", kind, name, **kwargs)


def catalog(kind, name, **kwargs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **kwargs)


def level(graph, kind):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == kind)


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


def forecast(extra=""):
    return source("ForecastingType", """<developerName>Revenue</developerName><masterLabel>Revenue</masterLabel>
<active>true</active><amount>true</amount><quantity>false</quantity><hasProductFamily>false</hasProductFamily>
<dateType>0</dateType><roleType>R</roleType>""" + extra, "Revenue")


def cms_member(name="title", typ="NAMEFIELD", extra=""):
    return f"<managedContentNodeTypes><nodeName>{name}</nodeName><nodeLabel>Label</nodeLabel><nodeType>{typ}</nodeType><isRequired>true</isRequired>{extra}</managedContentNodeTypes>"


def cms(body):
    return source("ManagedContentType", "<developerName>News</developerName><masterLabel>News</masterLabel>" + body, "News")


def topic(name, parent="", mode="Navigational"):
    return f"<ManagedTopic><name>{name}</name><managedTopicType>{mode}</managedTopicType><position>0</position>{'<parentName>' + parent + '</parentName>' if parent else ''}</ManagedTopic>"


def test_home_page_custom_widgets_have_exact_kind_and_line_evidence():
    src = source("HomePageLayout", "<wideComponents>News</wideComponents>")
    graph = build_graph([src, catalog("HomePageComponent", "News"), catalog("CustomObject", "News")])
    edge, = graph["edges"]
    assert edge["target"] == node_id("HomePageComponent", "News")
    assert edge["relation"] == "displays" and edge["column"] == "wideComponents"
    assert edge["line"] == 2 and edge["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()
    assert level(graph, "HomePageLayout") == "semantic"


def test_standard_home_widgets_are_platform_literals_even_if_catalog_names_match():
    src = source("HomePageLayout", "".join(f"<narrowComponents>standard-{s}</narrowComponents>" for s in STANDARD_HOME_COMPONENTS))
    graph = build_graph([src, *[catalog("HomePageComponent", "standard-" + s) for s in STANDARD_HOME_COMPONENTS]])
    assert not graph["edges"] and level(graph, "HomePageLayout") == "semantic"


@pytest.mark.parametrize("body", ["<wideComponents>standard-Future</wideComponents>", "<wideComponents/>", "<wideComponents><name>X</name></wideComponents>", "<futureWidget>X</futureWidget>"])
def test_unknown_home_widget_contract_is_partial(body):
    graph = build_graph([source("HomePageLayout", body)])
    assert level(graph, "HomePageLayout") == "partial" and not graph["edges"]


def test_late_home_widget_inventory_rebinds_unchanged_facts_and_recovers_coverage():
    src = source("HomePageLayout", "<wideComponents>News</wideComponents>")
    first = build_graph([src], include_facts=True)
    assert level(first, "HomePageLayout") == "partial"
    second = build_graph([src, catalog("HomePageComponent", "News")], previous_facts=first["facts"])
    assert second["edges"][0]["resolution"] == "resolved" and level(second, "HomePageLayout") == "semantic"


@pytest.mark.parametrize("tag", ["iframeWhiteListUrls", "iframeWhiteListUrl"])
def test_iframe_allowlist_urls_are_not_components(tag):
    graph = build_graph([source("IframeWhiteListUrlSettings", f"<{tag}><context>VisualforcePages</context><url>https://example.test/Account.Secret__c</url></{tag}>")])
    assert not graph["edges"] and level(graph, "IframeWhiteListUrlSettings") == "semantic"


@pytest.mark.parametrize("tag,value", [("dateType", "0"), ("dateType", "1"), ("dateType", "2"), ("dateType", "OpportunityCloseDate"), ("roleType", "Y")])
def test_forecast_modes_never_guess_object_fields_or_roles(tag, value):
    src = forecast()
    old = "0" if tag == "dateType" else "R"
    src = Source(src.path, src.content.replace(f"<{tag}>{old}", f"<{tag}>{value}"), src.metadata_type, src.full_name)
    graph = build_graph([src])
    assert not graph["edges"] and level(graph, "ForecastingType") == "semantic"


def test_forecasting_explicit_model_and_split_references_are_typed():
    graph = build_graph([forecast("<territory2Model>West</territory2Model><opportunitySplitType>Revenue</opportunitySplitType>"),
                         catalog("Territory2Model", "West"), catalog("OpportunitySplitType", "Revenue"), catalog("Role", "West")])
    assert {e["target"] for e in graph["edges"]} == {node_id("Territory2Model", "West"), node_id("OpportunitySplitType", "Revenue")}
    assert level(graph, "ForecastingType") == "semantic"


@pytest.mark.parametrize("extra", ["<hasCustomGroup>true</hasCustomGroup>", "<hasCustomGroup>maybe</hasCustomGroup>", "<active>false</active>", "<future>X</future>"])
def test_forecast_unknown_or_incomplete_config_is_partial(extra):
    assert level(build_graph([forecast(extra)]), "ForecastingType") == "partial"


def test_legacy_community_is_not_network_and_disabled_chatter_slots_are_ignored():
    graph = build_graph([source("Community", "<active>true</active><enableChatterAnswers>false</enableChatterAnswers><site>Portal</site><communityFeedPage>Feed</communityFeedPage><expertsGroup>Experts</expertsGroup>"), catalog("Group", "Experts"), catalog("Network", "Example"), catalog("CustomSite", "Portal"), catalog("ApexPage", "Feed")])
    assert len(graph["edges"]) == 1 and graph["edges"][0]["target"] == node_id("Group", "Experts")
    assert level(graph, "Community") == "semantic"


def test_community_enabled_reference_slots_are_not_labels_or_urls():
    graph = build_graph([source("Community", "<enableChatterAnswers>true</enableChatterAnswers><communityFeedPage>Feed</communityFeedPage><site>Portal</site><emailHeaderDocument>Shared/Header</emailHeaderDocument><emailNotificationUrl>https://example.test/Account.Name</emailNotificationUrl>"), catalog("ApexPage", "Feed"), catalog("CustomSite", "Portal"), catalog("Document", "Shared/Header")])
    assert {e["target_kind"] for e in graph["edges"]} == {"ApexPage", "CustomSite", "Document"}
    assert level(graph, "Community") == "semantic"


def test_cms_fields_are_scoped_declarations_not_salesforce_fields_or_content_records():
    graph = build_graph([cms(cms_member() + cms_member("image", "MEDIA") + cms_member("body", "RTE", "<placeholderText>{!Account.Secret__c}</placeholderText>"))])
    members = [n for n in graph["nodes"] if n["kind"] == "ManagedContentNode"]
    assert {n["name"] for n in members} == {"News.title", "News.image", "News.body"}
    assert {e["relation"] for e in graph["edges"]} == {"contains"}
    assert all(e["resolution"] == "resolved" for e in graph["edges"])
    assert level(graph, "ManagedContentType") == "semantic"


@pytest.mark.parametrize("body", [cms_member("1bad"), cms_member() * 2, cms_member("title", "Future"), cms_member("image", "IMG", "<isLocalizable>true</isLocalizable>"), cms_member("title", "NAMEFIELD").replace("<isRequired>true", "<isRequired>false"), cms_member() * 16])
def test_invalid_cms_definition_never_claims_complete(body):
    assert level(build_graph([cms(body)]), "ManagedContentType") == "partial"


def test_managed_topics_link_exact_site_and_site_scoped_parents():
    graph = build_graph([source("ManagedTopics", topic("Travel") + topic("Flights", "Travel"), "Portal"), catalog("Network", "Portal"), catalog("ManagedTopic", "Other.Travel"), catalog("Community", "Portal")])
    assert level(graph, "ManagedTopics") == "semantic"
    assert {e["target"] for e in graph["edges"] if e["relation"] == "belongs_to"} == {node_id("Network", "Portal")}
    parent, = [e for e in graph["edges"] if e["relation"] == "parent_topic"]
    assert parent["target"] == node_id("ManagedTopic", "Portal.Travel")


def test_topic_parent_cycles_do_not_publish_a_false_hierarchy():
    graph = build_graph([source("ManagedTopics", topic("A", "B") + topic("B", "C") + topic("C", "A"), "Portal"), catalog("Network", "Portal")])
    assert not [e for e in graph["edges"] if e["relation"] == "parent_topic"]
    assert "managed_topic_parent_cycle" in codes(graph) and level(graph, "ManagedTopics") == "partial"


def test_topic_scopes_cannot_collide_through_dotted_names():
    graph = build_graph([source("ManagedTopics", topic("B.C"), "A"), source("ManagedTopics", topic("C"), "A.B")])
    assert not [n for n in graph["nodes"] if n["kind"] == "ManagedTopic"]
    assert "managed_topic_scope_encoding_unsupported" in codes(graph)


@pytest.mark.parametrize("body", [cms_member(extra="<helpText>A</helpText><helpText>B</helpText>"), cms_member().replace("<nodeName>", "unmodeled text<nodeName>")])
def test_duplicate_nested_literals_and_non_xml_container_text_stay_partial(body):
    assert level(build_graph([cms(body)]), "ManagedContentType") == "partial"


@pytest.mark.parametrize("body", [topic("Travel") * 2, topic("Travel", "Travel"), topic("Travel", "Other", "Featured"), topic("Travel", "Unknown"), "<managedTopic><name>WrongCase</name></managedTopic>"])
def test_topic_ambiguous_missing_or_wrong_case_contract_stays_partial(body):
    assert level(build_graph([source("ManagedTopics", body, "Portal"), catalog("Network", "Portal")]), "ManagedTopics") == "partial"


def test_apex_email_literals_and_user_records_do_not_leak_into_derived_facts():
    graph = build_graph([source("ApexEmailNotifications", "<apexEmailNotification><email>synthetic@example.test</email></apexEmailNotification><apexEmailNotification><user>synthetic-user@example.test</user></apexEmailNotification>")], include_facts=True)
    assert not graph["edges"] and "synthetic" not in json.dumps(graph)
    assert "metadata_user_record_not_indexed" in codes(graph) and level(graph, "ApexEmailNotifications") == "partial"


def test_branding_explicit_assets_link_but_dynamic_urls_and_archives_remain_partial():
    graph = build_graph([source("NetworkBranding", "<network>Portal</network><loginLogo>Shared/Logo</loginLogo><pageHeader>Shared/Header</pageHeader><loginRightFrameUrl>https://example.test/{expid}</loginRightFrameUrl>"), catalog("Network", "Portal"), catalog("Document", "Shared/Logo"), catalog("Document", "Shared/Header"), source("SiteDotCom", "<label>Portal</label><siteType>ChatterNetworkPicasso</siteType>")])
    assert len(graph["edges"]) == 3 and all(e["resolution"] == "resolved" for e in graph["edges"])
    assert level(graph, "NetworkBranding") == level(graph, "SiteDotCom") == "partial"


def test_call_center_references_require_exact_id_kind_not_a_same_named_record():
    body = "<displayName>Test</displayName><displayNameLabel>Test</displayNameLabel><internalNameLabel>Test</internalNameLabel><contactCenterChannels><channel>0Mj000000000001</channel><contactCenter>04v000000000001</contactCenter><voiceMailHandler>301000000000001</voiceMailHandler></contactCenterChannels>"
    graph = build_graph([source("CallCenter", body), catalog("MessagingChannel", "Chat", salesforce_id="0Mj000000000001"), catalog("CallCenter", "Center", salesforce_id="04v000000000001"), catalog("Flow", "Voicemail", salesforce_id="301000000000001"), catalog("Flow", "301000000000001")])
    assert {e["target"] for e in graph["edges"]} == {node_id("MessagingChannel", "Chat"), node_id("CallCenter", "Center"), node_id("Flow", "Voicemail")}
    assert level(graph, "CallCenter") == "semantic"


@pytest.mark.parametrize("kind,body", [("HomePageLayout", ""), ("IframeWhiteListUrlSettings", ""), ("Community", "<active>true</active>"), ("ApexEmailNotifications", "")])
def test_legitimately_empty_reference_sets_do_not_invent_edges(kind, body):
    graph = build_graph([source(kind, body)])
    assert not graph["edges"] and level(graph, kind) == "semantic"
