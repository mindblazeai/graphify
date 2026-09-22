"""Every pinned Settings scalar slot gets positive and fail-closed tests.

Fixtures are synthetic. No customer configuration or source is checked in.
"""
import hashlib

import pytest

from graphify.salesforce import Source, build_graph
from graphify.salesforce.settings_literals import SCALAR_CONTRACTS, DOCUMENT_REVISION, DOCUMENT_SHA256

SLOTS = [(root, field, *contract) for root, fields in SCALAR_CONTRACTS.items() for field, contract in fields.items()]


def valid_value(typ, allowed):
    return {"boolean": "false", "int": "12", "double": "0.25"}.get(typ, allowed[0] if allowed else "")


def source(root, field, body):
    required = "".join(f"<{tag}>{valid_value(typ, allowed)}</{tag}>" for tag, (typ, mandatory, allowed)
                       in SCALAR_CONTRACTS[root].items() if mandatory and tag != field)
    return Source(f"settings/{root}.settings", f"<{root}>\n{required}{body}\n</{root}>", "Settings", root.removesuffix("Settings"))


@pytest.mark.parametrize("root,field,typ,required,allowed", SLOTS)
def test_each_known_scalar_has_semantics_without_fabricated_dependencies(root, field, typ, required, allowed):
    value = valid_value(typ, allowed)
    src = source(root, field, f"<{field}>{value}</{field}>")
    graph = build_graph([src])
    assert graph["coverage"][0]["level"] == "semantic"
    assert not graph["edges"] and not graph["diagnostics"]
    assert graph["nodes"][0]["source_sha"] == hashlib.sha256(src.content.encode()).hexdigest()


@pytest.mark.parametrize("root,field,typ,required,allowed", SLOTS)
@pytest.mark.parametrize("invalid", ["Account.Secret__c", "", "<nested>false</nested>"])
def test_every_scalar_slot_fails_closed_on_wrong_or_missing_value(root, field, typ, required, allowed, invalid):
    graph = build_graph([source(root, field, f"<{field}>{invalid}</{field}>")])
    assert graph["coverage"][0]["level"] == "partial" and not graph["edges"]


@pytest.mark.parametrize("root", SCALAR_CONTRACTS)
def test_every_settings_root_still_reports_unknown_properties(root):
    graph = build_graph([source(root, "", "<futureReference>Account.Secret__c</futureReference>")])
    assert graph["coverage"][0]["level"] == "partial"
    assert "metadata_xml_property_unsupported" in {d["code"] for d in graph["diagnostics"]}
    assert not graph["edges"]


@pytest.mark.parametrize("value", ["2147483648", "-2147483649", "1.5", "1e2", "9" * 10000])
def test_integer_range_and_bounded_parsing(value):
    graph = build_graph([source("ApexSettings", "defaultQueueableDelay", f"<defaultQueueableDelay>{value}</defaultQueueableDelay>")])
    assert graph["coverage"][0]["level"] == "partial"


def test_scalar_duplicates_and_case_mismatches_do_not_count_as_known():
    for body in ("<enableAccountHistory>true</enableAccountHistory><enableAccountHistory>false</enableAccountHistory>",
                 "<EnableAccountHistory>true</EnableAccountHistory>"):
        graph = build_graph([source("AccountSettings", "", body)])
        assert graph["coverage"][0]["level"] == "partial" and not graph["edges"]


def test_scalar_contracts_do_not_claim_string_or_complex_field_semantics():
    for body in ("<defaultCaseOwner>Someone</defaultCaseOwner>", "<emailToCase><futureRoutingReference>Account.Secret__c</futureRoutingReference></emailToCase>"):
        graph = build_graph([source("CaseSettings", "", body)])
        assert graph["coverage"][0]["level"] == "partial" and not graph["edges"]


def test_contract_provenance_is_fixed_and_has_each_document_hash():
    assert len(DOCUMENT_REVISION) == 40
    assert SCALAR_CONTRACTS.keys() == DOCUMENT_SHA256.keys()
    assert all(len(value) == 64 for value in DOCUMENT_SHA256.values())
