"""Synthetic metadata only; source names are not copied from customer orgs."""
import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.declarative import SHAPES


def source(kind, xml, name="Example", **kwargs):
    return Source(f"{kind}/{name}.xml", xml, kind, name, **kwargs)


def catalog(kind, name, **kwargs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **kwargs)


def refs(graph, kind=None):
    return [e for e in graph["edges"] if kind is None or e["target_kind"] == kind]


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


def test_path_links_picklist_step_fields_and_record_type_with_line_evidence():
    xml = """<PathAssistant>
  <entityName>Case</entityName>
  <fieldName>Status</fieldName>
  <pathAssistantSteps><fieldNames>Subject</fieldNames>
    <info>Literal {!Account.Secret__c} text is not a template.</info>
    <picklistValueName>Account.False__c</picklistValueName></pathAssistantSteps>
  <recordTypeName>Support</recordTypeName>
</PathAssistant>"""
    g = build_graph([source("PathAssistant", xml), catalog("CustomObject", "Case"),
                     catalog("CustomField", "Case.Status"), catalog("CustomField", "Case.Subject"),
                     catalog("RecordType", "Case.Support")])
    assert {e["target_name"] for e in g["edges"]} == {"Case", "Case.Status", "Case.Subject", "Case.Support"}
    assert all(e["resolution"] == "resolved" for e in g["edges"])
    assert next(e for e in g["edges"] if e["target_name"] == "Case.Status")["line"] == 3
    assert next(c for c in g["coverage"] if c["metadata_type"] == "PathAssistant")["level"] == "semantic"


@pytest.mark.parametrize("master", ["__MASTER__", "__Master__"])
def test_master_record_type_is_a_scope_not_an_invented_component(master):
    g = build_graph([source("PathAssistant", f"<PathAssistant><entityName>Case</entityName><fieldName>Status</fieldName><recordTypeName>{master}</recordTypeName></PathAssistant>")])
    assert not refs(g, "RecordType")
    assert next(n for n in g["nodes"] if not n["external"])["record_type_scope"] == "master"


@pytest.mark.parametrize("mode,name,expected", [
    ("All", "Ignored", None), ("Master", "__MASTER__", None),
    ("Custom", "Business", "Opportunity.Business"),
    ("Custom", "Opportunity.Business", "Opportunity.Business"),
])
def test_animation_context_is_not_a_blanket_record_type_reference(mode, name, expected):
    g = build_graph([source("AnimationRule", f"""<AnimationRule><sobjectType>Opportunity</sobjectType>
      <targetField>StageName</targetField><targetFieldChangeToValues>Account.False__c</targetFieldChangeToValues>
      <recordTypeContext>{mode}</recordTypeContext><recordTypeName>{name}</recordTypeName></AnimationRule>""")])
    assert [e["target_name"] for e in refs(g, "RecordType")] == ([expected] if expected else [])
    assert [e["target_name"] for e in refs(g, "FieldPath")] == ["Opportunity.StageName"]


@pytest.mark.parametrize("mode,expected", [("Future", "animation_record_type_context_unsupported"),
                                          ("Custom", "animation_record_type_context_conflict")])
def test_animation_unknown_or_conflicting_context_stays_partial(mode, expected):
    g = build_graph([source("AnimationRule", f"<AnimationRule><sobjectType>Case</sobjectType><targetField>Status</targetField><recordTypeContext>{mode}</recordTypeContext><recordTypeName>__MASTER__</recordTypeName></AnimationRule>")])
    assert expected in codes(g)
    assert g["coverage"][0]["level"] == "partial"
    assert not refs(g, "RecordType")


def test_role_parent_and_access_configuration_never_claim_effective_grants():
    g = build_graph([source("Role", """<Role><parentRole>Leadership</parentRole>
      <caseAccessLevel>None</caseAccessLevel><contactAccessLevel>Read</contactAccessLevel>
      <opportunityAccessLevel>Edit</opportunityAccessLevel><description>{!Case.Fake__c}</description></Role>"""),
      catalog("Role", "Leadership"), *[catalog("CustomObject", k) for k in ("Case", "Contact", "Opportunity")]])
    assert len(refs(g, "Role")) == 1 and refs(g, "Role")[0]["relation"] == "parent_role"
    assert len(refs(g, "CustomObject")) == 3
    assert {e["relation"] for e in g["edges"]} == {"parent_role", "configures_access"}
    assert all(e["resolution"] == "resolved" for e in g["edges"])
    assert not refs(g, "FieldPath")


def test_top_role_can_genuinely_have_no_outgoing_references():
    g = build_graph([source("Role", "<Role><name>Leadership</name></Role>")])
    assert not g["edges"] and g["coverage"][0]["level"] == "semantic"


def test_queue_typed_membership_keeps_each_subordinate_scope_and_routing():
    g = build_graph([source("Queue", """<Queue><queueMembers>
      <roles><role>Support</role></roles>
      <publicGroups><publicGroup>Helpers</publicGroup></publicGroups>
      <roleAndSubordinates><roleAndSubordinate>Support</roleAndSubordinate></roleAndSubordinates>
      <roleAndSubordinatesInternal><roleAndSubordinateInternal>Support</roleAndSubordinateInternal></roleAndSubordinatesInternal>
      </queueMembers><queueRoutingConfig>Fast</queueRoutingConfig>
      <queueSobject><sobjectType>Case</sobjectType></queueSobject>
      <name>NotAComponent</name><email>not-a-component@example.test</email></Queue>"""),
      catalog("Role", "Support"), catalog("Group", "Helpers"), catalog("QueueRoutingConfig", "Fast"), catalog("CustomObject", "Case")])
    assert {e["membership_scope"] for e in refs(g, "Role")} == {"direct", "all_subordinates", "internal_subordinates"}
    assert len(g["edges"]) == 6 and all(e["resolution"] == "resolved" for e in g["edges"])


def test_queue_username_is_an_explicit_out_of_catalog_reference_not_a_user_object():
    g = build_graph([source("Queue", "<Queue><queueMembers><users><user>member@example.test</user></users></queueMembers></Queue>"),
                     catalog("CustomObject", "User")])
    e = refs(g, "SalesforceUser")[0]
    assert e["target_name"] == "member@example.test" and e["resolution"] == "unresolved"
    assert "metadata_user_record_not_indexed" in codes(g)
    assert next(c for c in g["coverage"] if c["metadata_type"] == "Queue")["level"] == "partial"


@pytest.mark.parametrize("kind", ["ProfilePasswordPolicy", "ProfileSessionSetting"])
def test_profile_policy_binds_exact_case_insensitive_catalog_name_not_label_guess(kind):
    g = build_graph([source(kind, f"<{kind}><profile>admin</profile></{kind}>"), catalog("Profile", "Admin"), catalog("Profile", "System Administrator")])
    assert len(g["edges"]) == 1
    assert g["edges"][0]["target"] == node_id("Profile", "Admin")


def test_disabled_topics_setting_still_references_its_actual_object():
    g = build_graph([source("TopicsForObjects", "<TopicsForObjects><enableTopics>false</enableTopics><entityApiName>Case</entityApiName></TopicsForObjects>"), catalog("CustomObject", "Case")])
    assert len(g["edges"]) == 1 and g["edges"][0]["resolution"] == "resolved"


@pytest.mark.parametrize("value", ["Priority", "00N000000000001", "00N000000000001AAA"])
def test_service_channel_field_name_or_exact_id_binds_with_object_and_type_checks(value):
    g = build_graph([source("ServiceChannel", f"<ServiceChannel><relatedEntityType>Case</relatedEntityType><secondaryRoutingPriorityField>{value}</secondaryRoutingPriorityField></ServiceChannel>"),
                     catalog("CustomObject", "Case"), catalog("CustomField", "Case.Priority", salesforce_id="00N000000000001AAA")])
    assert refs(g, "FieldPath")[0]["target"] == node_id("CustomField", "Case.Priority")
    assert refs(g, "FieldPath")[0]["resolution"] == "resolved"


@pytest.mark.parametrize("kind,name,id", [
    ("CustomField", "Account.Priority", "00N000000000001AAA"),
    ("ApexClass", "Case.Priority", "00N000000000001AAA"),
    ("CustomField", "Case.Priority", "00n000000000001AAA"),
])
def test_service_channel_does_not_bind_wrong_object_type_or_id_case(kind, name, id):
    g = build_graph([source("ServiceChannel", "<ServiceChannel><relatedEntityType>Case</relatedEntityType><secondaryRoutingPriorityField>00N000000000001AAA</secondaryRoutingPriorityField></ServiceChannel>"),
                     catalog("CustomObject", "Case"), catalog(kind, name, salesforce_id=id)])
    assert refs(g, "FieldPath")[0]["resolution"] == "unresolved"


def test_fifteen_letter_standard_field_name_is_not_assumed_to_be_an_id():
    name = "PriorityFieldAB"
    assert len(name) == 15
    g = build_graph([source("ServiceChannel", f"<ServiceChannel><relatedEntityType>Case</relatedEntityType><secondaryRoutingPriorityField>{name}</secondaryRoutingPriorityField></ServiceChannel>"),
                     catalog("CustomObject", "Case"), catalog("CustomField", f"Case.{name}")])
    assert refs(g, "FieldPath")[0]["resolution"] == "resolved"


def test_service_channel_conflicting_name_and_id_remains_ambiguous():
    value = "PriorityFieldAB"
    g = build_graph([source("ServiceChannel", f"<ServiceChannel><relatedEntityType>Case</relatedEntityType><secondaryRoutingPriorityField>{value}</secondaryRoutingPriorityField></ServiceChannel>"),
                     catalog("CustomObject", "Case"), catalog("CustomField", f"Case.{value}"),
                     catalog("CustomField", "Case.Other__c", salesforce_id=value)])
    edge = refs(g, "FieldPath")[0]
    assert edge["resolution"] == "ambiguous"
    target = next(n for n in g["nodes"] if n["id"] == edge["target"])
    assert set(target["candidates"]) == {node_id("CustomField", "Case.Other__c"), node_id("CustomField", f"Case.{value}")}


def test_service_channel_does_not_guess_console_component_type_or_priority_values():
    g = build_graph([source("ServiceChannel", """<ServiceChannel><relatedEntityType>Case</relatedEntityType>
      <interactionComponent>Widget</interactionComponent><statusField>Status</statusField>
      <serviceChannelFieldPriorities><priority>1</priority><value>Account.False__c</value></serviceChannelFieldPriorities></ServiceChannel>"""),
                     catalog("ApexPage", "Widget"), catalog("LightningComponentBundle", "Widget")])
    assert "service_channel_console_component_unresolved" in codes(g)
    assert refs(g, "SalesforceConsoleComponent")[0]["resolution"] == "unresolved"
    assert [e["target_name"] for e in refs(g, "FieldPath")] == ["Case.Status"]


@pytest.mark.parametrize("kind", ["LeadConvertSettings", "Settings"])
def test_lead_mapping_retains_correct_object_pairs_reads_writes_and_member_ownership(kind):
    xml = """<LeadConvertSettings><objectMapping><inputObject>Lead</inputObject>
      <mappingFields><inputField>Source__c</inputField><outputField>Destination__c</outputField></mappingFields><outputObject>Account</outputObject>
      </objectMapping><objectMapping><inputObject>Lead</inputObject>
      <mappingFields><inputField>Other__c</inputField><outputField>Destination__c</outputField></mappingFields><outputObject>Contact</outputObject>
      </objectMapping></LeadConvertSettings>"""
    g = build_graph([source(kind, xml), *[catalog("CustomObject", k) for k in ("Lead", "Account", "Contact")],
                     *[catalog("CustomField", f) for f in ("Lead.Source__c", "Lead.Other__c", "Account.Destination__c", "Contact.Destination__c")]])
    pairs = [n for n in g["nodes"] if n["kind"] == "LeadConversionMapping"]
    assert len(pairs) == 2
    for pair in pairs:
        linked = {e["relation"]: e["target_name"] for e in g["edges"] if e["source"] == pair["id"]}
        assert linked in [{"reads": "Lead.Source__c", "writes": "Account.Destination__c"},
                          {"reads": "Lead.Other__c", "writes": "Contact.Destination__c"}]
        assert pair["component_id"] == node_id(kind, "Example")
    assert all(e["resolution"] == "resolved" for e in g["edges"])


def test_moderation_declared_fields_and_criteria_keep_api_only_field_gap_explicit():
    g = build_graph([source("ModerationRule", """<ModerationRule><userCriteria>Site.NewMembers</userCriteria>
      <entitiesAndFields><entityName>FeedItem</entityName><fieldName>RawBody</fieldName><keywordList>Site.Words</keywordList></entitiesAndFields>
      <entitiesAndFields><entityName>FeedComment</entityName><fieldName>CommentBody</fieldName></entitiesAndFields>
      <userMessage>{!Case.NotATemplate__c}</userMessage></ModerationRule>""", name="Site.Rule"),
      catalog("CustomObject", "FeedItem"), catalog("CustomObject", "FeedComment"), catalog("CustomField", "FeedItem.Body"),
      catalog("CustomField", "FeedComment.CommentBody"), catalog("UserCriteria", "Site.NewMembers"), catalog("KeywordList", "Site.Words")])
    assert {e["target_name"] for e in refs(g, "FieldPath")} == {"FeedItem.RawBody", "FeedComment.CommentBody"}
    assert next(e for e in refs(g, "FieldPath") if e["target_name"] == "FeedItem.RawBody")["resolution"] == "unresolved"
    assert "moderation_metadata_only_field" in codes(g)
    assert refs(g, "Network")[0]["target_name"] == "Site"
    assert refs(g, "Network")[0]["resolution"] == "unresolved"


@pytest.mark.parametrize("kind", sorted(SHAPES))
def test_unknown_reference_shaped_properties_are_partial_not_guessed(kind):
    g = build_graph([source(kind, f"<{kind}><future><field>Account.Secret__c</field></future></{kind}>")])
    assert "metadata_xml_property_unsupported" in codes(g)
    assert g["coverage"][0]["level"] == "partial"
    # ManagedTopics' own file identity supplies its site, independently of XML.
    assert all(kind == "ManagedTopics" and e["target_kind"] == "Network" and e["relation"] == "belongs_to" for e in g["edges"])


@pytest.mark.parametrize("xml", [
    "<PathAssistant><fieldName>Status</fieldName><recordTypeName>__MASTER__</recordTypeName></PathAssistant>",
    "<PathAssistant><entityName>Case</entityName><entityName>Lead</entityName><fieldName>Status</fieldName><recordTypeName>__MASTER__</recordTypeName></PathAssistant>",
    "<Role><parentRole>Other</parentRole></Role>",
])
def test_absent_duplicate_or_wrong_root_context_cannot_invent_a_field_binding(xml):
    g = build_graph([source("PathAssistant", xml)])
    assert g["coverage"][0]["level"] == "partial"
    assert not g["edges"]


def test_incremental_rebind_drops_a_removed_target_without_reparsing():
    src = source("PathAssistant", "<PathAssistant><entityName>Case</entityName><fieldName>Status</fieldName><recordTypeName>__MASTER__</recordTypeName></PathAssistant>")
    first = build_graph([src, catalog("CustomObject", "Case"), catalog("CustomField", "Case.Status")], include_facts=True)
    next_graph = build_graph([src, catalog("CustomObject", "Case")], previous_facts=first["facts"])
    assert next_graph["stats"]["reused"] == 2
    assert refs(next_graph, "FieldPath")[0]["resolution"] == "unresolved"
