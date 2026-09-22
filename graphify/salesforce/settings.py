"""Explicit Settings-root contracts, never a generic 'all booleans' shortcut.

Metadata API v68 documents these identity slots. Forecast list columns can be
report-style aliases: only verbatim qualified field paths or exact Salesforce
IDs are bound. We never rewrite CLOSE_DATE to CloseDate or guess an object for
an unqualified column. Unbound aliases retain their original evidence.
"""
from __future__ import annotations

import re

from .model import salesforce_id
from .setup import validate_literals


LIGHTNING_FLAGS = """enableAccessCheckCrucPref enableApiUserLtngOutAccessPref
enableAuraBoxcarReductionPref enableAuraCDNPref enableAuraDepAccessChksCRUCPref
enableAuraSecStaticResCRUCPref enableDeferRenderingWorkspacePage
enableErrorExperienceEnabled enableFeedbackInMobile enableGoogleSheetsForSfdcEnabled
enableIE11DeprecationMsgHidden enableIE11LEXCrucPref enableInAppLearning
enableInAppTooltips enableLEXOnIpadEnabled enableLexEndUsersNoSwitching
enableLightningPreviewPref enableNavPersonalizationOptOut enableNoBackgroundNavigations
enableQuip enableRemoveThemeBrandBanner enableS1BannerPref enableS1BrowserEnabled
enableS1DesktopEnabled enableS1UiLoggingEnabled enableSidToken3rdPartyAuraApp
enableSkypeChatEnabled enableSparkAllUsers enableSparkConversationEnabled
enableSplitViewOnStandard enableStackedModalManagerEnabled enableTryLightningOptOut
enableUseS1AlohaDesktop enableUsersAreLightningOnly enableWebExEnabled enableWebexAllUsers
isLEXExtensionComponentCustomizationOff isLEXExtensionDarkModeOff
isLEXExtensionLinkGrabberOff isLEXExtensionOff"""
SEARCH_FLAGS = """documentContentSearchEnabled enableAdvancedSearchInAlohaSidebar
enableEinsteinSearchAssistantDialog enableEinsteinSearchEs4kPilot
enableEinsteinSearchNaturalLanguage enableEinsteinSearchNLSFilters
enableEinsteinSearchPersonalization enablePersonalTagging enablePublicTagging
enableSalesforceGeneratedSynonyms enableSearchTermHistory enableSetupSearch
enableSuggestArticlesLinksOnly enableUseDefaultSearchEntity optimizeSearchForCJKEnabled
recentlyViewedUsersForBlankLookupEnabled sidebarAutoCompleteEnabled
sidebarDropDownListEnabled sidebarLimitToItemsIOwnCheckboxEnabled
singleSearchResultShortcutEnabled spellCorrectKnowledgeSearchEnabled enableQuerySuggestionPigOn"""
TERRITORY_FLAGS = """enableTerritoryManagement2 showTM2EnabledBanner
tm2BypassRealignAccInsert tm2EnableUserAssignmentLog"""
FORECAST_LISTS = ("forecastingObjectListLabelMappings", "forecastingObjectListSelectedSettings",
                  "forecastingObjectListUnselectedSettings")
OPPORTUNITY_LISTS = ("opportunityListFieldsLabelMappings", "opportunityListFieldsSelectedSettings",
                     "opportunityListFieldsUnselectedSettings")
SHAPES = {
    "LightningExperienceSettings": {"": "activeThemeName " + LIGHTNING_FLAGS},
    "IdentityProviderSettings": {"": "certificateName enableIdentityProvider"},
    "SearchSettings": {
        "": "searchSettingsByObject " + SEARCH_FLAGS,
        "searchSettingsByObject": "searchSettingsByObject",
        "searchSettingsByObject/searchSettingsByObject": "name enhancedLookupEnabled lookupAutoCompleteEnabled resultsPerPageCount",
    },
    "Territory2Settings": {
        "": "defaultAccountAccessLevel defaultCaseAccessLevel defaultContactAccessLevel defaultOpportunityAccessLevel opportunityFilterSettings supportedObjects t2ForecastAccessLevel " + TERRITORY_FLAGS,
        "opportunityFilterSettings": "apexClassName enableFilter runMultiThreaded runOnCreate",
        "supportedObjects": "defaultAccessLevel objectType state",
    },
    "ForecastingObjectListSettings": {
        "": "forecastingTypeObjectListSettings",
        "forecastingTypeObjectListSettings": "forecastingTypeDeveloperName " + " ".join(FORECAST_LISTS),
        **{"forecastingTypeObjectListSettings/" + tag: "field label" if "Label" in tag else "field" for tag in FORECAST_LISTS},
    },
    # This is deliberately a partial adapter: category, range, quota and legacy
    # contracts not implemented here are still reported by the shape validator.
    "ForecastingSettings": {
        "": "enableForecasts defaultToPersonalCurrency forecastingTypeSettings",
        "forecastingTypeSettings": "name active " + " ".join(OPPORTUNITY_LISTS),
        **{"forecastingTypeSettings/" + tag: "field label" if "Label" in tag else "field" for tag in OPPORTUNITY_LISTS},
    },
}


def parse_settings(facts, root, kind, *, issue, scalar, ref, children):
    def val(n):
        return n.text.strip() if n else ""

    def enum(n, allowed):
        if n and val(n) not in allowed:
            issue("settings_value_unsupported", n, property=n.tag)

    def boolean(n):
        enum(n, {"true", "false", "1", "0"})

    def named(n, target_kind, relation="configures", **attrs):
        if n is not None:
            if not val(n) or len(val(n)) > 1024:
                issue("metadata_reference_value_unsupported", n, property=n.tag)
            else:
                ref(n, target_kind, relation, identity_contract="settings_" + kind, **attrs)

    def bounded(n, tag, limit=2048):
        entries = children(n, tag)
        if len(entries) > limit:
            issue("settings_list_limit", n, property=tag)
            return []
        return entries

    repeated = tuple(path + "/field" for path in SHAPES[kind]
                     if path.endswith(("SelectedSettings", "UnselectedSettings")))
    validate_literals(root, SHAPES[kind], issue=issue, scalar=scalar, repeated=repeated)

    if kind == "LightningExperienceSettings":
        for tag in LIGHTNING_FLAGS.split():
            boolean(scalar(root, tag))
        named(scalar(root, "activeThemeName"), "LightningExperienceTheme", "activates")

    elif kind == "IdentityProviderSettings":
        enabled = scalar(root, "enableIdentityProvider", required=True)
        boolean(enabled)
        named(scalar(root, "certificateName", required=val(enabled) in {"true", "1"}), "Certificate", "uses_certificate")

    elif kind == "SearchSettings":
        for tag in SEARCH_FLAGS.split():
            boolean(scalar(root, tag))
        for group in bounded(root, "searchSettingsByObject", 1):
            for n in bounded(group, "searchSettingsByObject", 4096):
                named(scalar(n, "name", required=True), "CustomObject")
                boolean(scalar(n, "enhancedLookupEnabled"))
                boolean(scalar(n, "lookupAutoCompleteEnabled"))
                count = scalar(n, "resultsPerPageCount")
                if count and (not re.fullmatch(r"[0-9]{1,10}", val(count)) or int(val(count)) > 2147483647):
                    issue("settings_value_unsupported", count, property=count.tag)

    elif kind == "Territory2Settings":
        for tag in TERRITORY_FLAGS.split():
            boolean(scalar(root, tag))
        enum(scalar(root, "t2ForecastAccessLevel"), {"View", "Edit"})
        for group in bounded(root, "opportunityFilterSettings", 1):
            for tag in ("enableFilter", "runMultiThreaded", "runOnCreate"):
                boolean(scalar(group, tag))
            named(scalar(group, "apexClassName", required=val(scalar(group, "enableFilter")) in {"true", "1"}), "ApexClass", "executes")
        for n in bounded(root, "supportedObjects", 128):
            enum(scalar(n, "defaultAccessLevel", required=True), {"Read", "Edit", "Transfer", "All"})
            enum(scalar(n, "state", required=True), {"Disabled", "Enabled"})
            obj = scalar(n, "objectType", required=True)
            enum(obj, {"Lead"})
            if val(obj) == "Lead":
                named(obj, "CustomObject")

    elif kind in {"ForecastingObjectListSettings", "ForecastingSettings"}:
        group_tag, name_tag, lists = ("forecastingTypeObjectListSettings", "forecastingTypeDeveloperName", FORECAST_LISTS) if kind == "ForecastingObjectListSettings" else ("forecastingTypeSettings", "name", OPPORTUNITY_LISTS)
        if kind == "ForecastingSettings":
            boolean(scalar(root, "enableForecasts"))
            boolean(scalar(root, "defaultToPersonalCurrency"))
        for group in bounded(root, group_tag, 128):
            named(scalar(group, name_tag, required=True), "ForecastingType")
            if kind == "ForecastingSettings":
                boolean(scalar(group, "active", required=True))
            for tag in lists:
                for n in bounded(group, tag, 2048):
                    if "Label" in tag:
                        columns = [scalar(n, "field", required=True)]
                        scalar(n, "label", required=True)
                    else:
                        # Salesforce returns several <field> siblings in each
                        # selected/unselected container, not one scalar field.
                        columns = bounded(n, "field", 2048)
                    for column in columns:
                        if column is None:
                            continue
                        if column.children or not val(column):
                            issue("metadata_reference_value_missing", column, property="field")
                            continue
                        if val(column).startswith("00N") and salesforce_id(val(column)):
                            named(column, "CustomField", "references_field", target_salesforce_id=val(column))
                        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", val(column)):
                            named(column, "FieldPath", "references_field")
                        else:
                            issue("forecast_column_identity_unverified", column)
