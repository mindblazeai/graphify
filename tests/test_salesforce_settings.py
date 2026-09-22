"""Settings are dispatched by verified XML root, not arbitrary value strings."""
import pytest

from graphify.salesforce import Source, build_graph, node_id


def catalog(kind, name, **kwargs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **kwargs)


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


def source(root, body=""):
    return Source("settings/" + root + ".settings", f"<{root}>\n{body}\n</{root}>", "Settings", root.removesuffix("Settings"))


def level(graph):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == "Settings")


@pytest.mark.parametrize("root,body,target_kind,target_name,relation", [
    ("LightningExperienceSettings", "<activeThemeName>Brand</activeThemeName><enableS1DesktopEnabled>true</enableS1DesktopEnabled>", "LightningExperienceTheme", "Brand", "activates"),
    ("IdentityProviderSettings", "<enableIdentityProvider>true</enableIdentityProvider><certificateName>Signing</certificateName>", "Certificate", "Signing", "uses_certificate"),
    ("SearchSettings", "<searchSettingsByObject><searchSettingsByObject><name>Account</name><resultsPerPageCount>25</resultsPerPageCount></searchSettingsByObject></searchSettingsByObject>", "CustomObject", "Account", "configures"),
    ("Territory2Settings", "<opportunityFilterSettings><apexClassName>AssignTerritory</apexClassName><enableFilter>true</enableFilter></opportunityFilterSettings>", "ApexClass", "AssignTerritory", "executes"),
    ("ForecastingObjectListSettings", "<forecastingTypeObjectListSettings><forecastingTypeDeveloperName>Revenue</forecastingTypeDeveloperName></forecastingTypeObjectListSettings>", "ForecastingType", "Revenue", "configures"),
    ("ForecastingSettings", "<forecastingTypeSettings><name>Revenue</name><active>true</active></forecastingTypeSettings>", "ForecastingType", "Revenue", "configures"),
])
def test_settings_reference_slots_have_exact_type_and_evidence(root, body, target_kind, target_name, relation):
    src = source(root, body)
    graph = build_graph([src, catalog(target_kind, target_name), catalog("CustomField", target_name)])
    edge, = graph["edges"]
    assert edge["target"] == node_id(target_kind, target_name) and edge["resolution"] == "resolved"
    assert edge["source"] == src.component_id and edge["relation"] == relation and edge["line"] == 2
    assert level(graph) == "semantic"
    missing = build_graph([src])
    assert missing["edges"][0]["resolution"] == "unresolved" and level(missing) == "partial"


@pytest.mark.parametrize("root,body", [
    ("LightningExperienceSettings", "<enableS1DesktopEnabled>maybe</enableS1DesktopEnabled>"),
    ("LightningExperienceSettings", "<activeThemeName>A</activeThemeName><activeThemeName>B</activeThemeName>"),
    ("LightningExperienceSettings", "<futureReference>Account.Name</futureReference>"),
    ("LightningExperienceSettings", "<enableS1DesktopEnabled><nested>X</nested></enableS1DesktopEnabled>"),
    ("IdentityProviderSettings", "<enableIdentityProvider>true</enableIdentityProvider>"),
    ("Territory2Settings", "<opportunityFilterSettings><enableFilter>true</enableFilter></opportunityFilterSettings>"),
    ("SearchSettings", "<searchSettingsByObject><name>WrongContainer</name></searchSettingsByObject>"),
    ("SearchSettings", "<searchSettingsByObject><searchSettingsByObject><name>A</name><name>B</name></searchSettingsByObject></searchSettingsByObject>"),
    ("SearchSettings", "<searchSettingsByObject><searchSettingsByObject><name>Account</name><resultsPerPageCount>-1</resultsPerPageCount></searchSettingsByObject></searchSettingsByObject>"),
])
def test_malformed_and_new_settings_contracts_stay_partial(root, body):
    assert level(build_graph([source(root, body)])) == "partial"


def test_disabled_identity_provider_empty_certificate_is_not_missing_metadata():
    graph = build_graph([source("IdentityProviderSettings", "<enableIdentityProvider>false</enableIdentityProvider><certificateName/>")])
    assert not graph["edges"] and level(graph) == "semantic"


def test_forecast_aliases_and_labels_do_not_guess_api_field_names():
    graph = build_graph([source("ForecastingObjectListSettings", "<forecastingTypeObjectListSettings><forecastingTypeDeveloperName>Revenue</forecastingTypeDeveloperName><forecastingObjectListLabelMappings><field>OPPORTUNITY.CLOSE_DATE</field><label>Account.Secret__c</label></forecastingObjectListLabelMappings></forecastingTypeObjectListSettings>"), catalog("ForecastingType", "Revenue"), catalog("CustomField", "Opportunity.CloseDate"), catalog("CustomField", "Account.Secret__c")])
    assert len(graph["edges"]) == 2 and level(graph) == "partial"
    field, = [e for e in graph["edges"] if e["target_kind"] == "FieldPath"]
    assert field["target_name"] == "OPPORTUNITY.CLOSE_DATE" and field["resolution"] == "unresolved"


def test_forecast_verbatim_api_path_binds_without_rewriting_or_guessing_context():
    graph = build_graph([source("ForecastingObjectListSettings", "<forecastingTypeObjectListSettings><forecastingTypeDeveloperName>Revenue</forecastingTypeDeveloperName><forecastingObjectListSelectedSettings><field>OPPORTUNITY.AMOUNT</field></forecastingObjectListSelectedSettings><forecastingObjectListUnselectedSettings><field>DESCRIPTION</field></forecastingObjectListUnselectedSettings></forecastingTypeObjectListSettings>"), catalog("ForecastingType", "Revenue"), catalog("CustomField", "Opportunity.Amount"), catalog("CustomField", "Opportunity.Description")])
    assert {e["target"] for e in graph["edges"]} == {node_id("ForecastingType", "Revenue"), node_id("CustomField", "Opportunity.Amount")}
    assert "forecast_column_identity_unverified" in codes(graph) and level(graph) == "partial"


def test_forecast_exact_custom_field_id_does_not_bind_same_named_field():
    graph = build_graph([source("ForecastingObjectListSettings", "<forecastingTypeObjectListSettings><forecastingTypeDeveloperName>Revenue</forecastingTypeDeveloperName><forecastingObjectListSelectedSettings><field>00N000000000001</field></forecastingObjectListSelectedSettings></forecastingTypeObjectListSettings>"), catalog("ForecastingType", "Revenue"), catalog("CustomField", "Opportunity.Target__c", salesforce_id="00N000000000001"), catalog("CustomField", "00N000000000001")])
    assert {e["target"] for e in graph["edges"]} == {node_id("ForecastingType", "Revenue"), node_id("CustomField", "Opportunity.Target__c")}
    assert level(graph) == "semantic"


@pytest.mark.parametrize("tag", ["forecastingObjectListSelectedSettings", "forecastingObjectListUnselectedSettings"])
def test_forecast_field_lists_accept_repeated_fields_in_the_same_container(tag):
    graph = build_graph([source("ForecastingObjectListSettings", f"<forecastingTypeObjectListSettings><forecastingTypeDeveloperName>Revenue</forecastingTypeDeveloperName><{tag}><field>Opportunity.Name</field><field>Account.Name</field></{tag}></forecastingTypeObjectListSettings>"), catalog("ForecastingType", "Revenue"), catalog("CustomField", "Opportunity.Name"), catalog("CustomField", "Account.Name")])
    assert len(graph["edges"]) == 3 and level(graph) == "semantic"


def test_forecast_label_mapping_field_is_still_singleton():
    graph = build_graph([source("ForecastingObjectListSettings", "<forecastingTypeObjectListSettings><forecastingTypeDeveloperName>Revenue</forecastingTypeDeveloperName><forecastingObjectListLabelMappings><field>Opportunity.Name</field><field>Account.Name</field><label>Name</label></forecastingObjectListLabelMappings></forecastingTypeObjectListSettings>"), catalog("ForecastingType", "Revenue")])
    assert len(graph["edges"]) == 1 and level(graph) == "partial"


def test_territory_supported_object_contract_links_even_when_disabled():
    graph = build_graph([source("Territory2Settings", "<supportedObjects><objectType>Lead</objectType><defaultAccessLevel>Read</defaultAccessLevel><state>Disabled</state></supportedObjects>"), catalog("CustomObject", "Lead")])
    assert graph["edges"][0]["target"] == node_id("CustomObject", "Lead") and level(graph) == "semantic"


def test_unknown_settings_remain_structural_not_semantic_based_on_boolean_shape():
    graph = build_graph([source("FutureSettings", "<enableEverything>true</enableEverything>")])
    assert not graph["edges"] and level(graph) == "structural"


def test_settings_cannot_dispatch_into_unrelated_metadata_type():
    graph = build_graph([source("HomePageLayout", "<wideComponents>Widget</wideComponents>")])
    assert not graph["edges"] and level(graph) == "structural"


def test_search_hard_limit_does_not_emit_unbounded_references():
    graph = build_graph([source("SearchSettings", "<searchSettingsByObject>" + "<searchSettingsByObject><name>Account</name></searchSettingsByObject>" * 4097 + "</searchSettingsByObject>")])
    assert not graph["edges"] and level(graph) == "partial" and "settings_list_limit" in codes(graph)
