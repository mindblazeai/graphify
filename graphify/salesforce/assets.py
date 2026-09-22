"""Asset envelopes, theme references, picklist data and notification actions.

An understood XML envelope is not proof that a binary/code payload was indexed.
Contracts are from the Salesforce Metadata API v68 field tables.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

BRAND_IMAGES = frozenset("BANNER_IMAGE BRAND_IMAGE GROUP_IMAGE GROUPS_BANNER_IMAGE PROFILE_BANNER_IMAGE USER_IMAGE".split())
BRAND_LITERALS = frozenset("ACCENT_COLOR_1 ACCENT_COLOR_2 ACCENT_COLOR_3 ACCENT_CONTAINER_CONTENT_COLOR_1 ACCENT_CONTAINER_CONTENT_COLOR_2 ACCENT_CONTAINER_CONTENT_COLOR_3 BRAND_COLOR CONTAINER_ACCENT_COLOR_1 CONTAINER_ACCENT_COLOR_2 CONTAINER_ACCENT_COLOR_3 HEADER_BACKGROUND_COLOR LINK_AS_BACKGROUND OVERRIDE_A11Y_COLOR OVERRIDE_LOADING_PAGE PAGE_BACKGROUND_COLOR".split())
SHAPES = {
    "ContentAsset": {
        "": "content format isVisibleByExternalUsers language masterLabel originNetwork relationships versions",
        "relationships": "emailTemplate insightsApplication network organization workspace",
        **{"relationships/" + tag: "access isManagingWorkspace name" for tag in ("emailTemplate", "insightsApplication", "network", "organization", "workspace")},
        "versions": "version", "versions/version": "number pathOnClient zipEntry",
    },
    "Document": {"": "content description internalUseOnly keywords name public"},
    "StaticResource": {"": "cacheControl content contentType description"},
    "GlobalValueSet": {"": "customValue description masterLabel sorted", "customValue": "color default description fullName isActive label"},
    "RemoteSiteSetting": {"": "description disableProtocolSecurity isActive url"},
    "LightningExperienceTheme": {"": "defaultBrandingSet description designSystemVersion isDarkModeEnabled masterLabel shouldOverrideLoadingImage"},
    "BrandingSet": {"": "brandingSetProperty description masterLabel type", "brandingSetProperty": "propertyName propertyValue"},
    "CustomNotificationType": {
        "": "actionGroups customNotifTypeName description desktop masterLabel mobile slack",
        "actionGroups": "actions groupName",
        "actionGroups/actions": "actionLabel actionName actionTarget actionType",
    },
}


def parse_asset(facts, root, kind, *, issue, scalar, ref, children):
    owner = facts.source.component_id
    if kind in {"ContentAsset", "Document", "StaticResource"}:
        facts.nodes[owner]["content_analysis"] = "not_parsed"
        issue("asset_payload_not_analyzed")
    if kind == "StaticResource" and facts.source.path.endswith(".resource-meta.xml"):
        # Metadata API describeValueType(StaticResource), v67: both fields are
        # required; cacheControl is exactly Private/Public. A valid payload
        # cannot excuse an invalid or conflicting metadata envelope.
        cache = scalar(root, "cacheControl", required=True)
        if cache and cache.text.strip() not in {"Private", "Public"}:
            issue("asset_cache_control_invalid", cache)
        mime = scalar(root, "contentType", required=True)
        scalar(root, "description")
        name = scalar(root, "fullName", required=bool(children(root, "fullName")))
        if name and name.text.strip() != facts.source.full_name:
            issue("asset_descriptor_identity_mismatch", name)
        if children(root, "content"):
            # A second inline base64 payload needs its own byte comparison.
            issue("asset_inline_content_unverified")
        facts.asset_descriptor = {"payload_path": facts.source.path.removesuffix("-meta.xml"),
                                  "content_type": mime.text.strip().casefold() if mime else ""}
    if kind == "ContentAsset":
        from .content_assets import parse_content_asset
        parse_content_asset(facts, root, issue=issue, scalar=scalar, ref=ref, children=children)
    elif kind == "Document":
        if facts.source.path.endswith("-meta.xml"):
            for tag in ("internalUseOnly", "public"):
                value = scalar(root, tag, required=True)
                if value and value.text.strip() not in {"true", "false", "1", "0"}:
                    issue("document_boolean_invalid", value, property=tag)
            for tag in ("description", "keywords", "name"):
                scalar(root, tag)
            explicit = scalar(root, "fullName", required=bool(children(root, "fullName")))
            if explicit and explicit.text.strip() != facts.source.full_name:
                issue("asset_descriptor_identity_mismatch", explicit)
            if children(root, "content"):
                issue("asset_inline_content_unverified")
            from .image_payloads import IMAGE_EXTENSIONS, IMAGE_MIME_TYPES
            path = facts.source.path.removesuffix("-meta.xml")
            image_format = IMAGE_EXTENSIONS.get(PurePosixPath(path).suffix.lower())
            facts.asset_descriptor = {"payload_path": path,
                                      "content_type": next(iter(IMAGE_MIME_TYPES[image_format])) if image_format else ""}
        folder, slash, _ = facts.source.full_name.rpartition("/")
        if slash and folder:
            ref(root, "DocumentFolder", "belongs_to", name=folder, identity_contract="document_folder")
        else:
            issue("document_folder_context_missing")
    elif kind == "GlobalValueSet":
        values = children(root, "customValue")
        if not values:
            issue("value_set_values_missing")
        for value in values:
            scalar(value, "fullName", required=True)
        # Picklist values, labels, descriptions and colors are data, not formulas.
    elif kind == "RemoteSiteSetting":
        scalar(root, "url", required=True)
        # An allowlisted URL is not proof that any Apex callout uses it.
    elif kind == "LightningExperienceTheme":
        branding = scalar(root, "defaultBrandingSet", required=True)
        if branding:
            # The field table says ID and the provider's XML example uses a
            # developer name. Resolve both against real typed declarations.
            ref(branding, "BrandingSet", metadata_name_or_id=branding.text.strip(), identity_contract="theme_branding")
    elif kind == "BrandingSet":
        for prop in children(root, "brandingSetProperty"):
            name = scalar(prop, "propertyName", required=True)
            value = scalar(prop, "propertyValue")
            if not name:
                continue
            slot = name.text.strip()
            if slot in BRAND_IMAGES:
                # Salesforce's documented file-asset route carries an API
                # name, not pathOnClient or a filename. Only local routes are
                # normalized: an absolute host or oid could target another org.
                raw = value.text.strip() if value else ""
                asset = re.fullmatch(r"/file-asset/([A-Za-z_][A-Za-z0-9_]*)(?:\?v=([1-9][0-9]*))?", raw)
                if asset:
                    ref(value, "ContentAsset", name=asset[1], identity_contract="branding_asset",
                        asset_reference=raw, asset_version=asset[2])
                else:
                    ref(value, "ContentAsset", identity_contract="branding_asset")
            elif slot not in BRAND_LITERALS:
                issue("branding_property_semantics_unverified", name, property=slot)
        if typ := scalar(root, "type"):
            # Experience Builder definition identities aren't Aura bundle names.
            issue("branding_definition_identity_unverified", typ)
    elif kind == "CustomNotificationType":
        for group in children(root, "actionGroups"):
            for action in children(group, "actions"):
                mode = scalar(action, "actionType", required=True)
                target = scalar(action, "actionTarget")
                if mode and mode.text.strip() == "NotificationApiAction":
                    ref(target, "ApexClass", "invokes")
                    if target is None:
                        issue("notification_action_target_missing", action)
                elif mode and mode.text.strip() == "Share":
                    if target:
                        issue("notification_action_context_conflict", target)
                elif mode:
                    issue("notification_action_type_unsupported", mode)
