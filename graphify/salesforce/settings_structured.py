"""Reviewed nested Address, BusinessHours and Security Settings contracts.

Primary source: sf-skills c217b703b3e5a3c279f1a510d8703161b14bd0a5,
metadata_api/{AddressSettings,BusinessHoursSettings,SecuritySettings}.json.
WSDL types/cardinality are authoritative where the prose says "string" for
booleans/times. API 67 describeValueType confirms holiday recurrence enums
(including RecursYearlyNth, misspelled in one prose table). Permissions-Policy
enums are in the Metadata API guide's SecuritySettings field table; see also
https://help.salesforce.com/s/articleView?id=sf.security_browser_features.htm.
Definitions and static dependencies only: no record reads, security changes,
access evaluation, timezone scheduling, or user authentication is performed.
"""
from __future__ import annotations

from collections import Counter
from datetime import date
from importlib.resources import files
from ipaddress import ip_address
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .model import salesforce_id

DOCUMENT_SHA256 = {
    "AddressSettings": "6dbf7d9c4b6b378696760fe8fa0c26d1f1122bf829d390207e2c2621a8971c81",
    "BusinessHoursSettings": "d912827496c0e55cb1ba98eaabc82f4a2745ba36a7a0cdf52d0ebdb4b709d5f6",
    "SecuritySettings": "8a3eb444b0f94acc958d803e90875ec32277d4fb457d154a959e06d5d6b439dd",
}
DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
HOUR_TIMES = tuple(day.lower() + end for day in DAYS for end in ("StartTime", "EndTime"))
HOLIDAY_ENUMS = {
    "recurrenceType": frozenset("RecursDaily RecursEveryWeekday RecursMonthly RecursMonthlyNth RecursWeekly RecursYearly RecursYearlyNth".split()),
    "recurrenceDayOfWeek": frozenset(DAYS),
    "recurrenceInstance": frozenset("First Second Third Fourth Last".split()),
    "recurrenceMonthOfYear": frozenset("January February March April May June July August September October November December".split()),
}
PASSWORD_ENUMS = {
    "complexity": frozenset("NoRestriction AlphaNumeric SpecialCharacters UpperLowerCaseNumeric UpperLowerCaseNumericSpecialCharacters Any3UpperLowerCaseNumericSpecialCharacters".split()),
    "expiration": frozenset("ThirtyDays SixtyDays NinetyDays SixMonths OneYear Never".split()),
    "lockoutInterval": frozenset("FifteenMinutes ThirtyMinutes SixtyMinutes Forever".split()),
    "maxLoginAttempts": frozenset("ThreeAttempts FiveAttempts TenAttempts NoLimit".split()),
    "questionRestriction": frozenset({"None", "DoesNotContainPassword"}),
}
SESSION_ENUMS = {
    "grantCameraAccess": frozenset({"Always", "Never", "TrustedUrls"}),
    "grantMicrophoneAccess": frozenset({"Always", "Never", "TrustedUrls"}),
    "referrerPolicyDirective": frozenset("no-referrer origin same-origin strict-origin strict-origin-when-cross-origin no-referrer-when-downgrade origin-when-cross-origin unsafe-url".split()),
    "sessionTimeout": frozenset("TwentyFourHours TwelveHours EightHours FourHours TwoHours NinetyMinutes SixtyMinutes ThirtyMinutes FifteenMinutes".split()),
    "untrustedRedirect": frozenset({"AlwaysAllowed", "AllowWithUserPermission", "NeverAllowed"}),
    "vfInlineScriptInjection": frozenset({"Allow", "ReportOnly", "NeverAllow"}),
}
SESSION_BOOLEANS = """allowUserAuthenticationByCertificate allowUserCertBasedAuthenticationWithOcspValidation
auraBoxcarReductionPref canConfirmEmailChangeInLightningCommunities canConfirmIdentityBySmsOnly
disableTimeoutWarning enableBuiltInAuthenticator enableCSPOnEmail enableCSRFOnGet enableCSRFOnPost
enableCacheAndAutocomplete enableClickjackNonsetupSFDC enableClickjackNonsetupUser
enableClickjackNonsetupUserHeaderless enableClickjackSetup enableCoepHeader enableContentSniffingProtection
enableCoopHeader enableLightningLogin enableLightningLoginOnlyWithUserPerm enableMFADirectUILoginOptIn
enableOauthCorsPolicy enablePermissionsPolicy enablePostForSessions enableSMSIdentity enableU2F
enforceIpRangesEveryRequest enforceUserDeviceRevoked forceLogoutOnSessionTimeout forceRelogin
hasRetainedLoginHints hasUserSwitching hstsOnForcecomSites identityConfirmationOnEmailChange
identityConfirmationOnTwoFactorRegistrationEnabled lockSessionsToDomain lockSessionsToIp
lockerServiceCSP lockerServiceNext lockerServiceNextControl lockerTrustedMode redirectBlockModeEnabled
redirectionWarning referrerPolicy requireHttpOnly sendCspForUncommonClients sidToken3rdPartyAuraApp
skipSFAWhenMFADirectUILogin terminateUserSessionsWhenAdminResetsPassword unescapedHtmlSanitization
useEAPIRateLimitForConnectAPI useLocalStorageForLogoutUrl""".split()
SSO_BOOLEANS = "enableCaseInsensitiveFederationID enableForceDelegatedCallout enableMultipleSamlConfigs enableSamlJitProvisioning enableSamlLogin isLoginWithSalesforceCredentialsDisabled".split()
SHAPES = {
    "AddressSettings": {
        "": "countriesAndStates",
        "countriesAndStates": "countries",
        "countriesAndStates/countries": "active integrationValue isoCode label orgDefault standard states visible",
        "countriesAndStates/countries/states": "active integrationValue isoCode label standard visible",
    },
    "BusinessHoursSettings": {
        "": "businessHours holidays",
        "businessHours": "fullName active default name timeZoneId " + " ".join(HOUR_TIMES),
        "holidays": "activityDate businessHours description endTime isRecurring name recurrenceDayOfMonth recurrenceDayOfWeek recurrenceDayOfWeekMask recurrenceEndDate recurrenceInstance recurrenceInterval recurrenceMonthOfYear recurrenceStartDate recurrenceType startTime",
    },
    "SecuritySettings": {
        "": "networkAccess passwordPolicies sessionSettings singleSignOnSettings",
        "networkAccess": "ipRanges",
        "networkAccess/ipRanges": "description end start",
        "passwordPolicies": "apiOnlyUserHomePageURL historyRestriction minimumPasswordLength minimumPasswordLifetime obscureSecretAnswer passwordAssistanceMessage passwordAssistanceURL " + " ".join(PASSWORD_ENUMS),
        "sessionSettings": "lockerServiceAPIVersion lockerTrustedResources logoutURL welcomeEmailTemplateId " + " ".join(SESSION_BOOLEANS) + " " + " ".join(SESSION_ENUMS),
        "singleSignOnSettings": " ".join(SSO_BOOLEANS),
    },
}
REPEATED = {"BusinessHoursSettings": ("holidays/businessHours", "holidays/recurrenceDayOfWeek")}
MAX_ENTRIES = 4096


def valid_date(value):
    match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:Z|([+-])([0-9]{2}):([0-9]{2}))?", value)
    if not match:
        return False
    try:
        date.fromisoformat(match[1])
    except ValueError:
        return False
    return not match[2] or (int(match[3]) <= 14 and int(match[4]) <= 59 and (int(match[3]) < 14 or int(match[4]) == 0))


def valid_time(value):
    match = re.fullmatch(r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,12}))?(?:Z|([+-])([0-9]{2}):([0-9]{2}))?", value)
    if not match:
        return False
    hour, minute, second = map(int, match.group(1, 2, 3))
    midnight = hour == 24 and minute == second == 0 and not any(c != "0" for c in match[4] or "")
    return ((hour < 24 or midnight) and minute < 60 and second < 60 and
            (not match[5] or (int(match[6]) <= 14 and int(match[7]) < 60 and (int(match[6]) < 14 or int(match[7]) == 0))))


def parse_structured(facts, root, kind, *, issue, scalar, ref, children):
    owner = facts.source.component_id

    def value(n):
        return n.text.strip() if n else ""

    def item(n, tag, required=False):
        return scalar(n, tag, required=required or bool(children(n, tag)))

    def entries(n, tag, limit=1, required=False):
        found = children(n, tag)
        if len(found) > limit or (required and not found):
            issue("settings_container_cardinality", n, property=tag, limit=limit)
            return []
        return found

    def check(n, predicate):
        if n and not predicate(value(n)):
            issue("settings_value_unsupported", n, property=n.tag)

    def boolean(n, tag, required=False):
        check(item(n, tag, required), lambda v: v in {"true", "false", "0", "1"})

    def integer(n, tag, minimum=-2147483648, maximum=2147483647):
        check(item(n, tag), lambda v: bool(re.fullmatch(r"[+-]?[0-9]{1,10}", v)) and minimum <= int(v) <= maximum)

    def enums(n, values):
        for tag, choices in values.items():
            check(item(n, tag), lambda v, choices=choices: v in choices)

    def url(n, tag):
        def valid(v):
            if len(v) > 8192 or any(c.isspace() for c in v):
                return False
            try:
                parsed = urlsplit(v)
                return v.startswith("/") or (parsed.scheme in {"http", "https"} and bool(parsed.netloc))
            except ValueError:
                return False
        check(item(n, tag), valid)

    if kind == "AddressSettings":
        for group in entries(root, "countriesAndStates", required=True):
            countries = entries(group, "countries", MAX_ENTRIES)
            country_codes = Counter(n.value("isoCode").casefold() for n in countries)
            if sum(n.value("orgDefault") in {"true", "1"} for n in countries) > 1:
                issue("settings_multiple_default_countries", group)
            for country in countries:
                states = entries(country, "states", MAX_ENTRIES)
                state_codes = Counter(n.value("isoCode").casefold() for n in states)
                for n, counts, is_country in [(country, country_codes, True), *[(s, state_codes, False) for s in states]]:
                    for tag in ("active", "standard", "visible") + (("orgDefault",) if is_country else ()):
                        boolean(n, tag, True)
                    for tag in ("integrationValue", "isoCode", "label"):
                        item(n, tag, True)
                    if counts[n.value("isoCode").casefold()] > 1:
                        issue("settings_address_code_duplicate", n)
        # Countries, codes, labels and integration values configure picklist
        # data. They are not CustomObject/CustomField declarations or references.

    elif kind == "BusinessHoursSettings":
        hours = entries(root, "businessHours", MAX_ENTRIES)
        names = Counter((n.value("name") or n.value("fullName")).casefold() for n in hours)
        if sum(n.value("default") in {"true", "1"} for n in hours) > 1:
            issue("settings_multiple_default_business_hours", root)
        for n in hours:
            name = item(n, "name") or item(n, "fullName")
            if not name:
                issue("settings_business_hours_name_missing", n)
            elif names[value(name).casefold()] != 1 or (n.value("fullName") and n.value("name") and n.value("fullName").casefold() != n.value("name").casefold()):
                issue("settings_business_hours_identity_ambiguous", n)
            elif len(value(name)) > 1024:
                issue("settings_business_hours_name_unsupported", name)
            else:
                facts.declare("BusinessHoursEntry", value(name), n.line)
                facts.ref(owner, "BusinessHoursEntry", value(name), "contains", n.line)
            boolean(n, "default", True)
            boolean(n, "active")
            for tag in HOUR_TIMES:
                check(item(n, tag), valid_time)
            timezone = item(n, "timeZoneId")
            if timezone:
                try:
                    # Pinned packaged IANA data, not the host's optional or
                    # mutable timezone database. Never accept path traversal.
                    key = value(timezone)
                    if len(key) > 255 or not re.fullmatch(r"[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)*", key):
                        raise ValueError("invalid timezone key")
                    with files("tzdata.zoneinfo").joinpath(key).open("rb") as resource:
                        ZoneInfo.from_file(resource)
                except (ValueError, OSError, ImportError):
                    issue("settings_timezone_unsupported", timezone)
        for index, holiday in enumerate(entries(root, "holidays", MAX_ENTRIES)):
            # The provider explicitly permits duplicate holiday names. The
            # occurrence belongs to this settings source, not an invented ID.
            name = f"{facts.source.full_name}:holiday:{index + 1}"
            hid = facts.declare("BusinessHoursHoliday", name, holiday.line, label=holiday.value("name") or f"Holiday {index + 1}")
            facts.ref(owner, "BusinessHoursHoliday", name, "contains", holiday.line)
            boolean(holiday, "isRecurring")
            for tag in ("activityDate", "recurrenceStartDate", "recurrenceEndDate"):
                check(item(holiday, tag), valid_date)
            for tag in ("startTime", "endTime"):
                check(item(holiday, tag), valid_time)
            if bool(holiday.value("startTime")) != bool(holiday.value("endTime")):
                issue("settings_holiday_time_pair_missing", holiday)
            integer(holiday, "recurrenceDayOfMonth", 1, 31)
            integer(holiday, "recurrenceInterval", 1)
            integer(holiday, "recurrenceDayOfWeekMask", 0, 127)
            enums(holiday, {k: v for k, v in HOLIDAY_ENUMS.items() if k != "recurrenceDayOfWeek"})
            for n in entries(holiday, "recurrenceDayOfWeek", 7):
                if n.children or value(n) not in HOLIDAY_ENUMS["recurrenceDayOfWeek"]:
                    issue("settings_value_unsupported", n, property=n.tag)
            for n in entries(holiday, "businessHours", MAX_ENTRIES):
                if n.children or not value(n) or len(value(n)) > 1024:
                    issue("settings_business_hours_reference_invalid", n)
                else:
                    ref(n, "BusinessHoursEntry", "applies_to", source=hid, identity_contract="settings_holiday_hours")

    elif kind == "SecuritySettings":
        for network in entries(root, "networkAccess"):
            for n in entries(network, "ipRanges", MAX_ENTRIES):
                start, end = item(n, "start", True), item(n, "end", True)
                if start and end:
                    try:
                        lo, hi = ip_address(value(start)), ip_address(value(end))
                        if lo.version != hi.version or int(lo) > int(hi) or "%" in value(start) + value(end):
                            raise ValueError("invalid range")
                    except ValueError:
                        issue("settings_ip_range_invalid", n)
        for n in entries(root, "passwordPolicies"):
            enums(n, PASSWORD_ENUMS)
            integer(n, "historyRestriction", 0, 24)
            integer(n, "minimumPasswordLength", 5, 50)
            for tag in ("minimumPasswordLifetime", "obscureSecretAnswer"):
                boolean(n, tag)
            for tag in ("apiOnlyUserHomePageURL", "passwordAssistanceURL"):
                url(n, tag)
        for n in entries(root, "sessionSettings"):
            enums(n, SESSION_ENUMS)
            for tag in SESSION_BOOLEANS:
                boolean(n, tag)
            url(n, "logoutURL")
            check(item(n, "lockerServiceAPIVersion"), lambda v: bool(re.fullmatch(r"[0-9]{2}\.0", v)) and 46 <= int(v[:2]) <= 68)
            if children(n, "lockerTrustedResources"):
                issue("settings_internal_contract_unsupported", n, property="lockerTrustedResources")
            template = item(n, "welcomeEmailTemplateId")
            if template:
                if salesforce_id(value(template)):
                    ref(template, "EmailTemplate", "sends_email", target_salesforce_id=value(template), identity_contract="settings_security_welcome_email")
                else:
                    issue("settings_email_template_id_invalid", template)
        for n in entries(root, "singleSignOnSettings"):
            for tag in SSO_BOOLEANS:
                boolean(n, tag)
