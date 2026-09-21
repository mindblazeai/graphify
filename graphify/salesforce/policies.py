"""Explicit metadata policy, notification, prompt and cleaning contracts (v68).

Virtual data-service fields, provider notifications and standard permissions
are not silently reinterpreted as custom fields/notifications/permissions.
"""
from __future__ import annotations

from collections import Counter
import re

from .rule_expressions import restriction_expression

SHAPES = {
    "FieldRestrictionRule": {"": "active classification classificationType description enforcementType masterLabel recordFilter targetEntity userCriteria version"},
    "NotificationTypeConfig": {
        "": "notificationTypeSettings",
        "notificationTypeSettings": "appSettings notificationChannels notificationType",
        "notificationTypeSettings/appSettings": "connectedAppName enabled",
        "notificationTypeSettings/notificationChannels": "desktopEnabled mobileEnabled slackEnabled",
    },
    "Prompt": {
        "": "masterLabel promptVersions",
        "promptVersions": "actionButtonLabel actionButtonLink body customApplication delayDays description dismissButtonLabel displayPosition displayType elementRelativePosition endDate header image imageAltText imageLink imageLocation indexWithIsPublished indexWithoutIsPublished isPublished masterLabel publishedByUser publishedDate referenceElementContext shouldDisplayActionButton shouldIgnoreGlobalDelay startDate stepNumber targetAppDeveloperName targetAppNamespacePrefix targetPageKey1 targetPageKey2 targetPageKey3 targetPageKey4 targetPageType targetRecordType themeColor themeSaturation timesToDisplay title uiFormulaRule userAccess userProfileAccess versionNumber videoLink",
        "promptVersions/uiFormulaRule": "booleanFilter criteria",
        "promptVersions/uiFormulaRule/criteria": "leftValue operator rightValue",
    },
    "CleanDataService": {
        "": "cleanRules description masterLabel matchEngine",
        "cleanRules": "bulkEnabled bypassTriggers bypassWorkflow description developerName fieldMappings masterLabel matchRule sourceSobjectType status targetSobjectType",
        "cleanRules/fieldMappings": "developerName fieldMappingRows masterLabel SObjectType",
        "cleanRules/fieldMappings/fieldMappingRows": "fieldName fieldMappingFields mappingOperation SObjectType",
        "cleanRules/fieldMappings/fieldMappingRows/fieldMappingFields": "dataServiceField dataServiceObjectName priority",
    },
    "Group": {"": "description doesIncludeBosses name"},
    "CampaignInfluenceModel": {"": "isActive isDefaultModel isModelLocked modelDescription name recordPreference"},
    "CspTrustedSite": {"": "canAccessCamera canAccessMicrophone context description endpointUrl isActive isApplicableToConnectSrc isApplicableToFontSrc isApplicableToFrameSrc isApplicableToImgSrc isApplicableToMediaSrc isApplicableToStyleSrc"},
}


def parse_policy(facts, root, kind, *, issue, scalar, ref, children):
    owner = facts.source.component_id

    def value(n):
        return n.text.strip() if n else ""

    def enum(n, allowed):
        if n and value(n) not in allowed:
            issue("metadata_policy_value_unsupported", n, property=n.tag)

    def member(n, member_kind, name, parent=owner):
        nid = facts.declare(member_kind, name, n.line)
        facts.ref(parent, member_kind, name, "contains", n.line)
        return nid

    if kind == "FieldRestrictionRule":
        target = scalar(root, "targetEntity", required=True)
        if value(target) not in {"User", "Employee"}:
            issue("restriction_object_unsupported", target or root)
            return
        obj = value(target)
        ref(target, "CustomObject", "configures_access", identity_contract="restriction_object")
        enum(scalar(root, "enforcementType", required=True), {"FieldRestrict"})
        mode_node = scalar(root, "classificationType")
        mode = value(mode_node) or "ComplianceCategory"
        if mode not in {"FieldSet", "ComplianceCategory"}:
            issue("restriction_classification_unsupported", mode_node or root)
        classifications = children(root, "classification")
        if not classifications:
            issue("restriction_classification_missing")
        for item in classifications:
            if not value(item) or item.children:
                issue("metadata_reference_value_missing", item, property=item.tag)
            elif mode == "FieldSet":
                name = value(item)
                if "." in name and not name.casefold().startswith(obj.casefold() + "."):
                    issue("restriction_field_set_context_conflict", item)
                else:
                    ref(item, "FieldSet", "configures_access", name if "." in name else obj + "." + name,
                        identity_contract="restriction_field_set")
        restriction_expression(facts, scalar(root, "recordFilter", required=True), obj, issue=issue)
        restriction_expression(facts, scalar(root, "userCriteria", required=True), "", issue=issue)

    elif kind == "NotificationTypeConfig":
        settings = children(root, "notificationTypeSettings")
        counts = Counter(item.value("notificationType").casefold() for item in settings)
        for item in settings:
            notification = scalar(item, "notificationType", required=True)
            if not notification:
                continue
            if counts[value(notification).casefold()] != 1:
                issue("notification_type_settings_duplicate", notification)
                continue
            name = facts.source.full_name + ":" + value(notification)
            mid = member(item, "NotificationDeliverySetting", name)
            ref(notification, "NotificationType", "configures", source=mid, identity_contract="notification_type")
            for app in children(item, "appSettings"):
                target = scalar(app, "connectedAppName", required=True)
                enabled = scalar(app, "enabled")
                enum(enabled, {"true", "false"})
                ref(target, "NotificationApp", "configures_delivery", source=mid,
                    enabled=value(enabled) == "true" if enabled else None, identity_contract="notification_app")
            for channels in children(item, "notificationChannels"):
                for channel in channels.children:
                    enum(channel, {"true", "false"})

    elif kind == "Prompt":
        versions = children(root, "promptVersions")
        if not versions:
            issue("prompt_versions_missing")
        for version in versions:
            image = scalar(version, "image")
            if image and (scalar(version, "imageLink") or scalar(version, "videoLink")):
                issue("prompt_media_context_conflict", image)
            else:
                ref(image, "ContentAsset", identity_contract="prompt_image")
            # Internal fields and page keys don't provide a documented metadata
            # identity, even if their text resembles an existing app or record.
            for tag in ("customApplication", "publishedByUser", "referenceElementContext", "targetRecordType"):
                if n := scalar(version, tag):
                    issue("prompt_identity_context_unverified", n, property=tag)
            for tag in ("targetPageKey1", "targetPageKey2", "targetPageKey3", "targetPageKey4", "targetPageType"):
                if n := scalar(version, tag):
                    issue("prompt_page_identity_unverified", n, property=tag)
            app = scalar(version, "targetAppDeveloperName")
            namespace = scalar(version, "targetAppNamespacePrefix")
            if app:
                name = value(app)
                if namespace:
                    name = value(namespace) + "__" + name
                ref(app, "CustomApplication", name=name, identity_contract="prompt_app")
            elif namespace:
                issue("prompt_application_context_missing", namespace)
            enum(scalar(version, "userAccess"), {"Everyone", "SpecificPermissions"})
            enum(scalar(version, "userProfileAccess"), {"Everyone", "SpecificProfiles"})
            for rule in children(version, "uiFormulaRule"):
                # Boolean filter numbers combine criteria; they aren't fields.
                for criterion in children(rule, "criteria"):
                    left = scalar(criterion, "leftValue", required=True)
                    right = scalar(criterion, "rightValue", required=True)
                    operator = scalar(criterion, "operator", required=True)
                    if value(operator) != "EQUAL":
                        issue("prompt_permission_operator_unsupported", operator or criterion)
                        continue
                    permission = re.fullmatch(r"\{!\$Permission\.(CustomPermission|StandardPermission)\.([A-Za-z_][A-Za-z0-9_]*)\}", value(left))
                    if permission:
                        if value(right).casefold() != "true":
                            issue("prompt_permission_value_unsupported", right or criterion)
                        if permission[1] == "CustomPermission":
                            ref(left, "CustomPermission", "checks_permission", permission[2], identity_contract="prompt_permission")
                        else:
                            facts.nodes[owner].setdefault("standard_permission_filters", []).append(permission[2])
                    elif value(left) == "{!ENCODED:{!ID:$User.Profile.Key}}":
                        ref(right, "Profile", "checks_profile", identity_contract="prompt_profile")
                    else:
                        issue("prompt_permission_expression_unsupported", left or criterion)
            if value(scalar(version, "userAccess")) == "SpecificPermissions" or value(scalar(version, "userProfileAccess")) == "SpecificProfiles":
                if not any(children(rule, "criteria") for rule in children(version, "uiFormulaRule")):
                    issue("prompt_visibility_criteria_missing", version)

    elif kind == "CleanDataService":
        rules = children(root, "cleanRules")
        if not rules:
            issue("clean_data_rules_not_supplied")
        names = Counter(rule.value("developerName").casefold() for rule in rules)
        for rule in rules:
            rule_name = scalar(rule, "developerName", required=True)
            source = scalar(rule, "sourceSobjectType", required=True)
            target = scalar(rule, "targetSobjectType", required=True)
            if not rule_name or not source or not target:
                continue
            if names[value(rule_name).casefold()] != 1 or value(source).casefold() == value(target).casefold():
                issue("clean_data_rule_context_ambiguous", rule)
                continue
            rule_full = facts.source.full_name + ":" + value(rule_name)
            rid = member(rule, "CleanDataRule", rule_full)
            ref(target, "CustomObject", "configures_updates", source=rid, identity_contract="clean_data_object")
            # This is explicitly a virtual service object, not an org sObject.
            ref(source, "DataServiceObject", "data_service_source", facts.source.full_name + ":" + value(source), rid)
            issue("clean_data_virtual_schema_not_indexed", source)
            mappings = children(rule, "fieldMappings")
            if not mappings:
                issue("clean_data_mappings_missing", rule)
            mapping_names = Counter(m.value("developerName").casefold() for m in mappings)
            for mapping in mappings:
                mapping_name = scalar(mapping, "developerName", required=True)
                context = scalar(mapping, "SObjectType", required=True)
                if not mapping_name or not context:
                    continue
                if mapping_names[value(mapping_name).casefold()] != 1:
                    issue("clean_data_mapping_identity_ambiguous", mapping_name)
                    continue
                direction = "input" if value(context).casefold() == value(source).casefold() else "output" if value(context).casefold() == value(target).casefold() else ""
                if not direction:
                    issue("clean_data_mapping_context_unverified", context)
                    continue
                rows = children(mapping, "fieldMappingRows")
                if not rows:
                    issue("clean_data_rows_missing", mapping)
                row_names = Counter(r.value("fieldName").casefold() for r in rows)
                for row in rows:
                    field = scalar(row, "fieldName", required=True)
                    row_context = scalar(row, "SObjectType", required=True)
                    if not field or not row_context:
                        continue
                    if value(row_context).casefold() != value(context).casefold() or row_names[value(field).casefold()] != 1:
                        issue("clean_data_field_context_ambiguous", row)
                        continue
                    enum(scalar(row, "mappingOperation"), {"AutoFill", "Autofill"})
                    mid = member(row, "CleanDataMapping", rule_full + ":" + value(mapping_name) + ":" + value(field), rid)
                    facts.nodes[mid].update(mapping_direction=direction, mapping_object=value(context),
                                            mapping_field=value(field), data_service_object=value(source))
                    # The official input/output examples use API names here.
                    # Bind only exact independent declarations, never labels.
                    if direction == "output":
                        ref(field, "CustomField", "writes", value(target) + "." + value(field), mid,
                            identity_contract="clean_data_field")
                    pairs = children(row, "fieldMappingFields")
                    if not pairs:
                        issue("clean_data_pairs_missing", row)
                    for pair in pairs:
                        other_field = scalar(pair, "dataServiceField", required=True)
                        other_object = scalar(pair, "dataServiceObjectName", required=True)
                        expected = value(target) if direction == "input" else value(source)
                        if not other_field or not other_object:
                            continue
                        if value(other_object).casefold() != expected.casefold():
                            issue("clean_data_pair_context_unverified", other_object)
                        elif direction == "input":
                            ref(other_field, "CustomField", "reads", value(target) + "." + value(other_field), mid,
                                identity_contract="clean_data_field")
                        else:
                            facts.nodes[mid].setdefault("data_service_fields", []).append(value(other_field))
                    # Service field names stay context, not Salesforce fields.

    elif kind == "Group":
        scalar(root, "name", required=True)
        enum(scalar(root, "doesIncludeBosses", required=True), {"true", "false"})
        facts.nodes[owner]["membership_analysis"] = "not_in_metadata"
        facts.issue("group_members_not_in_metadata", root.line)

    elif kind == "CampaignInfluenceModel":
        scalar(root, "name", required=True)
        enum(scalar(root, "recordPreference"), {"AllRecords", "RecordsWithAttribution"})
        for tag in ("isActive", "isDefaultModel", "isModelLocked"):
            enum(scalar(root, tag, required=tag != "isActive"), {"true", "false"})

    elif kind == "CspTrustedSite":
        scalar(root, "endpointUrl", required=True)
        enum(scalar(root, "context"), {"All", "Communities", "FieldServiceMobileExtension", "LEX", "VisualForce"})
        for child in root.children:
            if child.tag.startswith(("is", "canAccess")):
                enum(child, {"true", "false"})
        # URLs, labels and policy switches aren't named Salesforce components.
