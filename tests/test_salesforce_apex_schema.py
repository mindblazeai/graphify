"""Reflection must prove an actual in-scope declaration, not merely a type."""
import copy
import hashlib
import json
from importlib.resources import files

import pytest

from graphify.salesforce import Source, build_graph, node_id


def caller(body, parameters=""):
    return Source("Caller.cls", "class Caller { void run(" + parameters + ") { " + body + " } }", "ApexClass", "Caller")


def schema(obj="Account"):
    return Source(obj + ".xml", """<CustomObject>
      <fields><fullName>Name</fullName><type>Text</type></fields>
      <recordTypes><fullName>Wholesale</fullName><label>Retail customers</label><active>true</active></recordTypes>
    </CustomObject>""", "CustomObject", obj)


def level(graph):
    return next(c["level"] for c in graph["coverage"] if c["source_file"] == "Caller.cls")


def refs(graph, kind="RecordType"):
    return [e for e in graph["edges"] if e["relation"] == "reflects" and e["target_kind"] == kind]


@pytest.mark.parametrize("root", [
    "Schema.SObjectType.Account", "Account.SObjectType.getDescribe()",
    "Schema.getGlobalDescribe().get('Account').getDescribe()",
    "System.Schema.getGlobalDescribe().get('Account').getDescribe()",
])
@pytest.mark.parametrize("suffix", [".getRecordTypeId()", ".getName().trim()", ".isAvailable()"])
def test_static_record_type_chain_has_declaration_and_source_proofs(root, suffix):
    source = caller("Object id=" + root + ".getRecordTypeInfosByDeveloperName().get('Wholesale')" + suffix + ";")
    declaration = schema()
    graph = build_graph([declaration, source])
    assert level(graph) == "semantic", graph["diagnostics"]
    edge, = refs(graph)
    assert edge["resolution"] == "resolved" and edge["target"] == node_id("RecordType", "Account.Wholesale")
    assert edge["source_file"] == source.path and edge["source_sha"] == hashlib.sha256(source.content.encode()).hexdigest()
    assert edge["binding_evidence"] == [
        {"source_file": declaration.path, "source_sha": hashlib.sha256(declaration.content.encode()).hexdigest(), "line": n, "status": "captured"}
        for n in (1, 3)]
    assert refs(graph, "CustomObject")
    assert not any(e["relation"] == "calls" for e in graph["edges"])


@pytest.mark.parametrize("body", [
    "Object x=Schema.SObjectType.Account;",
    "Object x=Account.SObjectType;",
    "Object x=Account.SObjectType.getDescribe();",
    "Boolean x=Schema.SObjectType.Account.isAccessible();",
    "String x=Account.SObjectType.getDescribe().getLabel();",
    "String x=Schema.getGlobalDescribe().get('Account').getDescribe().getName().trim();",
])
def test_bare_tokens_and_describe_calls_are_real_object_usages(body):
    graph = build_graph([schema(), caller(body)])
    assert level(graph) == "semantic", graph["diagnostics"]
    assert refs(graph, "CustomObject") and all(e["resolution"] == "resolved" for e in refs(graph, "CustomObject"))


@pytest.mark.parametrize("expression,parameters", [
    ("Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get(name).getRecordTypeId()", "String name"),
    ("Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get('Missing').getRecordTypeId()", ""),
    ("Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get('wholesale').getRecordTypeId()", ""),
    ("Schema.SObjectType.Account.getRecordTypeInfosByName().get('Wholesale').getRecordTypeId()", ""),
    ("Schema.SObjectType.Account.getRecordTypeInfosByName().get('Retail customers').getRecordTypeId()", ""),
    ("Schema.SObjectType.Account.getRecordTypeInfosById().get(recordId).getRecordTypeId()", "Id recordId"),
    ("token.getDescribe().getName()", "Schema.SObjectType token"),
    ("recordId.getSObjectType().getDescribe().getName()", "Id recordId"),
    ("Schema.getGlobalDescribe().get(name).getDescribe().getName()", "String name"),
    ("Schema.getGlobalDescribe().values().get(0).getDescribe().getName()", ""),
    ("Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName(1).get('Wholesale').getRecordTypeId()", ""),
    ("Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get('Wholesale', 1).getRecordTypeId()", ""),
    ("Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get('Wholesale').getRecordTypeId(1)", ""),
    ("Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get('Wholesale').futureMethod()", ""),
])
def test_runtime_keys_labels_missing_declarations_and_bad_signatures_stay_partial(expression, parameters):
    graph = build_graph([schema(), caller("Object x=" + expression + ";", parameters)])
    assert level(graph) == "partial"
    assert any(d["code"] == "apex_receiver_type_unresolved" for d in graph["diagnostics"])
    if not expression.endswith(("getRecordTypeId(1)", "futureMethod()")):
        assert not refs(graph)


def test_bounded_local_constants_and_concat_are_supported_without_retaining_unrelated_literals():
    graph = build_graph([schema(), caller("""final String key='Whole'+'sale';
        String privateValue='must-not-be-stored';
        Object x=Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get(key).getRecordTypeId();
        Object y=ApexPages.currentPage().getParameters().get('private-parameter').trim();""")], include_facts=True)
    assert level(graph) == "semantic" and len(refs(graph)) == 1
    raw = json.dumps(graph["facts"])
    assert "must-not-be-stored" not in raw and "private-parameter" not in raw
    assert json.dumps(["metadata_key", hashlib.sha256(b"Wholesale").hexdigest()]) in raw


def test_branch_mutation_and_shadowing_do_not_reuse_selector_constants():
    graph = build_graph([schema(), caller("""String key='Wholesale';
        if (change) { key=other; }
        Object x=Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get(key).getRecordTypeId();""", "Boolean change, String other")])
    assert level(graph) == "partial" and not refs(graph)


@pytest.mark.parametrize("identity,key,expected", [
    ("012Ab0000000001AAA", "012Ab0000000001AAA", True),
    ("012Ab0000000001AAA", "012Ab0000000001", True),
    ("012Ab0000000001AAA", "012ab0000000001AAA", False),
    (None, "012Ab0000000001AAA", False),
])
def test_record_type_id_uses_independent_metadata_identity_with_case_preserved(identity, key, expected):
    declaration = Source("Account.Wholesale.xml", "<RecordType><label>Retail customers</label></RecordType>",
                         "RecordType", "Account.Wholesale", salesforce_id=identity)
    graph = build_graph([schema(), declaration, caller(
        "Object x=Schema.SObjectType.Account.getRecordTypeInfosById().get('" + key + "').getRecordTypeId();")])
    assert bool(refs(graph)) is expected
    assert level(graph) == ("semantic" if expected else "partial")


def test_same_record_type_id_on_other_object_does_not_bind():
    declaration = Source("Other.Wholesale.xml", "<RecordType/>", "RecordType", "Other.Wholesale", salesforce_id="012Ab0000000001AAA")
    graph = build_graph([schema(), declaration, caller(
        "Object x=Schema.SObjectType.Account.getRecordTypeInfosById().get('012Ab0000000001AAA').getRecordTypeId();")])
    assert level(graph) == "partial" and not refs(graph)


def test_catalog_target_can_have_incoming_ref_but_is_never_claimed_as_captured_source():
    obj = Source("catalog/CustomObject/Account", "", "CustomObject", "Account", source_kind="catalog")
    record_type = Source("catalog/RecordType/Account.Wholesale", "", "RecordType", "Account.Wholesale", source_kind="catalog")
    graph = build_graph([obj, record_type, caller(
        "Object x=Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get('Wholesale').getRecordTypeId();")])
    assert level(graph) == "semantic"
    assert refs(graph)[0]["binding_evidence"] == []
    assert next(n for n in graph["nodes"] if n["id"] == record_type.component_id)["coverage"] == "catalog"


def test_removed_record_type_rebinds_cached_facts_and_scope_never_leaks_proofs():
    source = caller("Object x=Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get('Wholesale').getRecordTypeId();")
    complete = build_graph([schema(), source], include_facts=True)
    frozen = copy.deepcopy(complete["facts"])
    excluded = build_graph([schema(), source], previous_facts=complete["facts"], include_facts=True,
                           node_filter=lambda n: n["kind"] != "RecordType")
    assert excluded["stats"]["reused"] == 2 and level(excluded) == "partial" and not refs(excluded)
    assert complete["facts"] == frozen
    missing = build_graph([Source("Account.xml", "<CustomObject/>", "CustomObject", "Account"), source],
                          previous_facts=complete["facts"], include_facts=True)
    assert missing["stats"]["reused"] == 1 and level(missing) == "partial" and not refs(missing)
    restored = build_graph([schema(), source], previous_facts=missing["facts"])
    assert level(restored) == "semantic" and refs(restored)


def test_customer_class_or_variable_named_schema_cannot_use_platform_shortcuts():
    customer = Source("Schema.cls", "class Schema {}", "ApexClass", "Schema")
    for declaration in (customer, None):
        parameters = "" if declaration else "Object Schema"
        graph = build_graph([schema(), *([declaration] if declaration else []), caller(
            "Object x=Schema.SObjectType.Account.getRecordTypeInfosByDeveloperName().get('Wholesale').getRecordTypeId();", parameters)])
        assert level(graph) == "partial" and not refs(graph)


def test_local_type_named_account_cannot_be_misread_as_object_token():
    graph = build_graph([schema(), Source("Caller.cls", """class Caller {
        class Account {} void run() { Object x=Account.SObjectType.getDescribe().getName(); }
    }""", "ApexClass", "Caller")])
    assert level(graph) == "partial" and not refs(graph, "CustomObject")


def test_lookalike_customer_method_does_not_copy_its_argument_literal_into_facts():
    customer = Source("Factory.cls", "class Factory {public static Map<String,String> getRecordTypeInfosByDeveloperName(){return null;}}", "ApexClass", "Factory")
    graph = build_graph([customer, caller("String x=Factory.getRecordTypeInfosByDeveloperName().get('privateSecretValue').trim();")], include_facts=True)
    assert level(graph) == "semantic" and not refs(graph)
    assert "privateSecretValue" not in json.dumps(graph["facts"])


def test_reflection_catalog_is_signature_only_and_pinned():
    data = json.loads(files("graphify.salesforce").joinpath("apex_schema.json").read_text())
    assert data["source_sha256"] == "3a8c609cccb49b2383c13a6157bdc8b802a981ff4473cfc7d95c7b5fef6a9cda"
    assert len(data["methods"]) == 35
    assert ["Schema.DescribeSObjectResult", "getRecordTypeInfosByDeveloperName", False, [], "Map<String,Schema.RecordTypeInfo>"] in data["methods"]
    assert all(not args for owner, method, static, args, returns in data["methods"])
