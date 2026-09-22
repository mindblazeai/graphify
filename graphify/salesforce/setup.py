"""Typed setup definitions (Metadata API v68); literals are not dependencies.

CMS definitions describe fields, not the content records stored in them. Topic
members are site-scoped. Numeric forecast date modes are retained as modes, not
guessed field identities. Source/API/runtime limits remain explicit.
"""
from __future__ import annotations

from collections import Counter
import re

from .model import salesforce_id


SHAPES = {
    "HomePageLayout": {"": "narrowComponents wideComponents"},
    "IframeWhiteListUrlSettings": {
        "": "iframeWhiteListUrls iframeWhiteListUrl",
        "iframeWhiteListUrls": "context url", "iframeWhiteListUrl": "context url",
    },
    "ForecastingType": {"": "active amount dateType developerName forecastingGroupDeveloperName hasCustomGroup hasProductFamily masterLabel opportunitySplitType opptyLineItemSplitType quantity roleType territory2Model"},
    "Community": {
        "": "active chatterAnswersFacebookSsoUrl communityFeedPage description emailFooterDocument emailHeaderDocument emailNotificationUrl enableChatterAnswers enablePrivateQuestions expertsGroup portal portalEmailNotificationUrl reputationLevels showInPortal site",
        "reputationLevels": "chatterAnswersReputationLevels ideaReputationLevels",
        "reputationLevels/chatterAnswersReputationLevels": "name value",
        "reputationLevels/ideaReputationLevels": "name value",
    },
    "ManagedContentType": {
        "": "description developerName isMetadataContent managedContentNodeTypes masterLabel",
        "managedContentNodeTypes": "helpText isLocalizable isRequired nodeLabel nodeName nodeType placeholderText",
    },
    "ManagedTopics": {
        "": "ManagedTopic",
        "ManagedTopic": "name managedTopicType topicDescription parentName position",
    },
    "ApexEmailNotifications": {
        "": "apexEmailNotification", "apexEmailNotification": "email user",
    },
    "NetworkBranding": {"": "loginBackgroundImageUrl loginFooterText loginLogo loginLogoName loginPrimaryColor loginQuaternaryColor loginRightFrameUrl network pageFooter pageHeader primaryColor primaryComplementColor quaternaryColor quaternaryComplementColor secondaryColor staticLogoImageUrl tertiaryColor tertiaryComplementColor zeronaryColor zeronaryComplementColor"},
    "SiteDotCom": {"": "label siteType"},
    "CallCenter": {
        "": "adapterUrl contactCenterChannels customSettings displayName displayNameLabel internalNameLabel sections version",
        "sections": "items label name", "sections/items": "label name value",
        "contactCenterChannels": "channel contactCenter omniCallbackFallbackQueue omniCallbackHandler voiceMailFallbackQueue voiceMailHandler",
    },
}

# These are platform widgets, not HomePageComponent names. Unknown standard-
# tokens stay partial instead of becoming speculative custom declarations.
STANDARD_HOME_COMPONENTS = frozenset({
    "Calendar", "Dashboard", "Tasks", "CreateNew", "MessagesAndAlerts",
    "RecentItems", "UsefulLinks",
})
CMS_TYPES = frozenset({"TEXT", "MTEXT", "RTE", "IMG", "URL", "DATE", "DATETIME", "NAMEFIELD", "MEDIA"})
FORECAST_DATES = frozenset({"0", "1", "2", "OpportunityCloseDate", "ProductDate", "ScheduleDate",
                          "OLIMeasureCloseDateOnly", "ProductDateOnly", "ScheduleDateOnly"})


def validate_literals(root, shape, *, issue, scalar, repeated=()):
    """Known scalar slots are singleton, including optional non-reference text."""
    def visit(n, path=""):
        if path in shape:
            if n.text.strip():
                issue("metadata_container_text_unsupported", n, xml_path=path)
            for tag in shape[path].split():
                child_path = path + "/" + tag if path else tag
                if child_path not in shape and child_path not in repeated:
                    scalar(n, tag)
        for child in n.children:
            visit(child, path + "/" + child.tag if path else child.tag)
    visit(root)


def parse_setup(facts, root, kind, *, issue, scalar, ref, children):
    owner = facts.source.component_id

    def val(n):
        return n.text.strip() if n else ""

    def enum(n, allowed):
        if n and val(n) not in allowed:
            issue("setup_value_unsupported", n, property=n.tag)

    def boolean(n):
        enum(n, {"true", "false", "1", "0"})

    def integer(n, minimum=0, maximum=2147483647):
        if n and (not re.fullmatch(r"[+-]?[0-9]{1,10}", val(n)) or not minimum <= int(val(n)) <= maximum):
            issue("setup_value_unsupported", n, property=n.tag)

    def bounded(n, tag, limit=2048):
        entries = children(n, tag)
        if len(entries) > limit:
            issue("setup_list_limit", n, property=tag)
            return []
        return entries

    def named(n, target_kind, relation="references", name=None, source=owner, **attrs):
        if n is None:
            return
        target = val(n) if name is None else name
        if not target or len(target) > 1024:
            issue("metadata_reference_value_unsupported", n, property=n.tag)
        else:
            ref(n, target_kind, relation, target, source, identity_contract="setup_" + kind, **attrs)

    def id_ref(n, target_kind, relation="references"):
        if n is not None:
            if not salesforce_id(val(n)):
                issue("setup_metadata_id_invalid", n, property=n.tag)
            else:
                named(n, target_kind, relation, target_salesforce_id=val(n))

    # Scalar duplicates and malformed literal containers cannot claim complete
    # semantics merely because the reference-bearing slots were well-formed.
    validate_literals(root, SHAPES[kind], issue=issue, scalar=scalar,
                      repeated=("narrowComponents", "wideComponents") if kind == "HomePageLayout" else ())

    if kind == "HomePageLayout":
        for tag in ("narrowComponents", "wideComponents"):
            for n in bounded(root, tag):
                if n.children or not val(n):
                    issue("metadata_reference_value_missing", n, property=tag)
                elif val(n).startswith("standard-"):
                    if val(n)[9:] not in STANDARD_HOME_COMPONENTS:
                        issue("home_standard_component_unsupported", n)
                else:
                    named(n, "HomePageComponent", "displays", column=tag)

    elif kind == "IframeWhiteListUrlSettings":
        for tag in ("iframeWhiteListUrls", "iframeWhiteListUrl"):
            for n in bounded(root, tag):
                enum(scalar(n, "context", required=True), {"LightningOut", "Surveys", "VisualforcePages", "DisclosureAndComplianceHubConnector"})
                scalar(n, "url", required=True)
                # A trusted external domain is not a Visualforce component.

    elif kind == "ForecastingType":
        for tag in ("active", "amount", "hasProductFamily", "quantity"):
            boolean(scalar(root, tag, required=True))
        for tag in ("developerName", "masterLabel"):
            scalar(root, tag, required=True)
        boolean(scalar(root, "hasCustomGroup"))
        enum(scalar(root, "dateType", required=True), FORECAST_DATES)
        enum(scalar(root, "roleType", required=True), {"R", "Y"})
        named(scalar(root, "territory2Model"), "Territory2Model")
        # These identities need independently indexed declarations. Missing
        # members stay unresolved; same-named roles/objects never satisfy them.
        named(scalar(root, "forecastingGroupDeveloperName", required=val(scalar(root, "hasCustomGroup")) in {"true", "1"}), "ForecastingGroup")
        named(scalar(root, "opportunitySplitType"), "OpportunitySplitType")
        named(scalar(root, "opptyLineItemSplitType"), "OpportunityLineItemSplitType")

    elif kind == "Community":
        for tag in ("active", "enableChatterAnswers", "enablePrivateQuestions", "showInPortal"):
            boolean(scalar(root, tag))
        named(scalar(root, "expertsGroup"), "Group")
        enabled = val(scalar(root, "enableChatterAnswers"))
        for tag, target_kind in (("communityFeedPage", "ApexPage"), ("emailFooterDocument", "Document"), ("emailHeaderDocument", "Document"), ("site", "CustomSite")):
            n = scalar(root, tag)
            if enabled not in {"false", "0"}:
                named(n, target_kind)
        if n := scalar(root, "portal"):
            issue("community_portal_identity_unverified", n)
        for group in bounded(root, "reputationLevels", 1):
            for tag in ("chatterAnswersReputationLevels", "ideaReputationLevels"):
                for n in bounded(group, tag, 25):
                    scalar(n, "name", required=True)
                    integer(scalar(n, "value", required=True))

    elif kind == "ManagedContentType":
        scalar(root, "developerName", required=True)
        scalar(root, "masterLabel", required=True)
        boolean(scalar(root, "isMetadataContent"))
        members = bounded(root, "managedContentNodeTypes", 15)
        names = Counter(n.value("nodeName").casefold() for n in members)
        for n in members:
            name = scalar(n, "nodeName", required=True)
            label = scalar(n, "nodeLabel", required=True)
            typ = scalar(n, "nodeType", required=True)
            required = scalar(n, "isRequired")
            local = scalar(n, "isLocalizable")
            boolean(required)
            boolean(local)
            if not name or not label or not typ:
                continue
            if (not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,99}", val(name))
                    or "__" in val(name) or val(name).endswith("_") or names[val(name).casefold()] != 1):
                issue("cms_field_name_invalid_or_ambiguous", name)
                continue
            mode = val(typ).upper()
            if mode not in CMS_TYPES:
                issue("cms_field_type_unsupported", typ)
                continue
            if mode in {"IMG", "URL", "DATE", "DATETIME"} and val(local) in {"true", "1"}:
                issue("cms_field_localization_invalid", local)
            if mode == "NAMEFIELD" and val(required) not in {"true", "1"}:
                issue("cms_name_field_not_required", n)
            full_name = facts.source.full_name + "." + val(name)
            facts.declare("ManagedContentNode", full_name, n.line, data_type=mode)
            named(n, "ManagedContentNode", "contains", full_name)
        # NAMEFIELD is required for the legacy content types supported here.
        if sum(n.value("nodeType").upper() == "NAMEFIELD" for n in members) != 1:
            issue("cms_name_field_cardinality")

    elif kind == "ManagedTopics":
        named(root, "Network", "belongs_to", facts.source.full_name)
        members = bounded(root, "ManagedTopic", 2048)
        names = Counter(n.value("name").casefold() for n in members)
        parents = {n.value("name").casefold(): n.value("parentName").casefold() for n in members
                   if names[n.value("name").casefold()] == 1 and n.value("managedTopicType") == "Navigational"}
        cyclic, done = set(), set()
        for start in parents:
            path, positions, current = [], {}, start
            while current in parents and current not in done and current not in positions:
                positions[current] = len(path)
                path.append(current)
                current = parents[current]
            if current in positions:
                cyclic.update(path[positions[current]:])
            done.update(path)
        for n in members:
            name = scalar(n, "name", required=True)
            mode = scalar(n, "managedTopicType", required=True)
            enum(mode, {"Navigational", "Featured"})
            integer(scalar(n, "position"), 0, 24)
            if not name or not mode or val(mode) not in {"Navigational", "Featured"}:
                continue
            if names[val(name).casefold()] != 1 or len(val(name)) > 512:
                issue("managed_topic_name_ambiguous", name)
                continue
            if "." in val(name) or "." in facts.source.full_name:
                issue("managed_topic_scope_encoding_unsupported", name)
                continue
            full_name = facts.source.full_name + "." + val(name)
            topic = facts.declare("ManagedTopic", full_name, n.line, topic_type=val(mode))
            named(n, "ManagedTopic", "contains", full_name)
            parent = scalar(n, "parentName")
            if parent:
                if val(name).casefold() in cyclic:
                    issue("managed_topic_parent_cycle", parent)
                elif val(mode) != "Navigational" or "." in val(parent):
                    issue("managed_topic_parent_invalid", parent)
                else:
                    named(parent, "ManagedTopic", "parent_topic", facts.source.full_name + "." + val(parent), topic)

    elif kind == "ApexEmailNotifications":
        for n in bounded(root, "apexEmailNotification"):
            email, user = scalar(n, "email"), scalar(n, "user")
            if bool(email) == bool(user):
                issue("apex_notification_recipient_invalid", n)
            elif user:
                # Do not index user records or expose recipient emails in facts.
                issue("metadata_user_record_not_indexed", user)

    elif kind == "NetworkBranding":
        named(scalar(root, "network", required=True), "Network", "belongs_to")
        for tag in ("loginLogo", "pageFooter", "pageHeader"):
            named(scalar(root, tag), "Document", "displays")
        for tag in ("loginBackgroundImageUrl", "loginRightFrameUrl", "staticLogoImageUrl"):
            if n := scalar(root, tag):
                if "{expid}" in val(n) or not re.match(r"https?://", val(n), re.I):
                    issue("network_branding_dynamic_or_local_url", n, property=tag)
        # Payload files are independently assessed; descriptor literals alone
        # don't prove analysis of accompanying HTML/images/archives.

    elif kind == "SiteDotCom":
        scalar(root, "label", required=True)
        enum(scalar(root, "siteType", required=True), {"Siteforce", "ChatterNetworkPicasso"})
        issue("site_dot_com_archive_not_analyzed")

    elif kind == "CallCenter":
        for tag in ("displayName", "displayNameLabel", "internalNameLabel"):
            scalar(root, tag, required=True)
        for group in bounded(root, "sections", 128):
            scalar(group, "name", required=True)
            scalar(group, "label", required=True)
            for n in bounded(group, "items", 128):
                for tag in ("name", "label", "value"):
                    scalar(n, tag, required=True)
        if n := scalar(root, "customSettings"):
            issue("call_center_custom_settings_unverified", n)
        for n in bounded(root, "contactCenterChannels", 128):
            for tag, target_kind in (("channel", "MessagingChannel"), ("contactCenter", "CallCenter"),
                                     ("omniCallbackFallbackQueue", "Queue"), ("voiceMailFallbackQueue", "Queue"), ("voiceMailHandler", "Flow")):
                id_ref(scalar(n, tag, required=tag in {"channel", "contactCenter"}), target_kind)
            if handler := scalar(n, "omniCallbackHandler"):
                issue("call_center_flow_or_queue_id_unverified", handler)
