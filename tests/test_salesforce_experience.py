"""Synthetic Experience Cloud metadata; no customer source or user identities."""
import hashlib

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.experience import EMAIL_FIELDS, PAGE_FIELDS, NETWORK_WSDL_FLAGS


def source(kind, xml, name="Example", **kwargs):
    return Source(f"{kind}/{name}.xml", xml, kind, name, **kwargs)


def catalog(kind, name, **kwargs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **kwargs)


def refs(graph, kind=None):
    return [e for e in graph["edges"] if kind is None or e["target_kind"] == kind]


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


def level(graph, kind):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == kind and c["source_kind"] == "source")


def menu(items, container="Example Site", typ="Network"):
    return source("NavigationMenu", f"<NavigationMenu><container>{container}</container><containerType>{typ}</containerType>{items}</NavigationMenu>")


def item(typ, target="", extra=""):
    return f"<navigationMenuItem><type>{typ}</type><target>{target}</target>{extra}</navigationMenuItem>"


def test_network_uses_typed_slots_and_hash_verified_source_lines():
    xml = """<Network>
  <site>Example_Site</site>
  <picassoSite>SeparateSiteIdentity</picassoSite>
  <welcomeTemplate>public/Welcome</welcomeTemplate>
  <emailFooterLogo>public/Logo</emailFooterLogo>
  <selfRegProfile>Visitors</selfRegProfile>
  <networkMemberGroups><profile>admin</profile><permissionSet>Members</permissionSet></networkMemberGroups>
  <tabs><customTab>Help</customTab><defaultTab>Help</defaultTab><standardTab>Chatter</standardTab></tabs>
  <communityRoles><customerUserRole>RoleLabel</customerUserRole></communityRoles>
  <networkPageOverrides><homePageOverrideSetting>VisualForce</homePageOverrideSetting></networkPageOverrides>
  <description>{!Case.False__c}</description><emailFooterText>{!Case.False__c}</emailFooterText>
  <logoutUrl>https://example.test/Case.False__c</logoutUrl><sendWelcomeEmail>false</sendWelcomeEmail>
</Network>"""
    targets = [("CustomSite", "Example_Site"), ("SiteDotCom", "SeparateSiteIdentity"),
               ("EmailTemplate", "public/Welcome"), ("Document", "public/Logo"),
               ("Profile", "Visitors"), ("Profile", "Admin"), ("PermissionSet", "Members"), ("CustomTab", "Help")]
    g = build_graph([source("Network", xml, "Example Site"), *[catalog(*t) for t in targets],
                     catalog("Role", "RoleLabel"), catalog("ApexPage", "VisualForce"), catalog("CustomTab", "Chatter")])
    assert {e["target"] for e in g["edges"]} == {node_id(*t) for t in targets}
    assert all(e["resolution"] == "resolved" for e in g["edges"])
    assert len(refs(g, "CustomTab")) == 1  # Same target, relation and source line deduplicate.
    edge = refs(g, "EmailTemplate")[0]
    assert edge["line"] == 4 and edge["source_location"] == "L4"
    assert edge["source_sha"] == hashlib.sha256(xml.encode()).hexdigest()
    assert edge["source_file"] == "Network/Example Site.xml"
    assert level(g, "Network") == "semantic"


@pytest.mark.parametrize("tag", EMAIL_FIELDS)
def test_each_documented_network_template_slot(tag):
    g = build_graph([source("Network", f"<Network><site>Site</site><{tag}>public/Mail</{tag}></Network>"),
                     catalog("EmailTemplate", "public/Mail")])
    assert refs(g, "EmailTemplate")[0]["resolution"] == "resolved"


@pytest.mark.parametrize("tag", NETWORK_WSDL_FLAGS)
@pytest.mark.parametrize("value", ["true", "false", "0", "1"])
def test_network_new_wsdl_flags_are_literals_not_component_names(tag, value):
    g = build_graph([source("Network", f"<Network><site>Portal</site><{tag}>{value}</{tag}></Network>"), catalog("CustomSite", "Portal")])
    assert level(g, "Network") == "semantic"
    assert {e["target_kind"] for e in g["edges"]} == {"CustomSite"}


@pytest.mark.parametrize("tag", NETWORK_WSDL_FLAGS)
@pytest.mark.parametrize("value", ["", "Account.Name", "yes", "TRUE", "<nested>true</nested>"])
def test_network_new_wsdl_flags_fail_closed_on_unknown_values(tag, value):
    g = build_graph([source("Network", f"<Network><{tag}>{value}</{tag}></Network>")])
    assert level(g, "Network") == "partial" and not g["edges"]


@pytest.mark.parametrize("value", ["00X000000000001", "00X000000000001AAA"])
def test_activation_template_binds_exact_id_not_prefix_or_display_name(value):
    g = build_graph([source("Network", f"<Network><site>Site</site><deviceActEmailTemplate>{value}</deviceActEmailTemplate></Network>"),
                     catalog("EmailTemplate", "public/Activation", salesforce_id="00X000000000001AAA"),
                     catalog("EmailTemplate", value), catalog("ApexClass", "WrongKind", salesforce_id=value)])
    assert refs(g, "EmailTemplate")[0]["target"] == node_id("EmailTemplate", "public/Activation")


@pytest.mark.parametrize("sfid,kind", [("00x000000000001AAA", "EmailTemplate"), ("00X000000000002AAA", "EmailTemplate"),
                                      ("00X000000000001AAA", "ApexClass"), (None, "EmailTemplate")])
def test_activation_wrong_type_case_id_or_unverified_name_cannot_resolve(sfid, kind):
    value = "00X000000000001AAA"
    g = build_graph([source("Network", f"<Network><site>Site</site><deviceActEmailTemplate>{value}</deviceActEmailTemplate></Network>"),
                     catalog(kind, value, salesforce_id=sfid)])
    assert refs(g, "EmailTemplate")[0]["resolution"] == "unresolved"


def test_activation_conflicting_id_declarations_stay_ambiguous():
    g = build_graph([source("Network", "<Network><site>Site</site><deviceActEmailTemplate>00X000000000001AAA</deviceActEmailTemplate></Network>"),
                     *[catalog("EmailTemplate", name, salesforce_id="00X000000000001AAA") for name in ("public/A", "public/B")]])
    assert refs(g, "EmailTemplate")[0]["resolution"] == "ambiguous"


@pytest.mark.parametrize("tabs", ["<customTab>Help</customTab><standardTab>Help</standardTab><defaultTab>Help</defaultTab>",
                                 "<defaultTab>Unverified</defaultTab>"])
def test_network_default_tab_requires_unambiguous_typed_context(tabs):
    g = build_graph([source("Network", f"<Network><site>Site</site><tabs>{tabs}</tabs></Network>")])
    assert "network_default_tab_unverified" in codes(g)
    assert level(g, "Network") == "partial"
    assert len(refs(g, "CustomTab")) == int("customTab" in tabs)


@pytest.mark.parametrize("tabs", ["<defaultTab>home</defaultTab>", "<standardTab>Chatter</standardTab><defaultTab>Chatter</defaultTab>"])
def test_network_builtin_tabs_are_not_custom_components(tabs):
    g = build_graph([source("Network", f"<Network><site>Site</site><tabs>{tabs}</tabs></Network>"), catalog("CustomTab", "home"), catalog("CustomTab", "Chatter")])
    assert not refs(g, "CustomTab") and level(g, "Network") == "semantic"


@pytest.mark.parametrize("slot", ["<tabs><customTab/></tabs>", "<tabs><standardTab><name>Bad</name></standardTab></tabs>",
                                 "<networkMemberGroups><profile/></networkMemberGroups>"])
def test_network_invalid_list_references_are_partial(slot):
    g = build_graph([source("Network", f"<Network><site>Site</site>{slot}</Network>")])
    assert level(g, "Network") == "partial" and "metadata_reference_value_missing" in codes(g)


@pytest.mark.parametrize("typ", ["Network", "CommunityTemplateDefinition"])
def test_menu_container_is_explicitly_typed(typ):
    g = build_graph([menu("", typ=typ), catalog(typ, "Example Site"), catalog("CustomSite", "Example Site")])
    assert g["edges"][0]["target"] == node_id(typ, "Example Site")
    assert g["edges"][0]["relation"] == "belongs_to"


def test_menu_unknown_container_is_not_guessed_by_name():
    g = build_graph([menu("", typ="FutureType"), catalog("Network", "Example Site")])
    assert not g["edges"] and "navigation_container_type_unsupported" in codes(g)


def test_menu_object_list_view_asset_and_valid_submenu():
    nested = item("SalesforceObject", "Case", "<defaultListViewId>00B000000000001AAA</defaultListViewId><menuItemBranding><tileImage>Tile</tileImage></menuItemBranding>")
    g = build_graph([menu(item("MenuLabel", "Ignored", f"<subMenu>{nested}</subMenu>")),
                     catalog("Network", "Example Site"), catalog("CustomObject", "Case"), catalog("ContentAsset", "Tile"),
                     catalog("ListView", "Case.Open", salesforce_id="00B000000000001AAA")])
    assert {e["target"] for e in g["edges"]} == {node_id(k, n) for k, n in
        [("Network", "Example Site"), ("CustomObject", "Case"), ("ContentAsset", "Tile"), ("ListView", "Case.Open")]}
    assert level(g, "NavigationMenu") == "semantic" and all(e["resolution"] == "resolved" for e in g["edges"])


@pytest.mark.parametrize("kind,name,sfid,has_object", [
    ("ListView", "Account.Open", "00B000000000001AAA", True),
    ("ApexClass", "Case.Open", "00B000000000001AAA", True),
    ("ListView", "Case.Open", "00b000000000001AAA", True),
    ("ListView", "Case.Open", "00B000000000001AAA", False),
])
def test_menu_list_view_id_requires_type_object_and_case(kind, name, sfid, has_object):
    g = build_graph([menu(item("SalesforceObject", "Case", "<defaultListViewId>00B000000000001AAA</defaultListViewId>")),
                     catalog(kind, name, salesforce_id=sfid), *([catalog("CustomObject", "Case")] if has_object else [])])
    assert refs(g, "ListView")[0]["resolution"] == "unresolved"


def test_menu_routes_external_urls_labels_and_topics_do_not_invent_metadata():
    g = build_graph([menu(item("InternalLink", "/contactsupport") + item("ExternalLink", "https://example.test/Case.Fake__c")
                         + item("MenuLabel", "{!Case.Fake__c}") + item("NavigationalTopic", "Ignored")),
                     catalog("ApexPage", "contactsupport"), catalog("CustomField", "Case.Fake__c"), catalog("Topic", "Ignored")])
    assert {e["target_kind"] for e in g["edges"]} == {"Network"}
    assert codes(g) >= {"navigation_internal_route_unresolved", "navigation_topic_records_not_indexed"}
    assert level(g, "NavigationMenu") == "partial"


@pytest.mark.parametrize("parent,child", [("ExternalLink", "SalesforceObject"), ("MenuLabel", "MenuLabel"), ("MenuLabel", "NavigationalTopic")])
def test_invalid_submenus_cannot_claim_links_from_invalid_context(parent, child):
    g = build_graph([menu(item(parent, "https://example.test", f"<subMenu>{item(child, 'Case', '<menuItemBranding><tileImage>Hidden</tileImage></menuItemBranding>')}</subMenu>"))])
    assert "navigation_submenu_context_invalid" in codes(g)
    assert {e["target_kind"] for e in g["edges"]} == {"Network"}


def test_legacy_network_menu_and_nested_unknown_shape_are_checked():
    xml = "<Network><site>Site</site><navigationLinkSet>" + item("MenuLabel", "", "<subMenu>" + item("SalesforceObject", "Case", "<future><field>Account.Secret__c</field></future>") + "</subMenu>") + "</navigationLinkSet></Network>"
    g = build_graph([source("Network", xml)])
    assert [e["target_name"] for e in refs(g, "CustomObject")] == ["Case"]
    assert "metadata_xml_property_unsupported" in codes(g) and not refs(g, "FieldPath")


@pytest.mark.parametrize("tag", PAGE_FIELDS)
def test_each_site_page_slot_is_visualforce_not_a_guessed_component(tag):
    body = f"<{tag}>Landing</{tag}>" + ("<indexPage>Index</indexPage>" if tag != "indexPage" else "")
    g = build_graph([source("CustomSite", f"<CustomSite>{body}</CustomSite>"), catalog("ApexPage", "Landing"), catalog("LightningComponentBundle", "Landing")])
    assert next(e for e in g["edges"] if e["target_name"] == "Landing")["target"] == node_id("ApexPage", "Landing")


def test_site_profile_assets_and_users_have_distinct_kinds_and_honest_gaps():
    g = build_graph([source("CustomSite", """<CustomSite><indexPage>Index</indexPage><guestProfile>Visitors</guestProfile>
      <favoriteIcon>Icon</favoriteIcon><serverIsDown>Offline</serverIsDown>
      <customWebAddresses><certificate>SiteCert</certificate><domainName>example.test</domainName></customWebAddresses>
      <siteAdmin>admin@example.test</siteAdmin><siteGuestRecordDefaultOwner>owner@example.test</siteGuestRecordDefaultOwner>
      <portal>Legacy</portal><analyticsTrackingCode>{!Case.Fake__c}</analyticsTrackingCode>
      <siteRedirectMappings><source>/old</source><target>/Case.Fake__c</target></siteRedirectMappings></CustomSite>"""),
      catalog("CustomObject", "User"), catalog("Profile", "Visitors"), catalog("StaticResource", "Icon"), catalog("StaticResource", "Offline"), catalog("Certificate", "SiteCert")])
    assert all(e["resolution"] == "unresolved" for e in refs(g, "SalesforceUser"))
    assert len(refs(g, "SalesforceUser")) == 2
    assert all(e["resolution"] == "resolved" for e in refs(g, "StaticResource") + refs(g, "Profile") + refs(g, "Certificate"))
    assert not refs(g, "FieldPath")
    assert codes(g) >= {"metadata_user_record_not_indexed", "site_portal_identity_unverified"}


@pytest.mark.parametrize("kind,body", [("KeywordList", "<keywords><keyword>{!Case.Fake__c}</keyword></keywords>"),
                                      ("UserCriteria", "<userTypes>Internal</userTypes><creationAgeInSeconds>100</creationAgeInSeconds>"),
                                      ("ModerationRule", "<userMessage>{!Case.Fake__c}</userMessage>")])
def test_site_member_name_binds_network_without_underscore_or_suffix_guess(kind, body):
    if kind == "ModerationRule":
        body += "<action>Block</action><active>false</active><masterLabel>Example</masterLabel>"
    g = build_graph([source(kind, f"<{kind}>{body}</{kind}>", "Example Site.Member"), catalog("Network", "Example Site"),
                     catalog("CustomSite", "Example Site"), catalog("CustomSite", "Example_Site"), catalog("SiteDotCom", "Example_Site1")])
    assert len(g["edges"]) == 1 and g["edges"][0]["target"] == node_id("Network", "Example Site")
    assert g["edges"][0]["relation"] == "belongs_to" and level(g, kind) == "semantic"


@pytest.mark.parametrize("name", ["Member", ".Member", "Site.", "Site.Bad Member"])
def test_site_member_requires_verified_full_name_context(name):
    g = build_graph([source("KeywordList", "<KeywordList/>", name)])
    assert not g["edges"] and "experience_site_context_missing" in codes(g)


def test_presence_profiles_statuses_and_users_are_configuration_not_effective_access():
    g = build_graph([source("PresenceUserConfig", """<PresenceUserConfig><assignments>
      <profiles><profile>Agents</profile></profiles><users><user>agent@example.test</user></users></assignments>
      <presenceStatusOnDecline>Available</presenceStatusOnDecline><presenceStatusOnPushTimeout>Busy</presenceStatusOnPushTimeout>
      <declineReasons>Busy;Other</declineReasons><capacity>10</capacity><label>{!Case.Fake__c}</label></PresenceUserConfig>"""),
      catalog("Profile", "Agents"), catalog("ServicePresenceStatus", "Available"), catalog("ServicePresenceStatus", "Busy")])
    assert len(refs(g, "ServicePresenceStatus")) == 2 and len(refs(g, "Profile")) == 1
    assert refs(g, "SalesforceUser")[0]["resolution"] == "unresolved"
    assert codes(g) >= {"presence_decline_reason_list_unverified", "metadata_user_record_not_indexed"}
    assert not refs(g, "FieldPath") and {e["relation"] for e in g["edges"]} == {"references"}


def test_literal_only_presence_config_can_legitimately_have_no_dependencies():
    g = build_graph([source("PresenceUserConfig", "<PresenceUserConfig><capacity>10</capacity><enableAutoAccept>false</enableAutoAccept></PresenceUserConfig>")])
    assert not g["edges"] and level(g, "PresenceUserConfig") == "semantic"


@pytest.mark.parametrize("kind,xml", [
    ("Network", "<Network><site>First</site><site>Second</site></Network>"),
    ("CustomSite", "<CustomSite><indexPage><future>Page</future></indexPage></CustomSite>"),
    ("NavigationMenu", "<NavigationMenu><container>Site</container></NavigationMenu>"),
    ("CustomSite", "<Network><site>NotAPage</site></Network>"),
])
def test_missing_duplicate_and_wrong_root_contexts_stay_partial(kind, xml):
    g = build_graph([source(kind, xml)])
    assert not g["edges"] and level(g, kind) == "partial"


def test_incremental_site_reference_rebinds_without_stale_removed_target():
    src = source("Network", "<Network><site>Site</site><welcomeTemplate>public/Welcome</welcomeTemplate></Network>")
    first = build_graph([src, catalog("EmailTemplate", "public/Welcome")], include_facts=True)
    second = build_graph([src], previous_facts=first["facts"])
    assert second["stats"]["reused"] == 1
    assert refs(second, "EmailTemplate")[0]["resolution"] == "unresolved"
