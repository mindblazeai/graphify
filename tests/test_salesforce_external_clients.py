"""Synthetic fixtures only. Credentials must never enter derived graph facts."""
import hashlib
import json
from xml.sax.saxutils import escape

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.external_clients import OAUTH_SCOPES


def source(kind, body="", name="ClientSettings", **kwargs):
    return Source(f"{kind}/{name}.xml", f"<{kind}>\n{body}\n</{kind}>", kind, name, **kwargs)


def catalog(kind, name, **kwargs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **kwargs)


def config(kind="ExtlClntAppOauthSettings", extra="", app="Client"):
    return source(kind, f"<externalClientApplication>{app}</externalClientApplication>\n" + extra)


def edges(graph, kind=None):
    return [e for e in graph["edges"] if kind is None or e["target_kind"] == kind]


def level(graph, kind="ExtlClntAppOauthSettings"):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == kind)


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


@pytest.mark.parametrize("kind,extra", [
    ("ExtlClntAppConfigurablePolicies", "<isEnabled>false</isEnabled><startPage>None</startPage>"),
    ("ExtlClntAppGlobalOauthSettings", "<isConsumerSecretOptional>false</isConsumerSecretOptional>"),
    ("ExtlClntAppOauthConfigurablePolicies", "<permittedUsersPolicyType>AllSelfAuthorized</permittedUsersPolicyType>"),
    ("ExtlClntAppOauthSettings", "<isFirstPartyAppEnabled>false</isFirstPartyAppEnabled>"),
])
def test_each_settings_file_links_exact_parent_with_source_evidence_even_when_disabled(kind, extra):
    src = config(kind, extra)
    graph = build_graph([src, catalog("ExternalClientApplication", "Client"), catalog("ConnectedApp", "Client")])
    assert len(edges(graph)) == 1
    edge = edges(graph)[0]
    assert edge["target"] == node_id("ExternalClientApplication", "Client")
    assert edge["relation"] == "configures" and edge["resolution"] == "resolved"
    assert edge["line"] == 2 and edge["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()
    assert level(graph, kind) == "semantic"


def test_external_application_literals_do_not_invent_dependencies():
    src = source("ExternalClientApplication", """<label>Client</label><distributionState>Local</distributionState>
<contactEmail>example@example.test</contactEmail><contactPhone>1-555-0100</contactPhone>
<description>{!Case.Secret__c}</description><logoUrl>https://example.test/Account</logoUrl>
<isProtected>false</isProtected><orgScopedExternalApp>00D000000000001:Client</orgScopedExternalApp>""", "Client")
    graph = build_graph([src])
    assert not edges(graph) and level(graph, src.metadata_type) == "semantic"


@pytest.mark.parametrize("body,code", [
    ("<distributionState>Local</distributionState>", "metadata_reference_value_missing"),
    ("<label>Client</label><distributionState>Future</distributionState>", "external_client_value_unsupported"),
    ("<label>Client</label><managedType>Internal</managedType>", "external_client_internal_property_unverified"),
    ("<label>Client</label><iconUrl>https://example.test</iconUrl>", "external_client_internal_property_unverified"),
    ("<label>Client</label><futureReference>Case.Secret__c</futureReference>", "metadata_xml_property_unsupported"),
])
def test_external_application_missing_or_unverified_contract_stays_partial(body, code):
    graph = build_graph([source("ExternalClientApplication", body)])
    assert level(graph, "ExternalClientApplication") == "partial" and code in codes(graph)


def test_all_standard_oauth_scopes_are_literal_not_profiles_fields_permissions_or_apps():
    graph = build_graph([config(extra="<commaSeparatedOauthScopes>" + ", ".join(sorted(OAUTH_SCOPES)) + "</commaSeparatedOauthScopes>"),
                         catalog("ExternalClientApplication", "Client"), catalog("Profile", "Profile"),
                         catalog("CustomPermission", "CustomPermissions"), catalog("CustomApplication", "Web")])
    assert len(edges(graph)) == 1 and level(graph) == "semantic"


@pytest.mark.parametrize("extra,code", [
    ("<commaSeparatedOauthScopes>Api,Future</commaSeparatedOauthScopes>", "external_client_oauth_scope_unsupported"),
    ("<commaSeparatedOauthScopes>Api,</commaSeparatedOauthScopes>", "external_client_list_invalid"),
    ("<commaSeparatedOauthScopes>" + ",".join(["Api"] * 129) + "</commaSeparatedOauthScopes>", "external_client_list_limit"),
    ("<isFirstPartyAppEnabled>maybe</isFirstPartyAppEnabled>", "external_client_value_unsupported"),
    ("<label>One</label><label>Two</label>", "metadata_reference_ambiguous_scalar"),
    ("<trustedIpRanges><description>Range</description><startIpAddress>192.0.2.1</startIpAddress></trustedIpRanges>", "metadata_reference_value_missing"),
    ("<customAttributes><key>name</key><futureReference>Account.Name</futureReference></customAttributes>", "metadata_xml_property_unsupported"),
])
def test_literal_or_nested_contract_errors_cannot_report_complete(extra, code):
    graph = build_graph([config(extra=extra), catalog("ExternalClientApplication", "Client")])
    assert level(graph) == "partial" and code in codes(graph)
    assert len(edges(graph)) == 1


def test_credentials_pem_and_opaque_assertion_bytes_never_enter_graph_or_fact_cache():
    secrets = ["FAKE-CONSUMER-KEY", "FAKE-CONSUMER-SECRET", "FAKE-CERTIFICATE-BYTES", "FAKE-ASSERTION-BYTES"]
    global_src = config("ExtlClntAppGlobalOauthSettings", f"""<consumerKey>{secrets[0]}</consumerKey>
<consumerSecret>{secrets[1]}</consumerSecret><certificate>{secrets[2]}</certificate>
<idTokenConfig><idTokenAudience>Account.Name</idTokenAudience><idTokenIncludeAttributes>true</idTokenIncludeAttributes><idTokenValidityInMinutes>2</idTokenValidityInMinutes></idTokenConfig>""")
    oauth_src = config(extra=f"<clientAssertionCertificate>{secrets[3]}</clientAssertionCertificate>")
    graph = build_graph([global_src, oauth_src, catalog("ExternalClientApplication", "Client")], include_facts=True)
    serialized = json.dumps(graph)
    assert all(secret not in serialized for secret in secrets)
    assert not edges(graph, "Certificate") and not edges(graph, "CustomField")
    assert level(graph, global_src.metadata_type) == "semantic"
    assert level(graph) == "partial" and "external_client_assertion_certificate_unverified" in codes(graph)


@pytest.mark.parametrize("kind", ["ExtlClntAppOauthSettings", "ExtlClntAppOauthConfigurablePolicies"])
def test_custom_attribute_field_selectors_link_only_independent_fields(kind):
    src = config(kind, """<customAttributes><key>country</key><formula>User.Country</formula></customAttributes>
<customAttributes><key>orgCountry</key><formula>Organization.Country</formula></customAttributes>""")
    graph = build_graph([src, catalog("ExternalClientApplication", "Client"), catalog("CustomField", "User.Country"), catalog("CustomField", "Organization.Country")])
    assert {e["target_name"] for e in edges(graph, "FieldPath")} == {"User.Country", "Organization.Country"}
    assert all(e["resolution"] == "resolved" for e in edges(graph))
    assert level(graph, kind) == "semantic"


@pytest.mark.parametrize("formula", ["Country", "$User.Country", "'User.Country'", "IF(true, User.Country, '')", "User.Country + 'x'", "User." + "X" * 1024])
def test_unverified_attribute_expression_is_not_partially_tokenized(formula):
    graph = build_graph([config(extra=f"<customAttributes><key>country</key><formula>{escape(formula)}</formula></customAttributes>"),
                         catalog("ExternalClientApplication", "Client"), catalog("CustomField", "User.Country")])
    assert not edges(graph, "FieldPath") and level(graph) == "partial"
    assert "external_client_attribute_expression_unsupported" in codes(graph)


def test_ambiguous_attribute_key_does_not_merge_sibling_fields():
    graph = build_graph([config(extra="""<customAttributes><key>same</key><formula>User.Country</formula></customAttributes>
<customAttributes><key>same</key><formula>Organization.Country</formula></customAttributes>"""), catalog("ExternalClientApplication", "Client")])
    assert not edges(graph, "FieldPath") and "external_client_attribute_key_ambiguous" in codes(graph)


@pytest.mark.parametrize("selector", ["0PS000000000001", "0PS000000000001AAA"])
def test_permission_selectors_use_exact_case_sensitive_id_not_a_same_named_component(selector):
    src = config("ExtlClntAppOauthConfigurablePolicies", f"<commaSeparatedPermissionSet>{selector}</commaSeparatedPermissionSet>")
    graph = build_graph([src, catalog("ExternalClientApplication", "Client"),
                         catalog("PermissionSet", "Approved", salesforce_id="0PS000000000001AAA"),
                         catalog("PermissionSet", selector), catalog("Profile", "OtherKind", salesforce_id=selector)])
    assert edges(graph, "PermissionSet")[0]["target"] == node_id("PermissionSet", "Approved")
    assert level(graph, src.metadata_type) == "semantic"
    missing = build_graph([src, catalog("ExternalClientApplication", "Client"),
                           catalog("PermissionSet", selector), catalog("PermissionSet", "WrongCase", salesforce_id="0Ps000000000001AAA")])
    assert edges(missing, "PermissionSet")[0]["resolution"] == "unresolved"
    assert level(missing, src.metadata_type) == "partial"


def test_handler_custom_scopes_and_certificate_are_type_scoped():
    policies = config("ExtlClntAppOauthConfigurablePolicies", "<apexHandler>Handler</apexHandler><commaSeparatedCustomScopes>ReadOrders, ReadOrders, WriteOrders</commaSeparatedCustomScopes>")
    oauth = config(extra="<assetTokenSigningCertificate>0P1000000000001AAA</assetTokenSigningCertificate>")
    graph = build_graph([policies, oauth, catalog("ExternalClientApplication", "Client"), catalog("ApexClass", "Handler"),
                         catalog("OauthCustomScope", "ReadOrders"), catalog("OauthCustomScope", "WriteOrders"),
                         catalog("Certificate", "Signing", salesforce_id="0P1000000000001AAA"), catalog("Certificate", "0P1000000000001AAA")])
    assert len(edges(graph, "OauthCustomScope")) == 2 and len(edges(graph, "ApexClass")) == 1
    assert edges(graph, "Certificate")[0]["target"] == node_id("Certificate", "Signing")
    assert all(e["resolution"] == "resolved" for e in edges(graph))


@pytest.mark.parametrize("kind,extra,code", [
    ("ExtlClntAppOauthSettings", "<assetTokenSigningCertificate>Signing</assetTokenSigningCertificate>", "external_client_certificate_id_invalid"),
    ("ExtlClntAppOauthConfigurablePolicies", "<commaSeparatedPermissionSet>Approved</commaSeparatedPermissionSet>", "external_client_permission_set_id_invalid"),
    ("ExtlClntAppOauthConfigurablePolicies", "<commaSeparatedProfile>Profile</commaSeparatedProfile>", "external_client_profile_selector_unverified"),
    ("ExtlClntAppOauthConfigurablePolicies", "<executeHandlerAs>example@example.test</executeHandlerAs>", "external_client_runtime_user_not_indexed"),
    ("ExtlClntAppOauthConfigurablePolicies", "<clientCredentialsFlowUser>Example</clientCredentialsFlowUser>", "external_client_runtime_user_not_indexed"),
    ("ExtlClntAppOauthConfigurablePolicies", "<commaSeparatedCustomScopes>Read Orders</commaSeparatedCustomScopes>", "external_client_scope_name_invalid"),
    ("ExtlClntAppOauthConfigurablePolicies", "<requiredSessionLevel>Future</requiredSessionLevel>", "external_client_value_unsupported"),
])
def test_unsupported_identity_contracts_do_not_guess_matching_metadata_names(kind, extra, code):
    graph = build_graph([config(kind, extra), catalog("ExternalClientApplication", "Client"), catalog("Certificate", "Signing"),
                         catalog("PermissionSet", "Approved"), catalog("Profile", "Profile"), catalog("OauthCustomScope", "Read Orders")])
    assert len(edges(graph)) == 1 and level(graph, kind) == "partial" and code in codes(graph)


def test_incremental_reference_binding_recovers_and_reopens_gaps_without_reparsing():
    src = config(app="pkg__Client")
    wrong = catalog("ExternalClientApplication", "Client")
    target = catalog("ExternalClientApplication", "pkg__Client", namespace="pkg")
    first = build_graph([src, wrong], include_facts=True)
    assert edges(first)[0]["resolution"] == "unresolved" and level(first) == "partial"
    added = build_graph([src, wrong, target], previous_facts=first["facts"], include_facts=True)
    assert edges(added)[0]["resolution"] == "resolved" and added["stats"]["reused"] == 2 and level(added) == "semantic"
    removed = build_graph([src, wrong], previous_facts=added["facts"])
    assert edges(removed)[0]["resolution"] == "unresolved" and level(removed) == "partial"


@pytest.mark.parametrize("body", ["", "<externalClientApplication/>", "<externalClientApplication>Client</externalClientApplication><externalClientApplication>Other</externalClientApplication>", "<externalClientApplication><name>Client</name></externalClientApplication>"])
def test_parent_context_missing_or_ambiguous_has_no_edge(body):
    graph = build_graph([source("ExtlClntAppOauthSettings", body), catalog("ExternalClientApplication", "Client")])
    assert not edges(graph) and level(graph) == "partial"


def test_oversized_reference_is_not_silently_counted_as_complete():
    graph = build_graph([config(app="X" * 1025)])
    assert not edges(graph) and level(graph) == "partial"
    assert "metadata_reference_value_unsupported" in codes(graph)


def menu_item(kind, name):
    return f"<appMenuItems><type>{kind}</type><name>{escape(name)}</name></appMenuItems>"


def test_app_menu_explicit_kinds_link_to_real_components_and_remain_honestly_partial():
    kinds = ["CustomApplication", "ConnectedApp", "ExternalClientApplication", "Network", "CustomTab"]
    src = source("AppMenu", "\n".join(menu_item(kind, "Example") for kind in kinds) + menu_item("StandardAppMenuItem", "Tasks"), "AppSwitcher")
    graph = build_graph([src, *[catalog(kind, "Example") for kind in kinds], catalog("CustomTab", "Tasks")])
    assert len(edges(graph)) == len(kinds) and {e["target_kind"] for e in edges(graph)} == set(kinds)
    assert all(e["resolution"] == "resolved" and e["relation"] == "menu_item" for e in edges(graph))
    assert level(graph, "AppMenu") == "partial" and "app_menu_reserved_contract_partial" in codes(graph)


def test_app_menu_unknown_types_and_wrong_kind_names_never_fall_back_to_customer_apps():
    src = source("AppMenu", menu_item("FutureApp", "Example") + menu_item("ConnectedApp", "Example") + menu_item("CustomApplication", "pkg__Example"))
    graph = build_graph([src, catalog("CustomApplication", "Example")])
    assert len(edges(graph)) == 2 and all(e["resolution"] == "unresolved" for e in edges(graph))
    assert "app_menu_item_type_unsupported" in codes(graph)


def test_app_menu_ambiguous_slot_does_not_guess_a_kind():
    xml = menu_item("ConnectedApp", "Example").replace("</type>", "</type><type>CustomApplication</type>")
    graph = build_graph([source("AppMenu", xml), catalog("CustomApplication", "Example"), catalog("ConnectedApp", "Example")])
    assert not edges(graph) and "metadata_reference_ambiguous_scalar" in codes(graph)
