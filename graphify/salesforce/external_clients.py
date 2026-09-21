"""External client app configuration contracts from Metadata API v68.

Credentials, PEM certificates, OAuth scope enums, URLs and runtime users are
not named metadata. AppMenu's explicit type/name pairs are useful evidence,
but its reserved/undocumented contract still prevents complete coverage.
"""
from __future__ import annotations

from collections import Counter
import re

from .model import salesforce_id


SHAPES = {
    "ExternalClientApplication": {"": "contactEmail contactPhone description distributionState iconUrl infoUrl isProtected label logoUrl managedType orgScopedExternalApp"},
    "ExtlClntAppConfigurablePolicies": {"": "externalClientApplication isCanvasPluginEnabled isEnabled isMobilePluginEnabled isNotificationPluginEnabled isOauthPluginEnabled isPushPluginEnabled isSamlPluginEnabled label startPage startUrl"},
    "ExtlClntAppGlobalOauthSettings": {
        "": "callbackUrl certificate consumerKey consumerSecret externalClientApplication idTokenConfig isClientCredentialsFlowEnabled isCodeCredFlowEnabled isCodeCredPostOnly isConsumerSecretOptional isDeviceFlowEnabled isIntrospectAllTokens isNamedUserJwtEnabled isPkceRequired isRefreshTokenRotationEnabled isSecretRequiredForRefreshToken isSecretRequiredForTokenExchange isTokenExchangeEnabled label shouldRotateConsumerKey shouldRotateConsumerSecret",
        "idTokenConfig": "idTokenAudience idTokenIncludeAttributes idTokenIncludeStandardClaims idTokenValidityInMinutes",
    },
    "ExtlClntAppOauthConfigurablePolicies": {
        "": "apexHandler clientCredentialsFlowUser commaSeparatedCustomScopes commaSeparatedPermissionSet commaSeparatedProfile customAttributes executeHandlerAs externalClientApplication guestJwtTimeout guestJwtSessionTimeoutType ipRelaxationPolicyType isClientCredentialsFlowEnabled isGuestCodeCredFlowEnabled isNamedUserJwtEnabled isTokenExchangeFlowEnabled label namedUserJwtTimeout namedUserJwtSessionTimeoutType permittedUsersPolicyType policyAction refreshTokenPolicyType refreshTokenValidityPeriod refreshTokenValidityUnit requiredSessionLevel sessionTimeoutInMinutes singleLogoutUrl startUrl",
        "customAttributes": "formula key",
    },
    "ExtlClntAppOauthSettings": {
        "": "areAttributesIncludedInAssetToken areCustomPermsIncludedInAssetToken assetTokenAudiences assetTokenSigningCertificate assetTokenValidity clientAssertionCertificate commaSeparatedOauthScopes customAttributes externalClientApplication isFirstPartyAppEnabled label oauthLink singleLogoutUrl trustedIpRanges",
        "customAttributes": "formula key",
        "trustedIpRanges": "description endIpAddress startIpAddress",
    },
    "AppMenu": {"": "appMenuItems", "appMenuItems": "name type"},
}

OAUTH_SCOPES = frozenset("Basic Api Web Full Chatter CustomApplications RefreshToken OpenID Profile Email Address Phone OfflineAccess CustomPermissions Wave Eclair Pardot Lightning Content CDPIngest CDPProfile CDPQuery Chatbot CDPSegment CDPIdentityResolution CDPCalculatedInsight SFApiPlatform Interaction EinsteinGPT PwdlessLogin ForgotPassword UserRegistration MCP SCRT".split())
APP_MENU_TYPES = frozenset({"ConnectedApp", "CustomApplication", "CustomTab", "ExternalClientApplication", "Network"})
FIELD_SELECTOR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
SCOPE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_external_client(facts, root, kind, *, issue, scalar, ref, children):
    def value(n):
        return n.text.strip() if n else ""

    def enum(n, allowed):
        if n and value(n) not in allowed:
            issue("external_client_value_unsupported", n, property=n.tag)

    def csv(n):
        if not n:
            return []
        if len(n.text) > 16384 or n.text.count(",") >= 128:
            issue("external_client_list_limit", n, property=n.tag)
            return []
        items = [part.strip() for part in n.text.split(",")]
        if any(not item or len(item) > 1024 for item in items):
            issue("external_client_list_invalid", n, property=n.tag)
            return []
        return list(dict.fromkeys(items))

    if kind == "AppMenu":
        # v68 calls AppMenu reserved. Extract only explicit, supported kinds
        # with independent in-scope declarations; never infer a kind by name.
        issue("app_menu_reserved_contract_partial")
        for item in children(root, "appMenuItems"):
            target = scalar(item, "name", required=True)
            target_kind = scalar(item, "type", required=True)
            if not target or not target_kind:
                continue
            if len(value(target)) > 1024:
                issue("metadata_reference_value_unsupported", target, property="name")
            elif value(target_kind) in APP_MENU_TYPES:
                ref(target, value(target_kind), "menu_item", identity_contract="app_menu_explicit_type_name")
            elif value(target_kind) != "StandardAppMenuItem":
                issue("app_menu_item_type_unsupported", target_kind)
        return

    repeated = {"customAttributes", "trustedIpRanges"}
    nested = repeated | {"idTokenConfig"}
    # Check all known scalar slots, not only reference slots. Duplicate/invalid
    # literal settings cannot silently upgrade a malformed file to semantic.
    for tag in SHAPES[kind][""].split():
        if tag in repeated:
            continue
        if tag in nested:
            if len(children(root, tag)) > 1:
                issue("metadata_reference_ambiguous_scalar", root, property=tag)
            continue
        n = scalar(root, tag)
        if n and tag in {"externalClientApplication", "apexHandler"} and len(value(n)) > 1024:
            issue("metadata_reference_value_unsupported", n, property=tag)
        if tag.startswith(("is", "are", "shouldRotate")):
            enum(n, {"true", "false", "1", "0"})

    if kind == "ExternalClientApplication":
        scalar(root, "label", required=True)
        enum(scalar(root, "distributionState"), {"AutoInstalled", "Local", "Managed", "Packaged"})
        # Internal/reserved fields have no documented metadata-identity schema.
        for tag in ("managedType", "iconUrl", "infoUrl"):
            if n := scalar(root, tag):
                issue("external_client_internal_property_unverified", n, property=tag)
        return

    ref(scalar(root, "externalClientApplication", required=True), "ExternalClientApplication", "configures",
        identity_contract="external_client_application_name")

    if kind == "ExtlClntAppConfigurablePolicies":
        enum(scalar(root, "isEnabled", required=True), {"true", "false", "1", "0"})
        enum(scalar(root, "startPage"), {"Custom", "None", "OAuth"})

    elif kind == "ExtlClntAppGlobalOauthSettings":
        for config in children(root, "idTokenConfig"):
            for tag in SHAPES[kind]["idTokenConfig"].split():
                n = scalar(config, tag)
                if tag in {"idTokenIncludeAttributes", "idTokenIncludeStandardClaims"}:
                    enum(n, {"true", "false", "1", "0"})
                elif n and tag == "idTokenValidityInMinutes" and not re.fullmatch(r"[+-]?[0-9]+", value(n)):
                    issue("external_client_value_unsupported", n, property=tag)
        # Nothing from consumerKey, consumerSecret or the PEM certificate is
        # copied into nodes, refs, diagnostics or reusable parser facts.

    elif kind == "ExtlClntAppOauthConfigurablePolicies":
        ref(scalar(root, "apexHandler"), "ApexClass", "auth_handler", identity_contract="external_client_apex_handler")
        scopes = scalar(root, "commaSeparatedCustomScopes")
        for name in csv(scopes):
            if not SCOPE_NAME.fullmatch(name):
                issue("external_client_scope_name_invalid", scopes)
            else:
                ref(scopes, "OauthCustomScope", "oauth_scope", name, identity_contract="external_client_custom_scope")
        permissions = scalar(root, "commaSeparatedPermissionSet")
        for token in csv(permissions):
            if not salesforce_id(token):
                issue("external_client_permission_set_id_invalid", permissions)
            else:
                ref(permissions, "PermissionSet", "configures_preauthorization", token,
                    target_salesforce_id=token, identity_contract="external_client_permission_set_id")
        # Profiles' selector encoding is unspecified. Users are runtime records,
        # not metadata; do not turn their names/IDs into Profile or User fields.
        for tag in ("commaSeparatedProfile", "clientCredentialsFlowUser", "executeHandlerAs"):
            if n := scalar(root, tag):
                issue("external_client_profile_selector_unverified" if tag == "commaSeparatedProfile"
                      else "external_client_runtime_user_not_indexed", n, property=tag)
        enums = {
            "ipRelaxationPolicyType": {"Enforce", "Bypass", "Bypass_2factor", "Enforce_RelaxRefresh"},
            "permittedUsersPolicyType": {"AdminApprovedPreAuthorized", "AllSelfAuthorized"},
            "policyAction": {"Block", "RaiseSessionLevel"},
            "refreshTokenPolicyType": {"Infinite", "SpecificInactivity", "SpecificLifetime", "Zero"},
            "refreshTokenValidityUnit": {"Days", "Hours", "Months"},
            "requiredSessionLevel": {"HIGH_ASSURANCE", "LOW", "STANDARD"},
            "guestJwtSessionTimeoutType": {"UserSession", "Custom"},
            "namedUserJwtSessionTimeoutType": {"UserSession", "Custom"},
        }
        for tag, allowed in enums.items():
            enum(scalar(root, tag), allowed)
        for tag in ("guestJwtTimeout", "namedUserJwtTimeout", "refreshTokenValidityPeriod", "sessionTimeoutInMinutes"):
            if n := scalar(root, tag):
                if not re.fullmatch(r"[+-]?[0-9]+", value(n)):
                    issue("external_client_value_unsupported", n, property=tag)

    elif kind == "ExtlClntAppOauthSettings":
        cert = scalar(root, "assetTokenSigningCertificate")
        if cert:
            if salesforce_id(value(cert)):
                ref(cert, "Certificate", "signs_asset_token", target_salesforce_id=value(cert),
                    identity_contract="external_client_signing_certificate_id")
            else:
                issue("external_client_certificate_id_invalid", cert)
        if n := scalar(root, "clientAssertionCertificate"):
            # The guide doesn't specify name vs ID vs certificate bytes. Never
            # expose these bytes in a speculative graph node or diagnostic.
            issue("external_client_assertion_certificate_unverified", n)
        scopes = scalar(root, "commaSeparatedOauthScopes")
        if any(token not in OAUTH_SCOPES for token in csv(scopes)):
            issue("external_client_oauth_scope_unsupported", scopes)
        if n := scalar(root, "assetTokenValidity"):
            if not re.fullmatch(r"[+-]?[0-9]+", value(n)):
                issue("external_client_value_unsupported", n, property=n.tag)
        ranges = children(root, "trustedIpRanges")
        if len(ranges) > 128:
            issue("external_client_list_limit", root, property="trustedIpRanges")
        for item in ranges[:128]:
            for tag in ("description", "startIpAddress", "endIpAddress"):
                scalar(item, tag, required=tag != "description")

    attributes = children(root, "customAttributes")
    if len(attributes) > 128:
        issue("external_client_list_limit", root, property="customAttributes")
    names = Counter(item.value("key") for item in attributes)
    for item in attributes[:128]:
        key = scalar(item, "key", required=True)
        formula = scalar(item, "formula", required=True)
        if not key or not formula:
            continue
        if names[value(key)] != 1:
            issue("external_client_attribute_key_ambiguous", key)
        elif len(value(formula)) > 1024 or not FIELD_SELECTOR.fullmatch(value(formula)):
            # v68 documents an existing qualified field, not arbitrary formula
            # syntax. Preserve the gap instead of tokenizing unverified text.
            issue("external_client_attribute_expression_unsupported", formula)
        else:
            ref(formula, "FieldPath", "reads", identity_contract="external_client_attribute_field")
