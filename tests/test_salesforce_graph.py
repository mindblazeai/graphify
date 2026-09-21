from pathlib import Path

from graphify.salesforce import Source, build_graph, node_id, scan_project
from graphify.salesforce.registry import identify, registry


def source(kind, name, text, path=None, **kwargs):
    ext = {"ApexClass": ".cls", "ApexTrigger": ".trigger", "SOQL": ".soql", "SOSL": ".sosl"}.get(kind, ".xml")
    return Source(path or name + ext, text, kind, name, **kwargs)


def catalog(kind, name):
    return source(kind, name, "", source_kind="catalog")


def edges(graph, relation=None):
    names = {n["id"]: n["name"] for n in graph["nodes"]}
    return {(names[e["source"]], names[e["target"]]) for e in graph["edges"]
            if e["resolution"] == "resolved" and (relation is None or e["relation"] == relation)}


def schema():
    return [source("CustomObject", "Account", """<CustomObject>
      <fields><fullName>Score__c</fullName><type>Number</type></fields>
      <fields><fullName>Parent__c</fullName><type>Lookup</type>
        <referenceTo>Account</referenceTo><relationshipName>Children</relationshipName></fields>
      <fields><fullName>Name</fullName><type>Text</type></fields>
      </CustomObject>""")]


def test_apex_syntax_and_field_resolution_ignore_comments_and_strings():
    code = """public class Scorer {
      // Ghost.run(); update ghosts; [SELECT Hidden FROM Missing]
      public static void updateScore(Account a) {
        String s = 'Imaginary.run();';
        a.Score__c = 4;
        update a;
        List<Account> parents = [SELECT Parent__r.Name FROM Account];
      }
    }"""
    graph = build_graph([*schema(), source("ApexClass", "Scorer", code)])
    assert ("Scorer.updateScore(Account)", "Account.Score__c") in edges(graph, "writes")
    assert ("Scorer.updateScore(Account)", "Account") in edges(graph, "writes")
    assert ("Scorer.updateScore(Account)", "Account.Name") in edges(graph, "reads")
    assert not any("Ghost" in n["name"] or "Imaginary" in n["name"] for n in graph["nodes"])


def test_overloaded_methods_and_local_variable_types():
    graph = build_graph([
        source("ApexClass", "Service", "public class Service { public void run(String s) {} public void run(Integer n) {} }"),
        source("ApexClass", "Caller", "public class Caller { void go() { Service s = new Service(); s.run('x'); } }"),
    ])
    assert ("Caller.go()", "Service.run(String)") in edges(graph, "calls")
    assert ("Caller.go()", "Service.run(Integer)") not in edges(graph, "calls")


def test_unknown_overload_is_ambiguous_and_never_claimed_resolved():
    graph = build_graph([
        source("ApexClass", "Service", "public class Service { public void run(String s) {} public void run(Integer n) {} }"),
        source("ApexClass", "Caller", "public class Caller { void go() { Service.run(null); } }"),
    ])
    calls = [e for e in graph["edges"] if e["relation"] == "calls"]
    assert len(calls) == 1 and calls[0]["resolution"] == "ambiguous"


def test_flow_invocable_and_lwc_join_through_apex():
    graph = build_graph([
        source("ApexClass", "Score", "public class Score { @InvocableMethod public static void apply(List<Id> ids) {} }"),
        source("Flow", "ScoreFlow", """<Flow><start><object>Account</object><connector><targetReference>ScoreAction</targetReference></connector></start>
          <actionCalls><name>ScoreAction</name><actionType>apex</actionType><actionName>Score</actionName></actionCalls></Flow>"""),
        source("LightningComponentBundle", "scoreCard", "import apply from '@salesforce/apex/Score.apply';", "lwc/scoreCard/scoreCard.js"),
    ])
    assert ("ScoreFlow.ScoreAction", "Score.apply(List<Id>)") in edges(graph, "invokes")
    assert ("ScoreFlow.start", "ScoreFlow.ScoreAction") in edges(graph, "flows_to")
    assert ("scoreCard", "Score.apply(List<Id>)") in edges(graph, "imports")


def test_permissions_and_formulas_resolve_field_dependencies():
    graph = build_graph([*schema(),
        source("ValidationRule", "Account.Positive", "<ValidationRule><errorConditionFormula>Score__c &lt; 0 &amp;&amp; Name != 'Fake__c'</errorConditionFormula></ValidationRule>"),
        source("PermissionSet", "Sales", "<PermissionSet><fieldPermissions><field>Account.Score__c</field><readable>true</readable><editable>false</editable></fieldPermissions></PermissionSet>"),
    ])
    assert ("Account.Positive", "Account.Score__c") in edges(graph, "reads")
    assert ("Sales", "Account.Score__c") in edges(graph, "grants_access")
    grant = next(e for e in graph["edges"] if e["relation"] == "grants_access")
    assert grant["permissions"] == {"readable": True, "editable": False}
    assert not any("Fake__c" in n["name"] for n in graph["nodes"])


def test_dynamic_queries_are_reported_constant_queries_are_parsed():
    graph = build_graph([*schema(), source("ApexClass", "Queries", """public class Queries {
      void run(String fragment) { Database.query(fragment); Database.query('SELECT Name FROM Account'); }
    }""")])
    assert ("Queries.run(String)", "Account.Name") in edges(graph, "reads")
    assert any(d["code"] == "dynamic_query_unresolved" for d in graph["diagnostics"])


def test_generic_metadata_keeps_node_and_reports_coverage():
    graph = build_graph([source("NewSalesforceType", "Whatever", "<NewSalesforceType><thing>value</thing></NewSalesforceType>")])
    assert graph["coverage"][0]["level"] == "structural"
    assert graph["nodes"][0]["id"] == node_id("NewSalesforceType", "Whatever")


def test_xml_entities_rejected_without_dropping_component():
    graph = build_graph([source("CustomObject", "Bad", '<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><x>&x;</x>')])
    assert graph["coverage"][0]["level"] == "partial"
    assert graph["diagnostics"][0]["code"] == "xml_parse_error"
    assert len(graph["nodes"]) == 1


def test_incremental_reuses_unchanged_syntax_but_rebinds_and_prunes_deletions():
    caller = source("ApexClass", "Caller", "public class Caller { void go() { Service.run(); } }")
    service = source("ApexClass", "Service", "public class Service { static void run() {} }")
    first = build_graph([caller, service], include_facts=True)
    second = build_graph([caller], previous_facts=first["facts"], include_facts=True)
    assert second["stats"]["reused"] == 1
    assert not any(n["name"] == "Service.run()" for n in second["nodes"])
    assert next(e for e in second["edges"] if e["relation"] == "calls")["resolution"] == "unresolved"
    third = build_graph([caller, service], previous_facts=second["facts"])
    assert ("Caller.go()", "Service.run()") in edges(third, "calls")


def test_sfdx_and_mdapi_type_mapping_preserves_dots_folders_and_arbitrary_roots():
    assert identify("packages/sales/objects/Account/fields/Score__c.field-meta.xml") == ("CustomField", "Account.Score__c")
    assert identify("unpackaged/customMetadata/Config.Default.md") == ("CustomMetadata", "Config.Default")
    assert identify("packages/sales/reports/Folder/Sales.report-meta.xml") == ("Report", "Folder/Sales")
    assert identify("force-app/main/default/lwc/scoreCard/util.js") == ("LightningComponentBundle", "scoreCard")


def test_ids_do_not_depend_on_checkout_path(tmp_path):
    for prefix in ("a", "b"):
        path = tmp_path / prefix / "my-package/classes/Foo.cls"
        path.parent.mkdir(parents=True)
        path.write_text("public class Foo { void run() {} }")
    assert build_graph(scan_project(tmp_path / "a")) == build_graph(scan_project(tmp_path / "b"))


def test_all_registry_types_have_generic_coverage():
    sources = [source(kind, "Component", f"<{kind}/>") for kind in registry()]
    graph = build_graph(sources)
    assert len(graph["coverage"]) == len(registry())
    assert all(node_id(kind, "Component") in {n["id"] for n in graph["nodes"]} for kind in registry())


def test_namespace_prevents_cross_package_call_binding():
    graph = build_graph([
        source("ApexClass", "a.Service", "public class Service { static void run() {} }", namespace="a"),
        source("ApexClass", "b.Service", "public class Service { static void run() {} }", namespace="b"),
        source("ApexClass", "a.Caller", "public class Caller { void go() { Service.run(); } }", namespace="a"),
    ])
    assert ("a.Caller.go()", "a.Service.run()") in edges(graph, "calls")
    assert ("a.Caller.go()", "b.Service.run()") not in edges(graph, "calls")


def test_sosl_and_nested_relationship_fields():
    graph = build_graph([*schema(), source("SOSL", "FindAccounts", "FIND 'score' IN ALL FIELDS RETURNING Account(Name,Score__c)")])
    assert ("FindAccounts", "Account.Name") in edges(graph, "reads")
    assert ("FindAccounts", "Account") in edges(graph, "queries")
    graph = build_graph([*schema(), source("SOQL", "Parents", "SELECT Parent__r.Name FROM Account")])
    assert ("Parents", "Account.Parent__c") in edges(graph, "traverses")


def test_interface_extends_and_inherited_methods():
    graph = build_graph([
        source("ApexClass", "Base", "public virtual class Base { public void run() {} }"),
        source("ApexClass", "Child", "public class Child extends Base { void go() { run(); } }"),
        source("ApexClass", "Api", "public interface Api extends Other, More {void go();}"),
    ])
    assert ("Child.go()", "Base.run()") in edges(graph, "calls")
    assert {e["target_name"] for e in graph["edges"] if e["relation"] == "extends"} >= {"Base", "Other", "More"}


def test_aura_markup_js_helper_and_server_bindings():
    graph = build_graph([
        source("ApexClass", "Server", "public class Server { @AuraEnabled public static void run() {} }"),
        source("AuraDefinitionBundle", "card", '<aura:component controller="Server"><lightning:button onclick="{!c.go}" /></aura:component>', "aura/card/card.cmp"),
        source("AuraDefinitionBundle", "card", "({go: function(component,event,helper){ component.get('c.run'); helper.next(); }})", "aura/card/cardController.js"),
        source("AuraDefinitionBundle", "card", "({next: function(component){}})", "aura/card/cardHelper.js"),
    ])
    assert ("card", "card.go") in edges(graph, "handles")
    assert ("card.go", "Server.run()") in edges(graph, "calls")
    assert ("card.go", "card.helper.next") in edges(graph, "calls")


def test_visualforce_and_lwc_call_provenance():
    graph = build_graph([*schema(),
        source("ApexPage", "Detail", '<apex:page standardController="Account">{!Account.Name}</apex:page>', "pages/Detail.page"),
        source("ApexClass", "Service", "public class Service {static void run(){}}"),
        source("LightningComponentBundle", "card", "import run from '@salesforce/apex/Service.run'; export default class Card { go(){run();} }", "lwc/card/card.js"),
    ])
    assert ("Detail", "Account.Name") in edges(graph, "reads")
    assert ("card.go", "Service.run()") in edges(graph, "calls")


def test_json_metadata_and_profile_report_honest_coverage():
    graph = build_graph([
        source("ExperienceBundle", "Site", '{"regions":[{"componentName":"c:card"}]}', "experiences/Site/views/home.json"),
        catalog("LightningComponentBundle", "card"),
        source("Profile", "Sales", "<Profile/>"),
    ])
    assert ("Site", "card") in edges(graph, "references")
    assert any(e["source_location"] == "$.regions[0].componentName" for e in graph["edges"])
    assert next(c for c in graph["coverage"] if c["metadata_type"] == "Profile")["level"] == "partial"


def test_namespaced_field_and_label_resolution():
    graph = build_graph([
        catalog("CustomField", "Account.pkg__Score__c"), catalog("CustomLabel", "pkg__Greeting"),
        source("ApexClass", "pkg.Reader", "public class Reader { void read(Account a) { Integer i = a.Score__c; String s = System.Label.pkg.Greeting; } }", namespace="pkg"),
        source("ValidationRule", "Account.Rule", "<ValidationRule><errorConditionFormula>$Label.pkg.Greeting = 'hi'</errorConditionFormula></ValidationRule>"),
    ])
    assert ("pkg.Reader.read(Account)", "Account.pkg__Score__c") in edges(graph, "reads")
    assert ("Account.Rule", "pkg__Greeting") in edges(graph, "references")
