"""Synthetic nested settings: literal configuration, owned hours and evidence."""
import copy
import hashlib

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.settings_structured import (HOUR_TIMES, HOLIDAY_ENUMS, PASSWORD_ENUMS,
    SESSION_BOOLEANS, SESSION_ENUMS, SSO_BOOLEANS, MAX_ENTRIES)


def source(root, body):
    return Source(f"settings/{root}.settings-meta.xml", f"<{root}>\n{body}\n</{root}>", "Settings", root.removesuffix("Settings"))


def fields(values):
    return "".join(f"<{k}>{v}</{k}>" for k, v in values.items() if v is not None)


def country(states="", **values):
    return "<countries>" + fields({"active": "true", "standard": "true", "visible": "true", "orgDefault": "false",
                                  "integrationValue": "Case.Name", "isoCode": "US", "label": "Case.Secret__c", **values}) + states + "</countries>"


def state(**values):
    return "<states>" + fields({"active": "true", "standard": "true", "visible": "true", "integrationValue": "Name",
                               "isoCode": "CO", "label": "Colorado", **values}) + "</states>"


def hours(name="Default", **values):
    return "<businessHours>" + fields({"name": name, "default": "false", "timeZoneId": "America/Denver", **values}) + "</businessHours>"


def holiday(body="", **values):
    return "<holidays>" + fields({"name": "Office closed", **values}) + body + "</holidays>"


def level(graph):
    return next(c["level"] for c in graph["coverage"] if c["metadata_type"] == "Settings")


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


def test_address_values_and_labels_do_not_invent_field_references():
    graph = build_graph([source("AddressSettings", "<countriesAndStates>" + country(state()) + "</countriesAndStates>")])
    assert level(graph) == "semantic" and not graph["edges"]


@pytest.mark.parametrize("body", ["", "<countriesAndStates/><countriesAndStates/>",
    "<countriesAndStates>" + country(active="yes") + "</countriesAndStates>",
    "<countriesAndStates>" + country(label=None) + "</countriesAndStates>",
    "<countriesAndStates>" + country(state(visible="")) + "</countriesAndStates>",
    "<countriesAndStates>" + country(state() + state()) + "</countriesAndStates>",
    "<countriesAndStates>" + country() * 2 + "</countriesAndStates>",
    "<countriesAndStates>" + country(orgDefault="true") + country(orgDefault="true", isoCode="CA") + "</countriesAndStates>",
    "<countriesAndStates>" + country(futureField="A") + "</countriesAndStates>",
    "<countriesAndStates>" + country(state(active="<nested>true</nested>")) + "</countriesAndStates>"])
def test_incomplete_duplicate_or_unknown_address_configuration_stays_partial(body):
    assert level(build_graph([source("AddressSettings", body)])) == "partial"


def test_same_state_code_is_independent_in_different_countries():
    graph = build_graph([source("AddressSettings", "<countriesAndStates>" + country(state()) + country(state(), isoCode="CA") + "</countriesAndStates>")])
    assert level(graph) == "semantic"


def test_business_hours_members_and_duplicate_holiday_labels_keep_separate_evidence():
    src = source("BusinessHoursSettings", hours("Primary") + hours("Backup") + "\n" +
                 holiday("<businessHours>Primary</businessHours>") + "\n" + holiday("<businessHours>Backup</businessHours>"))
    graph = build_graph([src])
    assert level(graph) == "semantic"
    links = [e for e in graph["edges"] if e["relation"] == "applies_to"]
    assert len(links) == 2 and links[0]["source"] != links[1]["source"]
    assert {e["target"] for e in links} == {node_id("BusinessHoursEntry", "Primary"), node_id("BusinessHoursEntry", "Backup")}
    assert all(e["resolution"] == "resolved" and e["identity_contract"] == "settings_holiday_hours" for e in links)
    assert {e["line"] for e in links} == {3, 4}
    assert all(e["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest() for e in links)


@pytest.mark.parametrize("tag", HOUR_TIMES)
def test_each_documented_weekday_time_slot_accepts_midnight_and_rejects_bad_times(tag):
    for value, expected in [("00:00:00.000Z", "semantic"), ("24:00:00Z", "semantic"), ("17:30:00-07:00", "semantic"),
                            ("25:00:00Z", "partial"), ("24:00:00.001Z", "partial"), ("12:60:00Z", "partial"), ("12:00:00+14:01", "partial")]:
        assert level(build_graph([source("BusinessHoursSettings", hours(**{tag: value}))])) == expected


@pytest.mark.parametrize("zone", ["UTC", "GMT", "America/Denver", "Asia/Kolkata", "Etc/GMT+5"])
def test_timezone_values_use_pinned_packaged_definitions(zone):
    assert level(build_graph([source("BusinessHoursSettings", hours(timeZoneId=zone))])) == "semantic"


@pytest.mark.parametrize("zone", ["Future/NotAZone", "../zoneinfo/UTC", "/etc/passwd", "", "a" * 256])
def test_unknown_empty_or_traversal_timezone_is_explicitly_partial(zone):
    assert level(build_graph([source("BusinessHoursSettings", hours(timeZoneId=zone))])) == "partial"


@pytest.mark.parametrize("body", [hours() + hours(), hours(default="yes"), hours(default=None), hours(name=None),
    hours(fullName="Different"), hours(default="true") + hours("Other", default="true"),
    holiday(startTime="12:00:00Z"), holiday(activityDate="2026-02-30"), holiday(recurrenceDayOfMonth="32"),
    holiday(recurrenceInterval="0"), holiday(recurrenceDayOfWeekMask="128"), holiday(recurrenceType="RecursYealyNth"),
    holiday("<recurrenceDayOfWeek>Funday</recurrenceDayOfWeek>"), holiday("<businessHours/>"), holiday("<businessHours><name>Default</name></businessHours>")])
def test_malformed_business_hours_configuration_stays_partial(body):
    assert level(build_graph([source("BusinessHoursSettings", body)])) == "partial"


@pytest.mark.parametrize("tag,value", [(k, v) for k, values in HOLIDAY_ENUMS.items() for v in values])
def test_holiday_enumerations_are_literal_configuration(tag, value):
    graph = build_graph([source("BusinessHoursSettings", holiday(**{tag: value}))])
    assert level(graph) == "semantic" and all(e["relation"] == "contains" for e in graph["edges"])


def test_repeated_business_hours_and_day_selectors_are_not_singleton_scalars():
    body = hours("Primary") + hours("Backup") + holiday("<businessHours>Primary</businessHours><businessHours>Backup</businessHours>"
        "<recurrenceDayOfWeek>Monday</recurrenceDayOfWeek><recurrenceDayOfWeek>Friday</recurrenceDayOfWeek>", startTime="08:00:00Z", endTime="16:00:00Z", activityDate="2024-02-29Z")
    graph = build_graph([source("BusinessHoursSettings", body)])
    assert level(graph) == "semantic" and sum(e["relation"] == "applies_to" for e in graph["edges"]) == 2


def test_missing_hours_rebinds_without_poisoning_cached_facts():
    src = source("BusinessHoursSettings", holiday("<businessHours>Missing</businessHours>"))
    declaration = Source("catalog/hours", "", "BusinessHoursEntry", "Missing", source_kind="catalog")
    missing = build_graph([src], include_facts=True)
    saved = copy.deepcopy(missing["facts"])
    assert level(missing) == "partial" and "metadata_identity_unverified" in codes(missing)
    found = build_graph([src, declaration], previous_facts=missing["facts"], include_facts=True)
    assert level(found) == "semantic" and missing["facts"] == saved
    gone = build_graph([src], previous_facts=found["facts"])
    assert level(gone) == "partial"


@pytest.mark.parametrize("tag", SESSION_BOOLEANS + SSO_BOOLEANS)
def test_all_security_nested_boolean_slots_are_validated(tag):
    container = "sessionSettings" if tag in SESSION_BOOLEANS else "singleSignOnSettings"
    for value, expected in [("true", "semantic"), ("0", "semantic"), ("yes", "partial"), ("", "partial")]:
        assert level(build_graph([source("SecuritySettings", f"<{container}><{tag}>{value}</{tag}></{container}>")])) == expected


@pytest.mark.parametrize("container,enums", [("sessionSettings", SESSION_ENUMS), ("passwordPolicies", PASSWORD_ENUMS)])
def test_security_enum_contracts_are_closed_and_not_metadata_references(container, enums):
    for tag, values in enums.items():
        for value in (*values, "FutureValue"):
            graph = build_graph([source("SecuritySettings", f"<{container}><{tag}>{value}</{tag}></{container}>")])
            assert level(graph) == ("partial" if value == "FutureValue" else "semantic")
            assert not graph["edges"]


@pytest.mark.parametrize("lo,hi,expected", [("192.0.2.1", "192.0.2.254", "semantic"), ("2001:db8::1", "2001:db8::ff", "semantic"),
    ("192.0.2.9", "192.0.2.1", "partial"), ("192.0.2.1", "2001:db8::1", "partial"), ("bad", "bad", "partial"),
    ("192.0.2.0/24", "192.0.2.255", "partial"), ("fe80::1%en0", "fe80::2%en0", "partial")])
def test_ip_ranges_are_validated_as_data_not_fields(lo, hi, expected):
    graph = build_graph([source("SecuritySettings", f"<networkAccess><ipRanges><start>{lo}</start><end>{hi}</end></ipRanges></networkAccess>")])
    assert level(graph) == expected and not graph["edges"]


@pytest.mark.parametrize("body", ["<passwordPolicies><historyRestriction>25</historyRestriction></passwordPolicies>",
    "<passwordPolicies><minimumPasswordLength>4</minimumPasswordLength></passwordPolicies>", "<networkAccess/><networkAccess/>",
    "<networkAccess><ipRanges><start>192.0.2.1</start></ipRanges></networkAccess>",
    "<sessionSettings><lockerServiceAPIVersion>99.0</lockerServiceAPIVersion></sessionSettings>",
    "<sessionSettings><lockerTrustedResources>Known_Resource</lockerTrustedResources></sessionSettings>",
    "<sessionSettings><welcomeEmailTemplateId>public/Welcome</welcomeEmailTemplateId></sessionSettings>",
    "<sessionSettings><logoutURL>javascript:alert(1)</logoutURL></sessionSettings>",
    "<singleSignOnSettings><futureField>false</futureField></singleSignOnSettings>"])
def test_security_invalid_internal_or_unknown_values_stay_partial(body):
    assert level(build_graph([source("SecuritySettings", body)])) == "partial"


def test_security_welcome_template_id_preserves_type_case_and_source_evidence():
    src = source("SecuritySettings", "<sessionSettings><welcomeEmailTemplateId>00X000000000AbC</welcomeEmailTemplateId></sessionSettings>")
    target = Source("catalog/EmailTemplate/Welcome", "", "EmailTemplate", "public/Welcome", source_kind="catalog", salesforce_id="00X000000000AbCXYZ")
    graph = build_graph([src, target, Source("catalog/Document/Welcome", "", "Document", "Welcome", source_kind="catalog", salesforce_id=target.salesforce_id)])
    edge, = graph["edges"]
    assert edge["target"] == target.component_id and edge["resolution"] == "resolved"
    assert edge["line"] == 2 and edge["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()
    assert level(graph) == "semantic"
    assert level(build_graph([src])) == "partial"


@pytest.mark.parametrize("root,body", [("AddressSettings", "<countriesAndStates>" + country() * (MAX_ENTRIES + 1) + "</countriesAndStates>"),
    ("BusinessHoursSettings", hours() * (MAX_ENTRIES + 1)), ("SecuritySettings", "<networkAccess>" + "<ipRanges/>" * (MAX_ENTRIES + 1) + "</networkAccess>")])
def test_nested_collections_are_bounded(root, body):
    graph = build_graph([source(root, body)])
    assert level(graph) == "partial" and "settings_container_cardinality" in codes(graph)
