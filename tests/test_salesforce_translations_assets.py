"""Synthetic typed translation/asset contracts and safe incremental rebinding."""
from copy import deepcopy
import hashlib

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.assets import BRAND_IMAGES, BRAND_LITERALS
from graphify.salesforce.registry import identify


def source(kind, xml, name="Example", **kwargs):
    return Source(f"{kind}/{name}.xml", xml, kind, name, **kwargs)


def catalog(kind, name, **kwargs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **kwargs)


def refs(g, kind=None):
    return [e for e in g["edges"] if kind is None or e["target_kind"] == kind]


def codes(g):
    return {d["code"] for d in g["diagnostics"]}


def level(g, kind):
    return next(c["level"] for c in g["coverage"] if c["metadata_type"] == kind and c["source_kind"] == "source")


def test_object_translation_preserves_child_identity_with_real_field_and_source_evidence():
    xml = """<CustomObjectTranslation>
  <caseValues><value>{!Account.NotAReference__c}</value></caseValues>
  <fields><name>Amount__c</name><label>{!Case.False__c}</label>
    <help>{!Account.Secret__c}</help><lookupFilter><errorMessage>{!Case.False__c}</errorMessage></lookupFilter>
    <picklistValues><masterLabel>Account.False__c</masterLabel><translation>{!Case.False__c}</translation></picklistValues></fields>
  <nameFieldLabel>Translated name</nameFieldLabel>
</CustomObjectTranslation>"""
    g = build_graph([source("CustomObjectTranslation", xml, "Invoice__c-fr"), catalog("CustomObject", "Invoice__c"),
                     catalog("CustomField", "Invoice__c.Amount__c"), catalog("CustomField", "Invoice__c.Name")])
    child_id = node_id("CustomFieldTranslation", "Invoice__c-fr.Amount__c")
    assert {e["target_name"] for e in g["edges"]} == {"Invoice__c", "Invoice__c-fr.Amount__c", "Invoice__c.Amount__c", "Invoice__c.Name"}
    child = next(n for n in g["nodes"] if n["id"] == child_id)
    assert child["component_id"] == node_id("CustomObjectTranslation", "Invoice__c-fr")
    edge = next(e for e in g["edges"] if e["source"] == child_id)
    assert edge["relation"] == "translates" and edge["line"] == 3
    assert edge["source_sha"] == hashlib.sha256(xml.encode()).hexdigest()
    assert level(g, "CustomObjectTranslation") == "semantic"
    assert all(e["resolution"] == "resolved" for e in g["edges"])


@pytest.mark.parametrize("tag,kind", [("fieldSets", "FieldSet"), ("quickActions", "QuickAction"),
                                     ("recordTypes", "RecordType"), ("sharingReasons", "SharingReason"),
                                     ("validationRules", "ValidationRule"), ("webLinks", "WebLink"), ("workflowTasks", "WorkflowTask")])
def test_translation_member_slots_are_object_scoped_and_typed(tag, kind):
    g = build_graph([source("CustomObjectTranslation", f"<CustomObjectTranslation><{tag}><name>Member</name></{tag}></CustomObjectTranslation>", "Invoice__c-fr"),
                     catalog("CustomObject", "Invoice__c"), catalog(kind, "Invoice__c.Member"), catalog(kind, "Other__c.Member")])
    assert refs(g, kind)[0]["target"] == node_id(kind, "Invoice__c.Member")


@pytest.mark.parametrize("name", ["Sales Layout", "Invoice__c-Sales Layout", "Sales-Layout"])
def test_layout_translation_uses_object_hyphen_not_dot_or_basename(name):
    expected = name if name.startswith("Invoice__c-") else "Invoice__c-" + name
    g = build_graph([source("CustomObjectTranslation", f"<CustomObjectTranslation><layouts><layout>{name}</layout><sections><section>Section</section><label>{{!Case.False__c}}</label></sections></layouts></CustomObjectTranslation>", "Invoice__c-fr"),
                     catalog("CustomObject", "Invoice__c"), catalog("Layout", expected), catalog("Layout", "Other__c-Sales Layout")])
    assert refs(g, "Layout")[0]["target"] == node_id("Layout", expected)
    assert not refs(g, "FieldPath")


def test_opaque_standard_translation_alias_is_not_guessed_from_schema_or_label():
    g = build_graph([source("CustomObjectTranslation", "<CustomObjectTranslation><fields><name>account_number</name><label>AccountNumber</label></fields></CustomObjectTranslation>", "Account-de"),
                     catalog("CustomObject", "Account"), catalog("CustomField", "Account.AccountNumber")])
    assert refs(g, "CustomField")[0]["resolution"] == "unresolved"
    assert refs(g, "CustomField")[0]["target_name"] == "Account.account_number"
    assert level(g, "CustomObjectTranslation") == "partial" and "metadata_identity_unverified" in codes(g)
    assert all(n["coverage"] == "partial" for n in g["nodes"] if n.get("metadata_type") == "CustomObjectTranslation")


def test_legacy_packaged_object_spelling_cannot_be_reordered_into_an_api_name():
    g = build_graph([source("CustomObjectTranslation", "<CustomObjectTranslation/>", "Invoice-Acme__c-de"), catalog("CustomObject", "Acme__Invoice__c")])
    assert not g["edges"] and "translation_identity_context_unverified" in codes(g)


@pytest.mark.parametrize("kind,base,target", [("GlobalValueSetTranslation", "Choices__gvs", "GlobalValueSet"),
                                            ("StandardValueSetTranslation", "CaseStatus", "StandardValueSet")])
def test_value_set_translation_uses_exact_parent_and_never_translated_values(kind, base, target):
    g = build_graph([source(kind, f"<{kind}><valueTranslation><masterLabel>Case.Status</masterLabel><translation>{{!Case.False__c}}</translation></valueTranslation></{kind}>", base + "-fr"), catalog(target, base)])
    assert len(g["edges"]) == 1 and g["edges"][0]["target"] == node_id(target, base)
    assert level(g, kind) == "semantic"


def test_global_value_set_suffix_is_not_added_or_removed_to_guess_a_target():
    g = build_graph([source("GlobalValueSetTranslation", "<GlobalValueSetTranslation/>", "Choices-fr"), catalog("GlobalValueSet", "Choices__gvs")])
    assert g["edges"][0]["resolution"] == "unresolved" and level(g, "GlobalValueSetTranslation") == "partial"


def test_decomposed_field_translation_keeps_the_registry_identity():
    path = "force-app/main/default/objectTranslations/Invoice__c-fr/fields/Amount__c.fieldTranslation-meta.xml"
    kind, name = identify(path)
    assert (kind, name) == ("CustomFieldTranslation", "Invoice__c-fr.Amount__c")
    g = build_graph([Source(path, "<CustomFieldTranslation><name>Amount__c</name><label>Montant</label></CustomFieldTranslation>", kind, name),
                     catalog("CustomField", "Invoice__c.Amount__c")])
    assert g["edges"][0]["target"] == node_id("CustomField", "Invoice__c.Amount__c")
    assert level(g, kind) == "semantic"


@pytest.mark.parametrize("xml,name", [
    ("<CustomFieldTranslation><name>Other__c</name></CustomFieldTranslation>", "Invoice__c-fr.Amount__c"),
    ("<CustomFieldTranslation><name>Amount__c</name></CustomFieldTranslation>", "Amount__c"),
    ("<CustomFieldTranslation><name>Amount__c</name><name>Other__c</name></CustomFieldTranslation>", "Invoice__c-fr.Amount__c"),
])
def test_decomposed_field_context_conflicts_are_partial_not_guessed(xml, name):
    g = build_graph([source("CustomFieldTranslation", xml, name)])
    assert not g["edges"] and level(g, "CustomFieldTranslation") == "partial"


def test_translation_binding_overlay_does_not_poison_facts_or_remain_stale_after_rebind():
    src = source("CustomObjectTranslation", "<CustomObjectTranslation><fields><name>Amount__c</name></fields></CustomObjectTranslation>", "Invoice__c-fr")
    obj = catalog("CustomObject", "Invoice__c")
    field = catalog("CustomField", "Invoice__c.Amount__c")
    first = build_graph([src, obj], include_facts=True)
    original_facts = deepcopy(first["facts"])
    assert level(first, "CustomObjectTranslation") == "partial"
    assert next(f for f in first["facts"].values() if f["coverage"]["source_kind"] == "source")["coverage"]["level"] == "semantic"
    second = build_graph([src, obj, field], previous_facts=first["facts"], include_facts=True)
    assert first["facts"] == original_facts
    assert level(second, "CustomObjectTranslation") == "semantic" and second["stats"]["reused"] == 2
    third = build_graph([src, obj], previous_facts=second["facts"])
    assert level(third, "CustomObjectTranslation") == "partial"
    assert refs(third, "CustomField")[0]["resolution"] == "unresolved"


def test_wrong_translation_root_identity_and_unknown_legacy_field_shape_stay_partial():
    g = build_graph([source("CustomObjectTranslation", "<CustomObjectTranslation><fullName>Other__c-fr</fullName><fields><name>Amount__c</name></fields></CustomObjectTranslation>", "Invoice__c-fr")])
    assert not g["edges"] and "translation_source_identity_mismatch" in codes(g)
    g = build_graph([source("CustomObjectTranslation", "<CustomObjectTranslation><standardFields><name>Phone</name></standardFields><namedFilters><name>Legacy</name></namedFilters></CustomObjectTranslation>", "Account-fr")])
    assert codes(g) >= {"metadata_xml_property_unsupported", "translation_legacy_filter_identity_unverified"}
    assert not refs(g, "CustomField")


def asset(extra=""):
    return source("ContentAsset", f"<ContentAsset>{extra}<versions><version><number>1</number><pathOnClient>{{!Case.False__c}}.png</pathOnClient></version></versions></ContentAsset>")


def test_asset_origin_network_is_a_ref_but_binary_content_and_client_filenames_are_not():
    g = build_graph([asset("<originNetwork>Example Site</originNetwork><masterLabel>{!Case.False__c}</masterLabel><content>PGFzZXQ+</content>"), catalog("Network", "Example Site")])
    assert len(g["edges"]) == 1 and g["edges"][0]["target"] == node_id("Network", "Example Site")
    assert level(g, "ContentAsset") == "partial" and "asset_payload_not_analyzed" in codes(g)
    assert next(n for n in g["nodes"] if n.get("metadata_type") == "ContentAsset")["content_analysis"] == "not_parsed"


@pytest.mark.parametrize("kind", ["emailTemplate", "network", "insightsApplication", "workspace"])
def test_reserved_asset_link_name_cannot_bind_a_coincidentally_named_component(kind):
    g = build_graph([asset(f"<relationships><{kind}><access>VIEWER</access><name>Example</name></{kind}></relationships>"), catalog("EmailTemplate", "Example"), catalog("Network", "Example")])
    assert not g["edges"] and "asset_link_identity_unverified" in codes(g)


def test_asset_org_sharing_does_not_invent_effective_grants_or_metadata_targets():
    g = build_graph([asset("<relationships><organization><access>COLLABORATOR</access></organization></relationships>")])
    assert not g["edges"] and codes(g) == {"asset_payload_not_analyzed"}


@pytest.mark.parametrize("body,code", [("", "asset_versions_missing_or_ambiguous"),
    ("<versions><version><number>1</number></version></versions>", "metadata_reference_value_missing"),
    ("<relationships><organization><access>Future</access></organization></relationships>", "asset_link_access_unsupported")])
def test_asset_unknown_or_missing_envelope_fields_are_reported(body, code):
    g = build_graph([source("ContentAsset", f"<ContentAsset>{body}</ContentAsset>")])
    assert code in codes(g) and level(g, "ContentAsset") == "partial"


def test_document_folder_is_exact_and_legacy_display_name_is_not_an_alias():
    g = build_graph([source("Document", "<Document><name>Legacy Display Name</name><description>{!Case.False__c}</description></Document>", "Outer/Inner/Logo"),
                     catalog("DocumentFolder", "Outer/Inner"), catalog("DocumentFolder", "Inner")])
    assert len(g["edges"]) == 1 and g["edges"][0]["target"] == node_id("DocumentFolder", "Outer/Inner")
    assert level(g, "Document") == "partial" and "asset_payload_not_analyzed" in codes(g)


def test_static_resource_envelope_does_not_claim_to_have_parsed_base64_or_code():
    g = build_graph([source("StaticResource", "<StaticResource><contentType>application/javascript</contentType><description>{!Case.False__c}</description><content>opaque</content></StaticResource>")])
    assert not g["edges"] and level(g, "StaticResource") == "partial"
    assert "asset_payload_not_analyzed" in codes(g)


def test_global_value_set_literals_do_not_become_field_usages():
    g = build_graph([source("GlobalValueSet", "<GlobalValueSet><customValue><fullName>Case.Status</fullName><label>{!Account.False__c}</label><default>false</default></customValue><description>{!Case.False__c}</description></GlobalValueSet>")])
    assert not g["edges"] and level(g, "GlobalValueSet") == "semantic"


def test_remote_url_is_configuration_not_a_proven_apex_usage():
    g = build_graph([source("RemoteSiteSetting", "<RemoteSiteSetting><url>https://example.test/Case.False__c</url><description>{!Case.False__c}</description></RemoteSiteSetting>")])
    assert not g["edges"] and level(g, "RemoteSiteSetting") == "semantic"


@pytest.mark.parametrize("target", ["Brand", "0LZ000000000001", "0LZ000000000001AAA"])
def test_theme_binds_documented_name_or_exact_id_with_typed_declarations(target):
    g = build_graph([source("LightningExperienceTheme", f"<LightningExperienceTheme><defaultBrandingSet>{target}</defaultBrandingSet></LightningExperienceTheme>"),
                     catalog("BrandingSet", "Brand", salesforce_id="0LZ000000000001AAA"), catalog("ApexClass", target, salesforce_id="0LZ000000000001AAA")])
    assert g["edges"][0]["target"] == node_id("BrandingSet", "Brand") and level(g, "LightningExperienceTheme") == "semantic"


def test_theme_does_not_assume_a_fifteen_character_name_is_an_id_and_keeps_collisions():
    value = "BrandingSetName"
    assert len(value) == 15
    src = source("LightningExperienceTheme", f"<LightningExperienceTheme><defaultBrandingSet>{value}</defaultBrandingSet></LightningExperienceTheme>")
    g = build_graph([src, catalog("BrandingSet", value)])
    assert g["edges"][0]["resolution"] == "resolved"
    g = build_graph([src, catalog("BrandingSet", value), catalog("BrandingSet", "Other", salesforce_id=value)])
    assert g["edges"][0]["resolution"] == "ambiguous" and level(g, "LightningExperienceTheme") == "partial"


def test_theme_wrong_id_case_cannot_resolve():
    g = build_graph([source("LightningExperienceTheme", "<LightningExperienceTheme><defaultBrandingSet>0LZ000000000001AAA</defaultBrandingSet></LightningExperienceTheme>"),
                     catalog("BrandingSet", "Brand", salesforce_id="0lz000000000001AAA")])
    assert g["edges"][0]["resolution"] == "unresolved" and level(g, "LightningExperienceTheme") == "partial"


@pytest.mark.parametrize("slot", sorted(BRAND_IMAGES))
def test_each_documented_brand_image_slot_has_a_typed_asset_link(slot):
    g = build_graph([source("BrandingSet", f"<BrandingSet><brandingSetProperty><propertyName>{slot}</propertyName><propertyValue>Logo</propertyValue></brandingSetProperty></BrandingSet>"), catalog("ContentAsset", "Logo")])
    assert g["edges"][0]["target"] == node_id("ContentAsset", "Logo") and level(g, "BrandingSet") == "semantic"


def test_brand_asset_paths_are_not_basename_guesses_and_unknown_properties_remain_partial():
    g = build_graph([source("BrandingSet", "<BrandingSet><brandingSetProperty><propertyName>BRAND_IMAGE</propertyName><propertyValue>/unknown/Logo.png</propertyValue></brandingSetProperty><brandingSetProperty><propertyName>FutureImage</propertyName><propertyValue>{!Case.False__c}</propertyValue></brandingSetProperty><type>c:definition</type></BrandingSet>"), catalog("ContentAsset", "Logo")])
    assert len(g["edges"]) == 1 and g["edges"][0]["resolution"] == "unresolved"
    assert codes(g) >= {"metadata_identity_unverified", "branding_property_semantics_unverified", "branding_definition_identity_unverified"}


@pytest.mark.parametrize("path,version", [("/file-asset/Logo", None), ("/file-asset/Logo?v=2", "2")])
def test_documented_local_asset_route_binds_exact_api_identity(path, version):
    g = build_graph([source("BrandingSet", f"<BrandingSet><brandingSetProperty><propertyName>BRAND_IMAGE</propertyName><propertyValue>{path}</propertyValue></brandingSetProperty></BrandingSet>"), catalog("ContentAsset", "Logo")])
    edge, = g["edges"]
    assert edge["target"] == node_id("ContentAsset", "Logo") and edge["resolution"] == "resolved"
    assert edge["asset_reference"] == path and edge["asset_version"] == version
    assert level(g, "BrandingSet") == "semantic"


@pytest.mark.parametrize("path", ["https://other.my.salesforce.com/file-asset/Logo", "//other/file-asset/Logo",
    "/file-asset/Logo?oid=00D000000000001", "/file-asset/Logo?v=1&amp;oid=00D000000000001",
    "/file-asset/Logo/extra", "/file-asset/Logo.png", "/file-asset/%4cogo", "/file-asset/Logo?v=dynamic"])
def test_nonlocal_or_unverified_asset_route_cannot_bind_same_named_asset(path):
    g = build_graph([source("BrandingSet", f"<BrandingSet><brandingSetProperty><propertyName>BRAND_IMAGE</propertyName><propertyValue>{path}</propertyValue></brandingSetProperty></BrandingSet>"), catalog("ContentAsset", "Logo")])
    assert g["edges"][0]["resolution"] == "unresolved" and level(g, "BrandingSet") == "partial"


@pytest.mark.parametrize("slot", sorted(BRAND_LITERALS))
def test_documented_brand_color_and_boolean_slots_are_not_expressions(slot):
    g = build_graph([source("BrandingSet", f"<BrandingSet><brandingSetProperty><propertyName>{slot}</propertyName><propertyValue>{{!Case.False__c}}</propertyValue></brandingSetProperty></BrandingSet>")])
    assert not g["edges"] and level(g, "BrandingSet") == "semantic"


def notification(mode, target=""):
    return source("CustomNotificationType", f"<CustomNotificationType><actionGroups><groupName>Actions</groupName><actions><actionType>{mode}</actionType><actionTarget>{target}</actionTarget><actionLabel>{{!Case.False__c}}</actionLabel></actions></actionGroups></CustomNotificationType>")


def test_notification_api_action_links_apex_but_share_is_not_a_class():
    g = build_graph([notification("NotificationApiAction", "Handler"), catalog("ApexClass", "Handler")])
    assert g["edges"][0]["target"] == node_id("ApexClass", "Handler") and g["edges"][0]["relation"] == "invokes"
    g = build_graph([notification("Share")])
    assert not g["edges"] and level(g, "CustomNotificationType") == "semantic"


@pytest.mark.parametrize("mode,target,code", [("Share", "Handler", "notification_action_context_conflict"),
    ("Future", "Handler", "notification_action_type_unsupported"), ("NotificationApiAction", "", "notification_action_target_missing")])
def test_notification_invalid_action_context_stays_partial(mode, target, code):
    g = build_graph([notification(mode, target), catalog("ApexClass", "Handler")])
    assert not g["edges"] and code in codes(g) and level(g, "CustomNotificationType") == "partial"
