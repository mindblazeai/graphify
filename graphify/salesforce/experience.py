"""Experience Cloud's explicit metadata slots (Metadata API guide v68).

Site names in moderation/criteria fullNames are Network names, not CustomSite
developer names. Network.site and picassoSite supply those separate identities.
"""
from __future__ import annotations

import re


PAGE_FIELDS = "authorizationRequiredPage bandwidthExceededPage changePasswordPage chatterAnswersForgotPasswordConfirmPage chatterAnswersForgotPasswordPage chatterAnswersHelpPage chatterAnswersLoginPage chatterAnswersRegistrationPage fileNotFoundPage forgotPasswordPage genericErrorPage inMaintenancePage inactiveIndexPage indexPage myProfilePage robotsTxtPage selfRegPage siteTemplate".split()
EMAIL_FIELDS = "caseCommentEmailTemplate changePasswordTemplate chgEmailVerNewTemplate chgEmailVerOldTemplate forgotPasswordTemplate headlessForgotPasswordTemplate headlessRegistrationTemplate lockoutTemplate pwdlessRegEmailTemplate selfRegMicroBatchSubErrorEmailTemplate verificationTemplate welcomeTemplate".split()
NETWORK_WSDL_FLAGS = ("enableExpFriendlyUrlsAsDefault", "enableLWRExperienceConnectedApp")
MENU_ITEM_FIELDS = "defaultListViewId label menuItemBranding position publiclyAvailable subMenu target targetPreference type"

SHAPES = {
    "CustomSite": {
        "": " ".join(PAGE_FIELDS) + " active allowGuestPaymentsApi allowHomePage allowStandardAnswersPages allowStandardIdeasPages allowStandardLookups allowStandardPortalPages allowStandardSearch analyticsTrackingCode browserXssProtection cachePublicVisualforcePagesInProxyServers clickjackProtectionLevel contentSniffingProtection cspUpgradeInsecureRequests customWebAddresses description enableAuraRequests favoriteIcon guestProfile masterLabel portal redirectToCustomDomain referrerPolicyOriginWhenCrossOrigin requireHttps requireInsecurePortalAccess serverIsDown siteAdmin siteGuestRecordDefaultOwner siteIframeWhiteListUrls siteRedirectMappings siteType subdomain urlPathPrefix",
        "customWebAddresses": "certificate domainName primary",
        "siteIframeWhiteListUrls": "url",
        "siteRedirectMappings": "action isActive source target",
    },
    "Network": {
        "": " ".join(EMAIL_FIELDS) + " allowedExtensions allowInternalUserLogin allowMembersToFlag communityRoles description deviceActEmailTemplate disablePvtPagesForPwdResetInLWR disableReputationRecordConversations emailFooterLogo emailFooterText emailSenderAddress emailSenderName embeddedLoginEnabled enableApexCDNCaching enableCustomVFErrorPageOverrides enableDirectMessages enableExperienceBundleBasedSnaOverrideEnabled enableGuestChatter enableGuestFileAccess enableGuestMemberVisibility enableImageOptimizationCDN enableInvitation enableKnowledgeable enableMemberVisibility enableNicknameDisplay enablePrivateMessages enableReputation enableShowAllNetworkSettings enableSiteAsContainer enableTalkingAboutStats enableTopicAssignmentRules enableTopicSuggestions enableUpDownVote expFriendlyUrlsAsDefault feedChannel gatherCustomerSentimentData logoutUrl maxFileSizeKb navigationLinkSet networkMemberGroups networkOptionsBit networkPageOverrides newSenderAddress pendingSecondaryEmailAddress picassoSite secondaryEmailSenderEnabled selfRegProfile selfRegistration sendWelcomeEmail site siteArchiveStatus status tabs urlPathPrefix",
        "communityRoles": "customerUserRole employeeUserRole partnerUserRole",
        "networkMemberGroups": "permissionSet profile",
        "networkPageOverrides": "changePasswordPageOverrideSetting forgotPasswordPageOverrideSetting homePageOverrideSetting loginPageOverrideSetting selfRegProfilePageOverrideSetting",
        "tabs": "customTab defaultTab standardTab",
        "navigationLinkSet": "navigationMenuItem",
        "navigationLinkSet/navigationMenuItem": MENU_ITEM_FIELDS,
        "navigationLinkSet/navigationMenuItem/menuItemBranding": "tileImage",
        "navigationLinkSet/navigationMenuItem/subMenu": "navigationMenuItem",
    },
    "NavigationMenu": {
        "": "container containerType label navigationMenuItem",
        "navigationMenuItem": MENU_ITEM_FIELDS,
        "navigationMenuItem/menuItemBranding": "tileImage",
        "navigationMenuItem/subMenu": "navigationMenuItem",
    },
    "KeywordList": {"": "description masterLabel keywords", "keywords": "keyword"},
    "UserCriteria": {"": "creationAgeInSeconds description lastChatterActivityAgeInSeconds masterLabel userTypes"},
    "PresenceUserConfig": {
        "": "acwExtensionDuration afterConvoWorkMaxTime assignments capacity declineReasons enableAutoAccept enableDecline enableDeclineReason enableDisconnectSound enableRequestSound hasAcwExtensionEnabled hasAfterConvoWorkTimer interruptibleCapacity label maxExtensions presenceStatusOnDecline presenceStatusOnPushTimeout",
        "assignments": "profiles users", "assignments/profiles": "profile", "assignments/users": "user",
    },
}
SHAPES["Network"][""] += " " + " ".join(NETWORK_WSDL_FLAGS)


def shape_path(kind, path):
    # Recursive submenu schema, without accepting unrelated unknown branches.
    return path.replace("/subMenu/navigationMenuItem", "") if kind in {"Network", "NavigationMenu"} else path


def site_reference(facts, root, *, issue, ref):
    site, dot, member = facts.source.full_name.rpartition(".")
    if not dot or not site or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", member):
        issue("experience_site_context_missing", root)
        return
    # Network's file name is the Experience Cloud site's name. Do not turn
    # spaces into underscores or append '1' to guess a CustomSite/SiteDotCom.
    ref(root, "Network", "belongs_to", name=site)


def parse_experience(facts, root, kind, *, issue, scalar, ref, children):
    owner = facts.source.component_id

    def user(n):
        if n is not None:
            ref(n, "SalesforceUser")
            issue("metadata_user_record_not_indexed", n)

    def menu_items(container):
        pending = [(item, False) for item in children(container, "navigationMenuItem")]
        while pending:
            item, nested = pending.pop()
            typ = scalar(item, "type", required=True)
            mode = typ.text.strip() if typ else ""
            if nested and mode in {"MenuLabel", "NavigationalTopic"}:
                issue("navigation_submenu_context_invalid", typ)
                continue
            target = scalar(item, "target", required=mode in {"SalesforceObject", "InternalLink", "ExternalLink"})
            view = scalar(item, "defaultListViewId")
            if mode == "SalesforceObject":
                ref(target, "CustomObject", "references_object")
                if view and target:
                    ref(view, "ListView", target_salesforce_id=view.text.strip(), target_object=target.text.strip())
            elif mode == "InternalLink" and target:
                # A route can be dynamic or unavailable with the site bundle.
                # Never turn '/contactsupport' into a guessed page/API name.
                issue("navigation_internal_route_unresolved", target)
            elif mode == "NavigationalTopic":
                # Its target is ignored by Salesforce, not a named Topic.
                issue("navigation_topic_records_not_indexed", typ)
            elif mode not in {"ExternalLink", "MenuLabel", ""}:
                issue("navigation_item_type_unsupported", typ)
            if view and mode != "SalesforceObject":
                issue("navigation_list_view_context_missing", view)
            for branding in children(item, "menuItemBranding"):
                ref(scalar(branding, "tileImage"), "ContentAsset")
            for submenu in children(item, "subMenu"):
                if mode != "MenuLabel":
                    issue("navigation_submenu_context_invalid", submenu)
                else:
                    pending.extend((child, True) for child in children(submenu, "navigationMenuItem"))

    if kind == "CustomSite":
        for tag in PAGE_FIELDS:
            ref(scalar(root, tag, required=tag == "indexPage"), "ApexPage")
        for tag in ("favoriteIcon", "serverIsDown"):
            ref(scalar(root, tag), "StaticResource")
        ref(scalar(root, "guestProfile"), "Profile")
        for tag in ("siteAdmin", "siteGuestRecordDefaultOwner"):
            user(scalar(root, tag))
        for address in children(root, "customWebAddresses"):
            ref(scalar(address, "certificate"), "Certificate")
        portal = scalar(root, "portal")
        if portal:
            ref(portal, "SalesforcePortal")
            issue("site_portal_identity_unverified", portal)

    elif kind == "Network":
        # Salesforce's version-pinned WSDL includes these optional booleans,
        # although its prose field table omits them (sf-skills c217b703b3e5).
        for tag in NETWORK_WSDL_FLAGS:
            value = scalar(root, tag)
            if value is None and children(root, tag):
                issue("network_value_missing", root, property=tag)
            if value and value.text.strip() not in {"true", "false", "0", "1"}:
                issue("network_value_unsupported", value, property=tag)
        for tag in EMAIL_FIELDS:
            ref(scalar(root, tag), "EmailTemplate")
        activation = scalar(root, "deviceActEmailTemplate")
        if activation:
            ref(activation, "EmailTemplate", target_salesforce_id=activation.text.strip())
        ref(scalar(root, "site", required=True), "CustomSite")
        ref(scalar(root, "picassoSite"), "SiteDotCom")
        ref(scalar(root, "emailFooterLogo"), "Document")
        ref(scalar(root, "selfRegProfile"), "Profile")
        for group in children(root, "networkMemberGroups"):
            for tag, target_kind in (("profile", "Profile"), ("permissionSet", "PermissionSet")):
                for item in children(group, tag):
                    if item.text.strip() and not item.children:
                        ref(item, target_kind)
                    else:
                        issue("metadata_reference_value_missing", item, property=tag)
        for tabs in children(root, "tabs"):
            custom, standard = set(), set()
            for item in children(tabs, "customTab"):
                if item.text.strip() and not item.children:
                    ref(item, "CustomTab")
                    custom.add(item.text.strip().casefold())
                else:
                    issue("metadata_reference_value_missing", item, property="customTab")
            for item in children(tabs, "standardTab"):
                if item.text.strip() and not item.children:
                    standard.add(item.text.strip().casefold())
                else:
                    issue("metadata_reference_value_missing", item, property="standardTab")
            default = scalar(tabs, "defaultTab")
            if default:
                value = default.text.strip().casefold()
                if value in custom and value not in standard:
                    ref(default, "CustomTab")
                elif value in custom or (value not in standard and value != "home"):
                    issue("network_default_tab_unverified", default)
            # Standard/Home tabs are platform settings, not CustomTab names.
            facts.nodes[owner]["standard_tabs"] = sorted(standard)
        feed = scalar(root, "feedChannel")
        if feed:
            ref(feed, "SalesforceFeedChannel")
            issue("network_feed_record_not_indexed", feed)
        for menu in children(root, "navigationLinkSet"):
            menu_items(menu)

    elif kind == "NavigationMenu":
        typ = scalar(root, "containerType", required=True)
        container = scalar(root, "container", required=True)
        if typ and typ.text.strip() in {"Network", "CommunityTemplateDefinition"}:
            ref(container, typ.text.strip(), "belongs_to")
        elif typ:
            issue("navigation_container_type_unsupported", typ)
        menu_items(root)

    elif kind in {"KeywordList", "UserCriteria"}:
        site_reference(facts, root, issue=issue, ref=ref)
        # Keywords, age limits and user-type selectors are data, not fields,
        # formulas or membership lists. The site and incoming rule links suffice.

    elif kind == "PresenceUserConfig":
        for tag in ("presenceStatusOnDecline", "presenceStatusOnPushTimeout"):
            ref(scalar(root, tag), "ServicePresenceStatus")
        for assignments in children(root, "assignments"):
            for profiles in children(assignments, "profiles"):
                for item in children(profiles, "profile"):
                    if item.text.strip() and not item.children:
                        ref(item, "Profile")
                    else:
                        issue("metadata_reference_value_missing", item, property="profile")
            for users in children(assignments, "users"):
                for item in children(users, "user"):
                    if item.text.strip() and not item.children:
                        user(item)
                    else:
                        issue("metadata_reference_value_missing", item, property="user")
        decline = scalar(root, "declineReasons")
        if decline:
            # The schema calls this a list in a string but specifies no encoding.
            # Preserve the gap instead of splitting/guessing component names.
            issue("presence_decline_reason_list_unverified", decline)
