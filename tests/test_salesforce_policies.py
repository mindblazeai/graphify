"""Synthetic policy fixtures: named metadata, literal values, and virtual schemas."""
import hashlib
from xml.sax.saxutils import escape

import pytest

from graphify.salesforce import Source, build_graph, node_id


def source(kind, xml, name="Example", **kwargs):
    return Source(f"{kind}/{name}.xml", xml, kind, name, **kwargs)


def catalog(kind, name, **kwargs):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", **kwargs)


def refs(graph, kind=None):
    return [e for e in graph["edges"] if kind is None or e["target_kind"] == kind]


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


def level(graph, kind):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == kind)


def restriction(record="Id=$User.Id", user="$User.IsActive=true", mode="FieldSet", classification="Personal"):
    return source("FieldRestrictionRule", f"""<FieldRestrictionRule>
  <targetEntity>User</targetEntity><enforcementType>FieldRestrict</enforcementType>
  <classificationType>{mode}</classificationType><classification>{classification}</classification>
  <recordFilter>{escape(record)}</recordFilter>
  <userCriteria>{escape(user)}</userCriteria>
</FieldRestrictionRule>""")


def restriction_catalog():
    return [catalog("CustomObject", "User"), catalog("FieldSet", "User.Personal"),
            *[catalog("CustomField", "User." + name) for name in ("Id", "IsActive", "UserType", "Score__c")]]


def test_restriction_case_expression_links_real_fields_and_fieldset_with_hash_and_line():
    src = restriction(user="CASE($User.UserType,'Standard',0,'Guest',1,0)=1")
    graph = build_graph([src, *restriction_catalog()])
    assert {e["target_name"] for e in refs(graph)} == {"User", "User.Personal", "User.Id", "User.UserType"}
    assert all(e["resolution"] == "resolved" for e in refs(graph))
    assert level(graph, "FieldRestrictionRule") == "semantic"
    field = next(e for e in refs(graph, "FieldPath") if e["target_name"] == "User.UserType")
    assert field["line"] == 5 and field["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()
    assert field["relation"] == "reads" and field["identity_contract"] == "restriction_field"


@pytest.mark.parametrize("record,expected", [
    ("Id='Account.False__c'", {"User.Id"}),
    ('Id="User.Fake__c"', {"User.Id"}),
    ("IF(IsActive,Score__c,1e3) >= -2", {"User.IsActive", "User.Score__c"}),
    ("!(IsActive=false) && NOT(ISBLANK(Id))", {"User.IsActive", "User.Id"}),
    ("User.Id=$User.Id", {"User.Id"}),
    ("Id='it''s not a field'", {"User.Id"}),
    ("Score__c = -(1 + 2)", {"User.Score__c"}),
    ("true", set()),
])
def test_restriction_literals_functions_and_operators_are_not_field_names(record, expected):
    graph = build_graph([restriction(record, user="true"), *restriction_catalog()])
    assert {e["target_name"] for e in refs(graph, "FieldPath")} == expected
    assert level(graph, "FieldRestrictionRule") == "semantic"


@pytest.mark.parametrize("record", ["Id=", "(Id=true", "Id='unterminated", "Id=true junk", "Id ? true : false",
                                     "IF(Id,true)", "CASE(Id,1,2)", "AND()", "Id[0]=true", "Id=true; Id=false",
                                     "(" * 70 + "Id" + ")" * 70, "Id" + " " * 16384])
def test_restriction_invalid_or_unbounded_expression_keeps_no_speculative_references(record):
    graph = build_graph([restriction(record, user="true"), *restriction_catalog()])
    assert not refs(graph, "FieldPath")
    assert level(graph, "FieldRestrictionRule") == "partial"
    assert codes(graph) & {"restriction_expression_syntax_unsupported", "restriction_expression_limit"}


def test_restriction_unknown_function_preserves_known_arguments_but_stays_partial():
    graph = build_graph([restriction("FutureFunction(Id)=true", user="true"), *restriction_catalog()])
    assert {e["target_name"] for e in refs(graph, "FieldPath")} == {"User.Id"}
    assert "restriction_formula_function_unsupported" in codes(graph)
    assert level(graph, "FieldRestrictionRule") == "partial"


@pytest.mark.parametrize("user", ["IsActive=true", "$Organization.Name='Example'", "$Profile.Name='Example'"])
def test_restriction_user_criteria_needs_explicit_supported_global_context(user):
    graph = build_graph([restriction("true", user), *restriction_catalog()])
    assert not refs(graph, "FieldPath")
    assert "restriction_expression_context_unsupported" in codes(graph)


def test_restriction_compliance_categories_are_not_fieldsets_and_missing_type_defaults_to_category():
    src = restriction(mode="ComplianceCategory", classification="PII")
    for xml in (src.content, src.content.replace("<classificationType>ComplianceCategory</classificationType>", "")):
        graph = build_graph([source(src.metadata_type, xml), *restriction_catalog(), catalog("FieldSet", "User.PII")])
        assert not refs(graph, "FieldSet")
        assert level(graph, "FieldRestrictionRule") == "semantic"


@pytest.mark.parametrize("old,new,code", [
    ("<targetEntity>User</targetEntity>", "<targetEntity>Account</targetEntity>", "restriction_object_unsupported"),
    ("<targetEntity>User</targetEntity>", "<targetEntity>User</targetEntity><targetEntity>Employee</targetEntity>", "metadata_reference_ambiguous_scalar"),
    ("<classification>Personal</classification>", "<classification>Account.Personal</classification>", "restriction_field_set_context_conflict"),
    ("<classificationType>FieldSet</classificationType>", "<classificationType>Future</classificationType>", "restriction_classification_unsupported"),
    ("<classification>Personal</classification>", "", "restriction_classification_missing"),
    ("<enforcementType>FieldRestrict</enforcementType>", "<enforcementType>Future</enforcementType>", "metadata_policy_value_unsupported"),
])
def test_restriction_ambiguous_or_unsupported_contract_is_partial(old, new, code):
    src = restriction()
    graph = build_graph([source(src.metadata_type, src.content.replace(old, new)), *restriction_catalog()])
    assert level(graph, src.metadata_type) == "partial" and code in codes(graph)
    assert not any(e["target_name"] == "Account.Personal" for e in refs(graph))


def test_restriction_exact_fieldset_identity_overlay_can_recover_without_reparse():
    src = restriction()
    missing = [s for s in restriction_catalog() if s.metadata_type != "FieldSet"]
    first = build_graph([src, *missing], include_facts=True)
    assert level(first, src.metadata_type) == "partial"
    added = build_graph([src, *restriction_catalog()], previous_facts=first["facts"], include_facts=True)
    assert level(added, src.metadata_type) == "semantic" and added["stats"]["reused"] == len(missing) + 1
    removed = build_graph([src, *missing], previous_facts=added["facts"])
    assert refs(removed, "FieldSet")[0]["resolution"] == "unresolved"
    assert level(removed, src.metadata_type) == "partial"


def notification(name, app="Client", enabled="true"):
    return f"""<notificationTypeSettings><notificationType>{name}</notificationType>
      <appSettings><connectedAppName>{app}</connectedAppName><enabled>{enabled}</enabled></appSettings>
      <notificationChannels><desktopEnabled>true</desktopEnabled><mobileEnabled>false</mobileEnabled></notificationChannels>
      </notificationTypeSettings>"""


def test_notification_standard_names_are_not_fabricated_custom_types_and_siblings_stay_owned():
    src = source("NotificationTypeConfig", "<NotificationTypeConfig>" + notification("CustomAlert") + notification("ProviderAlert", "Other", "false") + "</NotificationTypeConfig>")
    graph = build_graph([src, catalog("CustomNotificationType", "CustomAlert"), catalog("ConnectedApp", "Client"), catalog("ConnectedApp", "Other")])
    assert len(refs(graph, "NotificationDeliverySetting")) == 2
    settings = refs(graph, "NotificationType")
    assert {e["target_name"]: e["resolution"] for e in settings} == {"CustomAlert": "resolved", "ProviderAlert": "unresolved"}
    assert not refs(graph, "CustomNotificationType")
    assert not any(n["kind"] == "CustomNotificationType" and n["external"] for n in graph["nodes"])
    custom = next(e for e in settings if e["target_name"] == "CustomAlert")
    assert custom["target"] == node_id("CustomNotificationType", "CustomAlert")
    disabled = next(e for e in refs(graph, "NotificationApp") if e["target_name"] == "Other")
    assert disabled["enabled"] is False and disabled["relation"] == "configures_delivery"
    assert disabled["source"] != custom["source"]
    assert all(n["component_id"] == src.component_id for n in graph["nodes"] if n["kind"] == "NotificationDeliverySetting")


@pytest.mark.parametrize("app", ["Client", "pkg__Client"])
def test_notification_exact_external_client_application_wins_over_connected_app(app):
    src = source("NotificationTypeConfig", "<NotificationTypeConfig>" + notification("CustomAlert", app) + "</NotificationTypeConfig>")
    graph = build_graph([src, catalog("CustomNotificationType", "CustomAlert"), catalog("ConnectedApp", app), catalog("ExternalClientApplication", app)])
    assert refs(graph, "NotificationApp")[0]["target"] == node_id("ExternalClientApplication", app)
    assert level(graph, src.metadata_type) == "semantic"


def test_notification_package_prefix_is_never_stripped_to_customer_app():
    src = source("NotificationTypeConfig", "<NotificationTypeConfig>" + notification("pkg__Alert", "pkg__Client") + "</NotificationTypeConfig>")
    graph = build_graph([src, catalog("CustomNotificationType", "Alert"), catalog("ConnectedApp", "Client")])
    assert all(e["resolution"] == "unresolved" for e in refs(graph) if e["relation"] != "contains")


def test_notification_duplicate_type_settings_cannot_merge_sibling_app_bindings():
    src = source("NotificationTypeConfig", "<NotificationTypeConfig>" + notification("Alert") + notification("alert", "Other") + "</NotificationTypeConfig>")
    graph = build_graph([src])
    assert not graph["edges"] and "notification_type_settings_duplicate" in codes(graph)


@pytest.mark.parametrize("old,new,code", [
    ("<enabled>true</enabled>", "<enabled>future</enabled>", "metadata_policy_value_unsupported"),
    ("<connectedAppName>Client</connectedAppName>", "", "metadata_reference_value_missing"),
    ("<notificationType>Alert</notificationType>", "<notificationType>Alert</notificationType><notificationType>Other</notificationType>", "metadata_reference_ambiguous_scalar"),
])
def test_notification_invalid_contract_is_explicitly_partial(old, new, code):
    graph = build_graph([source("NotificationTypeConfig", "<NotificationTypeConfig>" + notification("Alert").replace(old, new) + "</NotificationTypeConfig>")])
    assert level(graph, "NotificationTypeConfig") == "partial" and code in codes(graph)


def prompt(contents):
    required = "<body>Message</body><displayType>DockedComposer</displayType><masterLabel>Example</masterLabel><title>Example</title><versionNumber>1</versionNumber>"
    if "<body>" in contents:
        required = required.replace("<body>Message</body>", "")
    return source("Prompt", "<Prompt>\n<promptVersions>\n" + contents + required + "\n</promptVersions>\n</Prompt>")


def criterion(left, right="true", operator="EQUAL"):
    return f"<criteria><leftValue>{escape(left)}</leftValue><operator>{operator}</operator><rightValue>{escape(right)}</rightValue></criteria>"


def test_prompt_image_links_exact_asset_not_body_urls_labels_or_access_enum():
    src = prompt("<image>Picture</image><body>{!Case.Secret__c}</body><actionButtonLink>https://example.test/Account</actionButtonLink><userAccess>Everyone</userAccess><userProfileAccess>Everyone</userProfileAccess>")
    graph = build_graph([src, catalog("ContentAsset", "Picture"), catalog("Profile", "Everyone")])
    assert len(graph["edges"]) == 1 and refs(graph, "ContentAsset")[0]["resolution"] == "resolved"
    assert refs(graph)[0]["line"] == 3 and refs(graph)[0]["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()
    assert level(graph, "Prompt") == "semantic"


@pytest.mark.parametrize("tag", ["imageLink", "videoLink"])
def test_prompt_conflicting_media_context_has_no_guessed_asset_reference(tag):
    graph = build_graph([prompt(f"<image>Picture</image><{tag}>https://example.test/media</{tag}>")])
    assert not graph["edges"] and "prompt_media_context_conflict" in codes(graph)


@pytest.mark.parametrize("tag", ["experienceContext", "publishedByUser", "referenceElementContext", "targetRecordType",
                                    "targetPageKey1", "targetPageType", "experience"])
def test_prompt_internal_ids_page_keys_and_unknown_properties_stay_partial(tag):
    graph = build_graph([prompt(f"<{tag}>Example</{tag}>"), catalog("CustomApplication", "Example"), catalog("RecordType", "Case.Example")])
    assert not graph["edges"] and level(graph, "Prompt") == "partial"


def test_prompt_deprecated_application_fields_have_an_explicit_namespace_contract():
    graph = build_graph([prompt("<targetAppDeveloperName>Console</targetAppDeveloperName><targetAppNamespacePrefix>pkg</targetAppNamespacePrefix>"),
                         catalog("CustomApplication", "pkg__Console"), catalog("CustomApplication", "Console")])
    assert refs(graph, "CustomApplication")[0]["target"] == node_id("CustomApplication", "pkg__Console")


def test_prompt_specific_permission_and_profile_filters_bind_only_documented_expressions():
    src = prompt("<userAccess>SpecificPermissions</userAccess><userProfileAccess>SpecificProfiles</userProfileAccess><uiFormulaRule><booleanFilter>1 AND (2 OR 3)</booleanFilter>" +
                 criterion("{!$Permission.CustomPermission.Can_View}") + criterion("{!$Permission.StandardPermission.ViewSetup}") +
                 criterion("{!ENCODED:{!ID:$User.Profile.Key}}", "Support Agent") + "</uiFormulaRule>")
    graph = build_graph([src, catalog("CustomPermission", "Can_View"), catalog("CustomPermission", "ViewSetup"), catalog("Profile", "Support Agent")])
    assert {(e["target_kind"], e["target_name"]) for e in graph["edges"]} == {("CustomPermission", "Can_View"), ("Profile", "Support Agent")}
    assert all(e["resolution"] == "resolved" for e in graph["edges"])
    assert level(graph, "Prompt") == "semantic"
    owner = next(n for n in graph["nodes"] if n["id"] == src.component_id)
    assert owner["standard_permission_filters"] == ["ViewSetup"]


@pytest.mark.parametrize("contents,code", [
    ("<userAccess>SpecificPermissions</userAccess>", "prompt_visibility_criteria_missing"),
    ("<userProfileAccess>SpecificProfiles</userProfileAccess>", "prompt_visibility_criteria_missing"),
    ("<targetAppNamespacePrefix>pkg</targetAppNamespacePrefix>", "prompt_application_context_missing"),
    ("<uiFormulaRule>" + criterion("{!$Permission.CustomPermission.Example}", operator="FUTURE") + "</uiFormulaRule>", "prompt_permission_operator_unsupported"),
    ("<uiFormulaRule>" + criterion("{!$User.Profile.Name}") + "</uiFormulaRule>", "prompt_permission_expression_unsupported"),
    ("<uiFormulaRule>" + criterion("{!$Permission.CustomPermission.Example}", right="false") + "</uiFormulaRule>", "prompt_permission_value_unsupported"),
])
def test_prompt_visibility_gaps_are_not_silently_marked_semantic(contents, code):
    graph = build_graph([prompt(contents), catalog("CustomPermission", "Example")])
    assert level(graph, "Prompt") == "partial" and code in codes(graph)


def clean_row(field, context, other_field, other_context):
    return f"""<fieldMappingRows><fieldName>{field}</fieldName><SObjectType>{context}</SObjectType>
      <fieldMappingFields><dataServiceField>{other_field}</dataServiceField><dataServiceObjectName>{other_context}</dataServiceObjectName><priority>1</priority></fieldMappingFields>
      <mappingOperation>Autofill</mappingOperation></fieldMappingRows>"""


def clean_mapping(name="Input", context="VirtualCompany", rows=None):
    if rows is None:
        rows = clean_row("CompanyName", "VirtualCompany", "Company", "Lead")
    return f"<fieldMappings><developerName>{name}</developerName><SObjectType>{context}</SObjectType>{rows}</fieldMappings>"


def clean_rule(name="Enrich", mappings=None):
    if mappings is None:
        mappings = clean_mapping() + clean_mapping("Output", "Lead", clean_row("AnnualRevenue", "Lead", "Revenue", "VirtualCompany"))
    return f"<cleanRules><developerName>{name}</developerName><sourceSobjectType>VirtualCompany</sourceSobjectType><targetSobjectType>Lead</targetSobjectType><matchRule>InternalRule</matchRule>{mappings}</cleanRules>"


def clean_source(rules=None):
    return source("CleanDataService", "<CleanDataService><matchEngine>InternalEngine</matchEngine>" + (clean_rule() if rules is None else rules) + "</CleanDataService>")


def test_clean_data_input_reads_output_writes_and_virtual_schema_never_binds_org_names():
    src = clean_source()
    graph = build_graph([src, catalog("CustomObject", "Lead"), catalog("CustomObject", "VirtualCompany"),
                         *[catalog("CustomField", name) for name in ("Lead.Company", "Lead.AnnualRevenue", "VirtualCompany.CompanyName", "VirtualCompany.Revenue")],
                         catalog("MatchingRule", "InternalRule")])
    assert {(e["relation"], e["target_name"]) for e in refs(graph, "CustomField")} == {("reads", "Lead.Company"), ("writes", "Lead.AnnualRevenue")}
    assert all(e["resolution"] == "resolved" for e in refs(graph, "CustomField"))
    assert {e["target_name"] for e in refs(graph, "CustomObject")} == {"Lead"}
    assert not refs(graph, "MatchingRule")
    virtual = refs(graph, "DataServiceObject")[0]
    assert virtual["resolution"] == "unresolved" and virtual["target_name"] == "Example:VirtualCompany"
    mappings = [n for n in graph["nodes"] if n["kind"] == "CleanDataMapping"]
    assert len(mappings) == 2 and all(n["component_id"] == src.component_id for n in mappings)
    assert {n["mapping_direction"] for n in mappings} == {"input", "output"}
    assert next(n for n in mappings if n["mapping_direction"] == "output")["data_service_fields"] == ["Revenue"]
    assert all(e["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest() for e in graph["edges"])
    assert "clean_data_virtual_schema_not_indexed" in codes(graph) and level(graph, "CleanDataService") == "partial"


def test_clean_data_display_label_does_not_fuzzy_bind_api_name():
    src = clean_source(clean_rule(mappings=clean_mapping("Output", "Lead", clean_row("Annual Revenue", "Lead", "Revenue", "VirtualCompany"))))
    graph = build_graph([src, catalog("CustomField", "Lead.AnnualRevenue")])
    edge = refs(graph, "CustomField")[0]
    assert edge["target_name"] == "Lead.Annual Revenue" and edge["resolution"] == "unresolved"
    assert "metadata_identity_unverified" in codes(graph)


@pytest.mark.parametrize("rules,code", [
    ("", "clean_data_rules_not_supplied"),
    (clean_rule() + clean_rule(), "clean_data_rule_context_ambiguous"),
    (clean_rule(mappings=""), "clean_data_mappings_missing"),
    (clean_rule(mappings=clean_mapping(rows="")), "clean_data_rows_missing"),
    (clean_rule(mappings=clean_mapping() + clean_mapping()), "clean_data_mapping_identity_ambiguous"),
    (clean_rule(mappings=clean_mapping(context="Account")), "clean_data_mapping_context_unverified"),
    (clean_rule(mappings=clean_mapping(rows=clean_row("Name", "Account", "Company", "Lead"))), "clean_data_field_context_ambiguous"),
    (clean_rule(mappings=clean_mapping(rows=clean_row("Name", "VirtualCompany", "Company", "Account"))), "clean_data_pair_context_unverified"),
    (clean_rule(mappings=clean_mapping(rows=clean_row("Name", "VirtualCompany", "Company", "Lead") * 2)), "clean_data_field_context_ambiguous"),
    (clean_rule(mappings=clean_mapping(rows="<fieldMappingRows><fieldName>Name</fieldName><SObjectType>VirtualCompany</SObjectType></fieldMappingRows>")), "clean_data_pairs_missing"),
])
def test_clean_data_missing_or_ambiguous_context_has_no_guessed_field_edges(rules, code):
    graph = build_graph([clean_source(rules), catalog("CustomField", "Lead.Company"), catalog("CustomField", "Account.Company")])
    assert code in codes(graph) and level(graph, "CleanDataService") == "partial"
    assert not refs(graph, "CustomField")


def test_clean_data_virtual_and_real_object_cannot_share_ambiguous_context():
    src = clean_source()
    graph = build_graph([source(src.metadata_type, src.content.replace("VirtualCompany", "Lead"))])
    assert not graph["edges"] and "clean_data_rule_context_ambiguous" in codes(graph)


@pytest.mark.parametrize("kind,xml", [
    ("Group", "<Group><name>Team {!Case.Fake__c}</name><doesIncludeBosses>false</doesIncludeBosses></Group>"),
    ("CampaignInfluenceModel", "<CampaignInfluenceModel><name>Model</name><isDefaultModel>false</isDefaultModel><isModelLocked>true</isModelLocked><recordPreference>AllRecords</recordPreference><modelDescription>{!Case.Fake__c}</modelDescription></CampaignInfluenceModel>"),
    ("CspTrustedSite", "<CspTrustedSite><endpointUrl>https://example.test/Account.Fake__c</endpointUrl><context>All</context><isActive>true</isActive><isApplicableToConnectSrc>true</isApplicableToConnectSrc></CspTrustedSite>"),
])
def test_literal_policy_definitions_can_legitimately_have_no_dependencies(kind, xml):
    graph = build_graph([source(kind, xml)])
    assert not graph["edges"] and level(graph, kind) == "semantic"
    if kind == "Group":
        assert graph["nodes"][0]["membership_analysis"] == "not_in_metadata"
        assert "group_members_not_in_metadata" in codes(graph)


@pytest.mark.parametrize("kind,xml", [
    ("Group", "<Group><name>Team</name></Group>"),
    ("Group", "<Group><name>Team</name><doesIncludeBosses>Future</doesIncludeBosses></Group>"),
    ("CampaignInfluenceModel", "<CampaignInfluenceModel><name>Model</name><isDefaultModel>false</isDefaultModel><isModelLocked>true</isModelLocked><recordPreference>Future</recordPreference></CampaignInfluenceModel>"),
    ("CspTrustedSite", "<CspTrustedSite><endpointUrl>https://example.test</endpointUrl><context>LightningOut</context></CspTrustedSite>"),
    ("CspTrustedSite", "<CspTrustedSite><context>All</context></CspTrustedSite>"),
])
def test_literal_policy_missing_required_or_future_values_are_partial(kind, xml):
    graph = build_graph([source(kind, xml)])
    assert level(graph, kind) == "partial" and not graph["edges"]
