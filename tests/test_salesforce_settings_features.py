"""Nested Settings contracts, independent declarations and source evidence."""
import hashlib

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.settings_features import CASE_TEMPLATES, EMAIL_FLAGS


def source(root, body):
    prefix = {"CompanySettings": "<enableCustomFiscalYear>false</enableCustomFiscalYear>",
              "FileUploadAndDownloadSecuritySettings": "<noHtmlUploadAsAttachment>false</noHtmlUploadAsAttachment>"}.get(root, "")
    return Source(f"settings/{root}.settings", f"<{root}>\n{prefix}{body}\n</{root}>", "Settings", root.removesuffix("Settings"))


def catalog(kind, name, **kwargs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **kwargs)


def level(graph):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == "Settings")


@pytest.mark.parametrize("root,body", [
    ("CompanySettings", "<fiscalYear><fiscalYearNameBasedOn>endingMonth</fiscalYearNameBasedOn><startMonth>January</startMonth></fiscalYear>"),
    ("MyDomainSettings", "<myDomainName>example-customer</myDomainName>"),
    ("EmployeeUserSettings", "<emailEncoding>UTF-8</emailEncoding><usernameSuffix>Account.Secret__c</usernameSuffix>"),
    ("FileUploadAndDownloadSecuritySettings", "<dispositions><fileType>PDF</fileType><behavior>HYBRID</behavior><securityRiskFileType>false</securityRiskFileType></dispositions>"),
    ("CaseSettings", "<caseFeedItemSettings><characterLimit>400</characterLimit><displayFormat>Default</displayFormat><feedItemType>EmailMessageEvent</feedItemType></caseFeedItemSettings><emailToCase><enableEmailToCase>false</enableEmailToCase></emailToCase><webToCase><enableWebToCase>false</enableWebToCase></webToCase>"),
])
def test_reviewed_nested_literal_contracts_do_not_invent_edges(root, body):
    graph = build_graph([source(root, body)])
    assert level(graph) == "semantic" and not graph["edges"]


@pytest.mark.parametrize("root,body,target_kind,target_name", [
    ("EmployeeUserSettings", "<profile>Employee</profile>", "Profile", "Employee"),
    ("EmployeeUserSettings", "<permset>Staff</permset>", "PermissionSet", "Staff"),
    ("RealTimeEventSettings", "<realTimeEvents><entityName>LoginEventStream</entityName><isEnabled>false</isEnabled></realTimeEvents>", "CustomObject", "LoginEventStream"),
    ("CaseSettings", "<emailActionDefaultsHandlerClass>Handler</emailActionDefaultsHandlerClass>", "ApexClass", "Handler"),
    ("CaseSettings", "<defaultCaseOwnerType>Queue</defaultCaseOwnerType><defaultCaseOwner>Support</defaultCaseOwner>", "Queue", "Support"),
    ("CaseSettings", "<webToCase><defaultResponseTemplate>public/Reply</defaultResponseTemplate></webToCase>", "EmailTemplate", "public/Reply"),
    ("CaseSettings", "<webToCase><caseOrigin>Account.Secret__c</caseOrigin></webToCase>", "CustomField", "Case.Origin"),
    *[("CaseSettings", f"<emailToCase><routingAddresses><{tag}>{value}</{tag}></routingAddresses></emailToCase>", kind, name)
      for tag, value, kind, name in [("fallbackQueue", "Support", "Queue", "Support"), ("routingFlow", "RouteCase", "Flow", "RouteCase"),
                                     ("newEntityRecordType", "Support", "RecordType", "Case.Support"),
                                     ("caseOrigin", "Email", "CustomField", "Case.Origin"), ("casePriority", "High", "CustomField", "Case.Priority"),
                                     ("taskStatus", "Open", "CustomField", "Task.Status")]],
    *[("CaseSettings", f"<{tag}>public/Reply</{tag}>", "EmailTemplate", "public/Reply") for tag in CASE_TEMPLATES],
])
def test_settings_dependency_slots_are_typed_and_keep_evidence(root, body, target_kind, target_name):
    src = source(root, body)
    graph = build_graph([src, catalog(target_kind, target_name), catalog("CustomField", "Account.Secret__c")])
    edge, = graph["edges"]
    assert edge["target"] == node_id(target_kind, target_name) and edge["resolution"] == "resolved"
    assert edge["line"] == 2 and edge["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()
    assert level(graph) == "semantic"
    missing = build_graph([src])
    assert missing["edges"][0]["resolution"] == "unresolved" and level(missing) == "partial"


@pytest.mark.parametrize("tag", EMAIL_FLAGS)
def test_all_known_email_to_case_booleans_are_validated(tag):
    good = source("CaseSettings", f"<emailToCase><{tag}>true</{tag}></emailToCase>")
    assert level(build_graph([good])) == "semantic"
    for text in ("yes", "", "<nested>true</nested>"):
        bad = source("CaseSettings", f"<emailToCase><{tag}>{text}</{tag}></emailToCase>")
        assert level(build_graph([bad])) == "partial"


@pytest.mark.parametrize("root,body", [
    ("CompanySettings", "<fiscalYear><startMonth>Account.Name</startMonth></fiscalYear>"),
    ("CompanySettings", "<fiscalYear/><fiscalYear/>"),
    ("EmployeeUserSettings", "<emailEncoding>future</emailEncoding>"),
    ("EmployeeUserSettings", "<profile>A</profile><profile>B</profile>"),
    ("MyDomainSettings", "<myDomainName>https://other.example</myDomainName>"),
    ("FileUploadAndDownloadSecuritySettings", "<dispositions><fileType>Account.Name</fileType><behavior>DOWNLOAD</behavior><securityRiskFileType>false</securityRiskFileType></dispositions>"),
    ("FileUploadAndDownloadSecuritySettings", "<dispositions><behavior>DOWNLOAD</behavior></dispositions>"),
    ("RealTimeEventSettings", "<realTimeEvents><isEnabled>false</isEnabled></realTimeEvents>"),
    ("CaseSettings", "<defaultCaseOwner>Support</defaultCaseOwner>"),
    ("CaseSettings", "<defaultCaseOwnerType>Role</defaultCaseOwnerType><defaultCaseOwner>Support</defaultCaseOwner>"),
    ("CaseSettings", "<caseFeedItemSettings><feedItemType>UnverifiedFutureType</feedItemType></caseFeedItemSettings>"),
    ("CaseSettings", "<emailToCase/><emailToCase/>"),
    ("CaseSettings", "<emailToCase><unauthorizedSenderAction>Requeue</unauthorizedSenderAction></emailToCase>"),
    ("CaseSettings", "<webToCase><futureTemplate>public/Reply</futureTemplate></webToCase>"),
])
def test_new_or_malformed_nested_contracts_stay_partial(root, body):
    graph = build_graph([source(root, body)])
    assert level(graph) == "partial" and not graph["edges"]


def test_case_user_records_are_not_reinterpreted_as_queues_or_metadata():
    graph = build_graph([source("CaseSettings", "<defaultCaseOwnerType>User</defaultCaseOwnerType><defaultCaseOwner>Support</defaultCaseOwner>"), catalog("Queue", "Support")])
    edge, = graph["edges"]
    assert edge["target_kind"] == "SalesforceUser" and edge["resolution"] == "unresolved"
    assert level(graph) == "partial" and "metadata_user_record_not_indexed" in {d["code"] for d in graph["diagnostics"]}


def test_routing_record_type_id_binds_independent_case_identity():
    src = source("CaseSettings", "<emailToCase><routingAddresses><newEntityRecordType>012000000000001</newEntityRecordType></routingAddresses></emailToCase>")
    graph = build_graph([src, catalog("CustomObject", "Case"), catalog("RecordType", "Case.Support", salesforce_id="012000000000001")])
    assert graph["edges"][0]["target"] == node_id("RecordType", "Case.Support") and level(graph) == "semantic"
    for wrong in ("Account.Support", "Unqualified"):
        invalid = build_graph([src, catalog("CustomObject", "Case"), catalog("RecordType", wrong, salesforce_id="012000000000001")])
        assert invalid["edges"][0]["resolution"] == "unresolved" and level(invalid) == "partial"
