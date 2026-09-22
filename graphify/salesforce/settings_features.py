"""Reviewed Settings reference slots and nested literal contracts.

Source: Salesforce's Metadata API field tables and WSDL, pinned in
settings_literals.DOCUMENT_REVISION. String fields are classified explicitly;
unknown nested fields never become semantic just because their XML parses.
"""
from __future__ import annotations

import re

from .model import salesforce_id

CASE_TEMPLATES = "caseAssignNotificationTemplate caseCloseNotificationTemplate caseCommentNotificationTemplate caseCreateNotificationTemplate".split()
EMAIL_FLAGS = "enableE2CAttachmentAsFile enableE2CDeduplicateAttachments enableE2CExternalServer enableE2CSourceTracking enableEmailToCase enableHtmlEmail enableNewToReadTriggers enableOnDemandEmailToCase enableThreadIDInBody enableThreadIDInSubject enableThreadTokenInBody enableThreadTokenInSubject movingEmailEnabled notifyOwnerOnNewCaseEmail notifySenderE2CError preQuoteSignature replyWithNewContentOnly showServiceEmailOpenPrompt showWordCountInComposer useEmailHeadersForThreading".split()
EMAIL_ENCODINGS = frozenset("UTF-8 ISO-8859-1 Shift_JIS ISO-2022-JP EUC-JP x-SJIS_0213 ks_c_5601-1987 Big5 GB2312 Big5-HKSCS".split())
DOWNLOAD_TYPES = frozenset("AVI EXCEL EXCEL_X EXE FLASH HTML INSIGHT MOV MP3 MP4 MPEG PDF POWER_POINT POWER_POINT_X RFC822 SVG TXML UNKNOWN WAV WEBVIEW WMA WMV WORD WORD_X XHTML XML".split())
MONTHS = frozenset("January February March April May June July August September October November December".split())

SHAPES = {
    "CompanySettings": {"": "fiscalYear", "fiscalYear": "fiscalYearNameBasedOn startMonth"},
    "MyDomainSettings": {"": "myDomainName"},
    "EmployeeUserSettings": {"": "emailEncoding permset profile usernameSuffix"},
    "FileUploadAndDownloadSecuritySettings": {"": "dispositions", "dispositions": "behavior fileType securityRiskFileType"},
    "RealTimeEventSettings": {"": "realTimeEvents", "realTimeEvents": "entityName isEnabled"},
    "CaseSettings": {
        "": " ".join(CASE_TEMPLATES) + " caseFeedItemSettings defaultCaseOwner defaultCaseOwnerType defaultCaseUser emailActionDefaultsHandlerClass emailToCase systemUserEmail webToCase",
        "caseFeedItemSettings": "characterLimit displayFormat feedItemType",
        "emailToCase": " ".join(EMAIL_FLAGS) + " overEmailLimitAction unauthorizedSenderAction routingAddresses",
        "emailToCase/routingAddresses": "addressType authorizedSenders caseOrigin caseOwner caseOwnerType casePriority createTask emailAddress emailServicesAddress fallbackQueue isPermsetControlled isVerified newEntityRecordType routingFlow routingName saveEmailHeaders taskStatus",
        "webToCase": "caseOrigin defaultResponseTemplate enableWebToCase",
    },
}


def parse_features(root, kind, *, issue, scalar, ref, children):
    def value(n):
        return n.text.strip() if n else ""

    def enum(n, choices):
        if n is not None and value(n) not in choices:
            issue("settings_value_unsupported", n, property=n.tag)

    def boolean(parent, tag, required=False):
        n = scalar(parent, tag, required=required)
        if n is None and children(parent, tag):
            issue("settings_scalar_value_missing", parent, property=tag)
        enum(n, {"true", "false", "0", "1"})

    def entries(parent, tag, maximum=1):
        result = children(parent, tag)
        if len(result) > maximum:
            issue("settings_list_limit", parent, property=tag)
            return []
        return result

    def named(n, target_kind, relation="configures", **attrs):
        if n is not None:
            if len(value(n)) > 1024:
                issue("metadata_reference_value_unsupported", n, property=n.tag)
            else:
                ref(n, target_kind, relation, identity_contract="settings_" + kind, **attrs)

    def user(n):
        if n is not None:
            ref(n, "SalesforceUser")
            issue("metadata_user_record_not_indexed", n)

    def owner(parent, name_tag, type_tag):
        n, typ = scalar(parent, name_tag), scalar(parent, type_tag)
        if n is None:
            if typ:
                issue("settings_owner_context_missing", typ)
        elif value(typ) == "User":
            user(n)
        elif value(typ) == "Queue":
            named(n, "Queue")
        else:
            issue("settings_owner_context_unverified", n)

    def default_picklist(parent, tag, field):
        n = scalar(parent, tag)
        if n is not None:
            named(n, "CustomField", "sets_default", name=field)

    if kind == "CompanySettings":
        for fiscal in entries(root, "fiscalYear"):
            enum(scalar(fiscal, "fiscalYearNameBasedOn"), {"endingMonth", "startingMonth"})
            enum(scalar(fiscal, "startMonth"), MONTHS)
    elif kind == "MyDomainSettings":
        # The provider defines this as the org's URL subdomain, not a Domain
        # metadata-component reference. Preserve it as configuration data.
        name = scalar(root, "myDomainName")
        if name and (len(value(name)) > 255 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", value(name))):
            issue("settings_value_unsupported", name, property=name.tag)
    elif kind == "EmployeeUserSettings":
        enum(scalar(root, "emailEncoding"), EMAIL_ENCODINGS)
        named(scalar(root, "permset"), "PermissionSet", "assigns_permissions")
        named(scalar(root, "profile"), "Profile", "assigns_profile")
        # usernameSuffix is a login-domain suffix, not a component identity.
    elif kind == "FileUploadAndDownloadSecuritySettings":
        for item in entries(root, "dispositions", 256):
            enum(scalar(item, "fileType", required=True), DOWNLOAD_TYPES)
            # EXECUTE is in the field table; EXECUTE_IN_BROWSER is the WSDL's
            # spelling. Both are literal download behavior, never references.
            enum(scalar(item, "behavior", required=True), {"DOWNLOAD", "HYBRID", "EXECUTE", "EXECUTE_IN_BROWSER"})
            boolean(item, "securityRiskFileType", required=True)
    elif kind == "RealTimeEventSettings":
        for item in entries(root, "realTimeEvents", 512):
            named(scalar(item, "entityName", required=True), "CustomObject", "configures_events")
            boolean(item, "isEnabled", required=True)
    elif kind == "CaseSettings":
        for tag in CASE_TEMPLATES:
            named(scalar(root, tag), "EmailTemplate", "sends_email")
        named(scalar(root, "emailActionDefaultsHandlerClass"), "ApexClass", "executes")
        user(scalar(root, "defaultCaseUser"))
        owner(root, "defaultCaseOwner", "defaultCaseOwnerType")
        for item in entries(root, "caseFeedItemSettings", 256):
            enum(scalar(item, "feedItemType", required=True), {"EmailMessageEvent"})
            enum(scalar(item, "displayFormat"), {"Default", "HideBlankLines"})
            limit = scalar(item, "characterLimit")
            if limit and (not re.fullmatch(r"[0-9]{1,10}", value(limit)) or int(value(limit)) > 2147483647):
                issue("settings_value_unsupported", limit, property=limit.tag)
        for item in entries(root, "emailToCase"):
            for tag in EMAIL_FLAGS:
                boolean(item, tag)
            enum(scalar(item, "overEmailLimitAction"), {"Bounce", "Discard", "Requeue"})
            enum(scalar(item, "unauthorizedSenderAction"), {"Bounce", "Discard"})
            for address in entries(item, "routingAddresses", 4096):
                enum(scalar(address, "addressType"), {"EmailToCase", "Outlook"})
                for tag in ("createTask", "isPermsetControlled", "isVerified", "saveEmailHeaders"):
                    boolean(address, tag)
                owner(address, "caseOwner", "caseOwnerType")
                named(scalar(address, "fallbackQueue"), "Queue")
                named(scalar(address, "routingFlow"), "Flow", "invokes")
                for tag, field in (("caseOrigin", "Case.Origin"), ("casePriority", "Case.Priority"), ("taskStatus", "Task.Status")):
                    default_picklist(address, tag, field)
                record_type = scalar(address, "newEntityRecordType")
                if record_type:
                    raw = value(record_type)
                    if raw.startswith("012") and salesforce_id(raw):
                        named(record_type, "RecordType", target_salesforce_id=raw, target_object="Case")
                    elif re.fullmatch(r"(?:Case\.)?[A-Za-z_][A-Za-z0-9_]*", raw):
                        named(record_type, "RecordType", name=raw if raw.startswith("Case.") else "Case." + raw)
                    else:
                        issue("settings_record_type_identity_unverified", record_type)
        for item in entries(root, "webToCase"):
            boolean(item, "enableWebToCase")
            named(scalar(item, "defaultResponseTemplate"), "EmailTemplate", "sends_email")
            default_picklist(item, "caseOrigin", "Case.Origin")
