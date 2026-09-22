"""Cross-file Apex receiver proofs, native signatures, and conservative failures."""
import copy
import hashlib
import json
from importlib.resources import files

import pytest

from graphify.salesforce import Source, build_graph, node_id
from graphify.salesforce.apex_types import canonical, compatible, split_type


def apex(name, body, **kwargs):
    return Source(name + ".cls", body, "ApexClass", name, **kwargs)


def caller(body):
    return apex("Caller", "class Caller { void run() { " + body + " } }")


def schema():
    return Source("Account.xml", """<CustomObject>
      <fields><fullName>Name</fullName><type>Text</type></fields>
      <fields><fullName>Score__c</fullName><type>Number</type></fields>
    </CustomObject>""", "CustomObject", "Account")


def level(graph, name="Caller"):
    return next(c["level"] for c in graph["coverage"] if c["full_name"] == name and c["source_file"].endswith(".cls"))


def reads(graph, name):
    return [e for e in graph["edges"] if e["target"] == node_id("CustomField", name)
            and e["resolution"] == "resolved" and e["relation"] in {"reads", "writes"}]


@pytest.mark.parametrize("expression", [
    "Date.today().format()",
    "System.Date.newInstance(2026, 9, 22).addDays(1).year()",
    "Datetime.now().date().format()",
    "Time.newInstance(10, 30, 0, 0).hour()",
    "ApexPages.currentPage().getParameters().get('private-value').trim()",
    "System.currentPageReference().getParameters().values().get(0).trim()",
    "EncodingUtil.base64Decode('cHJpdmF0ZQ==').toString().length()",
    "Url.getOrgDomainUrl().toExternalForm().toLowerCase()",
    "String.valueOf(42).substring(0, 1).length()",
])
def test_reviewed_platform_return_chains_do_not_invent_metadata(expression):
    graph = build_graph([caller("Object value = " + expression + ";")], include_facts=True)
    assert level(graph) == "semantic"
    assert not graph["diagnostics"]
    assert all(e["relation"] == "method" for e in graph["edges"])
    facts = json.dumps(graph["facts"])
    assert "private-value" not in facts and "cHJpdmF0ZQ==" not in facts


@pytest.mark.parametrize("expression", [
    "Date.today(1).format()", "Date.newInstance('wrong', 1, 1).format()",
    "Date.notARealMethod().format()", "Date.today().futureUnknownMethod()",
    "ApexPages.currentPage().getParameters(1).get('x')",
    "Factory.missing().Name", "Unknown.missing().Name",
])
def test_unknown_or_wrong_native_signature_does_not_close_a_gap(expression):
    graph = build_graph([schema(), caller("Object value = " + expression + ";")])
    assert level(graph) == "partial"
    assert any(d["code"] == "apex_receiver_type_unresolved" for d in graph["diagnostics"])
    assert not reads(graph, "Account.Name")


def test_custom_return_type_uses_scoped_declaration_and_secondary_provenance():
    factory = apex("Factory", "class Factory {\n public static Account get() { return null; }\n}")
    use = caller("Factory.get().Score__c = 1; String name = Factory.get().Name;")
    graph = build_graph([schema(), factory, use], include_facts=True)
    assert level(graph) == "semantic"
    for name in ("Account.Name", "Account.Score__c"):
        edge, = reads(graph, name)
        assert edge["source_file"] == use.path and edge["source_sha"] == hashlib.sha256(use.content.encode()).hexdigest()
        assert edge["binding_evidence"] == [{"source_file": factory.path,
            "source_sha": hashlib.sha256(factory.content.encode()).hexdigest(), "line": 2, "status": "captured"}]
    # The already-proven inner call is not duplicated merely to type its result.
    calls = [e for e in graph["edges"] if e["relation"] == "calls"]
    assert len(calls) == 1 and calls[0]["resolution"] == "resolved"


@pytest.mark.parametrize("body,expression", [
    ("public static List<Account> get(){return null;}", "Factory.get()[0].Name"),
    ("public static List<Account> get(){return null;}", "Factory.get().get(0).Name"),
    ("public static Map<String,List<Account>> get(){return null;}", "Factory.get().get('x')[0].Name"),
    ("public static Map<Id,Account> get(){return null;}", "Factory.get().values().get(0).Name"),
    ("public static Account[] get(){return null;}", "Factory.get()[0].Name"),
])
def test_custom_collection_return_types_bind_real_schema_fields(body, expression):
    graph = build_graph([schema(), apex("Factory", "class Factory {" + body + "}"), caller("Object value=" + expression + ";")])
    assert level(graph) == "semantic"
    assert reads(graph, "Account.Name")


def test_dto_properties_keep_class_member_identity_and_nested_return_scope():
    factory = apex("Factory", """class Factory {
      public static DTO get(){return null;}
      public class DTO {
        public List<Account> records { get; set; }
        public String title;
      }
    }""")
    graph = build_graph([schema(), factory, caller(
        "String name=Factory.get().records[0].Name; Factory.get().title='private-title';")])
    assert level(graph) == "semantic"
    edge, = reads(graph, "Account.Name")
    assert {p["line"] for p in edge["binding_evidence"]} == {2, 4}
    members = [e for e in graph["edges"] if e.get("apex_member")]
    assert {(e["apex_member"], e["relation"]) for e in members} == {("records", "reads_member"), ("title", "writes_member")}
    assert all(e["target"] == node_id("ApexClass", "Factory.DTO") for e in members)
    assert not any(n["kind"] == "CustomField" and n["name"].startswith("Factory") for n in graph["nodes"])


def test_inherited_fields_and_methods_bind_actual_owner():
    graph = build_graph([schema(), apex("Base", "virtual class Base {public Account item; public Account get(){return null;}}"),
        apex("Child", "class Child extends Base {}"),
        apex("Factory", "class Factory {public static Child get(){return null;}}"),
        caller("String x=Factory.get().item.Name; String y=Factory.get().get().Name;")])
    assert level(graph) == "semantic"
    assert reads(graph, "Account.Name")
    assert any(e.get("apex_member") == "item" and e["target"] == node_id("ApexClass", "Base") for e in graph["edges"])
    assert any(e["target"] == node_id("ApexMethod", "Base.get()") for e in graph["edges"])


def test_same_component_member_read_does_not_create_self_usage():
    graph = build_graph([schema(), apex("Caller", """class Caller {
      public static Caller get(){return null;}
      public List<Account> records;
      void run(){String name=get().records[0].Name;}
    }""")])
    assert level(graph) == "semantic" and reads(graph, "Account.Name")
    assert not any(e.get("apex_member") for e in graph["edges"])


@pytest.mark.parametrize("argument,expected", [("'x'", "semantic"), ("1", "semantic"), ("null", "partial"), ("true", "partial")])
def test_overloads_require_one_verified_signature(argument, expected):
    graph = build_graph([schema(), apex("Factory", """class Factory {
      public static Account get(String x){return null;}
      public static Account get(Integer x){return null;}
    }"""), caller("Object x=Factory.get(" + argument + ").Name;")])
    assert level(graph) == expected
    assert bool(reads(graph, "Account.Name")) == (expected == "semantic")


def test_same_line_overloads_keep_both_calls():
    graph = build_graph([schema(), apex("Factory", """class Factory {
      public static Account get(String x){return null;}
      public static Account get(Integer x){return null;}
    }"""), caller("String a=Factory.get('x').Name; String b=Factory.get(1).Name;")])
    targets = {e["target"] for e in graph["edges"] if e["relation"] == "calls" and e["resolution"] == "resolved"}
    assert targets == {node_id("ApexMethod", "Factory.get(String)"), node_id("ApexMethod", "Factory.get(Integer)")}


@pytest.mark.parametrize("signature,call,expected", [
    ("public static Account get()", "Factory.get()", "semantic"),
    ("public Account get()", "Factory.get()", "partial"),
    ("public Account get()", "new Factory().get()", "semantic"),
    ("public static Account get()", "new Factory().get()", "partial"),
    ("public static void get()", "Factory.get()", "partial"),
    ("public static Missing get()", "Factory.get()", "partial"),
])
def test_return_type_and_static_instance_constraints(signature, call, expected):
    graph = build_graph([schema(), apex("Factory", "class Factory {" + signature + " {return null;}}"),
                         caller("Object x=" + call + ".Name;")])
    assert level(graph) == expected
    assert bool(reads(graph, "Account.Name")) == (expected == "semantic")


def test_custom_class_shadow_does_not_inherit_native_methods():
    declared = apex("Date", "class Date {public static Account today(){return null;}}")
    graph = build_graph([schema(), declared, caller("Object x=Date.today().Name; Object y=System.Date.today().format();")])
    assert level(graph) == "semantic" and reads(graph, "Account.Name")
    assert any(e["target"] == node_id("ApexMethod", "Date.today()") for e in graph["edges"])
    missing = build_graph([declared, caller("Object x=Date.today().format();")])
    assert level(missing) == "partial"


def test_custom_string_is_not_a_native_string_argument_or_return():
    graph = build_graph([apex("String", "class String {}"), caller(
        "Object x=System.String.valueOf(1).trim(); Object y=Date.newInstance(new String(),1,1).format();")])
    assert level(graph) == "partial"
    diagnostics = [d for d in graph["diagnostics"] if d["code"] == "apex_receiver_type_unresolved"]
    assert len(diagnostics) == 1 and diagnostics[0]["member"] == "format"


def test_dependent_signatures_rebind_without_mutating_cached_caller_facts():
    first = apex("Factory", "class Factory {public static Account get(){return null;}}")
    use = caller("String x=Factory.get().Name;")
    complete = build_graph([schema(), first, use], include_facts=True)
    frozen = copy.deepcopy(complete["facts"])
    for replacements in ([], [apex("Factory", "class Factory {public static String get(){return null;}}")]):
        changed = build_graph([schema(), *replacements, use], previous_facts=complete["facts"], include_facts=True)
        assert changed["stats"]["reused"] == 2
        assert level(changed) == "partial" and not reads(changed, "Account.Name")
        assert complete["facts"] == frozen
        restored = build_graph([schema(), first, use], previous_facts=changed["facts"])
        assert level(restored) == "semantic" and reads(restored, "Account.Name")


def test_scope_filter_prevents_return_type_proof_from_excluded_package():
    target = apex("pkg.Factory", "class Factory {public static Account get(){return null;}}", namespace="pkg")
    use = caller("String x=pkg.Factory.get().Name;")
    sources = [schema(), target, use]
    captured = build_graph(sources, include_facts=True)
    assert level(captured) == "semantic" and reads(captured, "Account.Name")
    graph = build_graph(sources, previous_facts=captured["facts"], node_filter=lambda n: n.get("namespace") != "pkg")
    assert level(graph) == "partial" and not reads(graph, "Account.Name")
    assert not any(p["source_file"] == target.path for e in graph["edges"] for p in e.get("binding_evidence", []))


def test_other_syntax_diagnostics_are_not_erased_by_resolved_receivers():
    graph = build_graph([caller("String x=Date.today().format(); broken @;")])
    assert level(graph) == "partial"
    assert any(d["code"] == "syntax_error" for d in graph["diagnostics"])


def test_partial_sidecar_keeps_component_and_members_partial():
    source = caller("String x=Date.today().format();")
    sidecar = Source("Caller.cls-meta.xml", "<ApexClass><", "ApexClass", "Caller")
    graph = build_graph([source, sidecar])
    assert level(graph) == "semantic"
    assert next(n for n in graph["nodes"] if n["id"] == node_id("ApexClass", "Caller"))["coverage"] == "partial"


@pytest.mark.parametrize("expression", [
    "Date.today()" + ".addDays(1)" * 24 + ".format()",
    "Date.newInstance(" + ",".join("1" for _ in range(33)) + ").format()",
])
def test_expression_limits_keep_unproven_receivers_partial(expression):
    graph = build_graph([caller("Object x=" + expression + ";")])
    assert level(graph) == "partial"


def test_type_identity_normalization_preserves_customer_class_names():
    assert split_type("Map < Id, List<Account> >") == ("Map", ["Id", "List<Account>"])
    assert split_type("Account[]") == ("List", ["Account"])
    assert split_type("List<Account>>") == ("", [])
    assert canonical("MySystem.String") == "mysystem.string"
    assert compatible("System.String", "System.Id")
    assert not compatible("String", "System.String")
    assert not compatible("", "")


def test_packaged_platform_contract_is_pinned_data_not_customer_source():
    contract = json.loads(files("graphify.salesforce").joinpath("apex_platform.json").read_text())
    assert contract["source_sha256"] == "3a8c609cccb49b2383c13a6157bdc8b802a981ff4473cfc7d95c7b5fef6a9cda"
    assert len(contract["methods"]) == 332
    assert ["System.Date", "today", True, [], "Date"] in contract["methods"]
    assert all(owner.startswith("System.") and isinstance(static, bool)
               for owner, name, static, params, returns in contract["methods"])
