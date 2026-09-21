from pathlib import Path
import json

import pytest

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


@pytest.mark.parametrize("declaration,access", [
    ("List<Account> records", "records[0]"),
    ("Account[] records", "records[0]"),
    ("System.List<Account> records", "records.get(0)"),
    ("Map<Id,Account> records", "records.get('id')"),
    ("Map<Id,List<Account>> records", "records.get('id')[0]"),
    ("List<List<Account>> records", "records[0].get(0)"),
    ("Map<Id,Account> records", "records.values().get(0)"),
])
def test_collection_receivers_keep_element_fields_without_fake_method_calls(declaration, access):
    graph = build_graph([*schema(), source("ApexClass", "Caller", f"""class Caller {{
      void run({declaration}) {{ records.size(); {access}.Score__c = 1; String name = {access}.Name; }}
    }}""")])
    assert any(target == "Account.Score__c" for _, target in edges(graph, "writes"))
    assert any(target == "Account.Name" for _, target in edges(graph, "reads"))
    assert not any(e["relation"] == "calls" for e in graph["edges"])
    assert not graph["diagnostics"]


@pytest.mark.parametrize("operation", ["update records;", "Database.update(records);", "update records[0];", "Database.update(byId.values());"])
def test_collection_dml_still_links_the_actual_sobject(operation):
    graph = build_graph([*schema(), source("ApexClass", "Caller", f"class Caller {{ void run(List<Account> records, Map<Id,Account> byId) {{ {operation} }} }}")])
    assert any(target == "Account" for _, target in edges(graph, "writes"))
    assert not any("<" in e["target_name"] for e in graph["edges"] if e["target_kind"] == "CustomObject")


def test_collection_overload_and_custom_get_method_are_not_filtered_as_platform_calls():
    graph = build_graph([*schema(),
        source("ApexClass", "Service", "class Service { void run(List<Account> items) {} void run(Account item) {} String get(Integer i) {return null;} }"),
        source("ApexClass", "Caller", "class Caller { void run(List<Account> records, Service svc) { svc.run(records); svc.get(0); } }")])
    assert ("Caller.run(List<Account>,Service)", "Service.run(List<Account>)") in edges(graph, "calls")
    assert ("Caller.run(List<Account>,Service)", "Service.get(Integer)") in edges(graph, "calls")
    assert not any(target == "Service.run(Account)" for _, target in edges(graph, "calls"))


def test_unknown_custom_return_type_is_explicit_not_a_fabricated_field():
    graph = build_graph([source("ApexClass", "Caller", "class Caller { void run() { String name = Factory.get().Name; } }")])
    assert any(e["target_name"] == "Factory.get" for e in graph["edges"])
    assert not any(e["target_kind"] == "FieldPath" for e in graph["edges"])
    assert any(d["code"] == "apex_receiver_type_unresolved" for d in graph["diagnostics"])
    assert graph["coverage"][0]["level"] == "partial"


def test_new_collection_references_elements_without_fabricating_their_constructor():
    graph = build_graph([*schema(), source("ApexClass", "Caller", "class Caller { void run() { List<Account> records = new List<Account>(); } }")])
    assert any(target == "Account" for _, target in edges(graph, "references_type"))
    assert not edges(graph, "constructs")


def test_snapshot_source_order_is_stable_and_preserves_evidence():
    sources = [*schema(), source("ApexClass", "Reader", "class Reader { void run(Account a) { a.Score__c = 1; update a; } }")]
    a, b = build_graph(sources), build_graph(list(reversed(sources)))
    assert a == b
    assert a["edges"] == sorted(a["edges"], key=lambda e: (e["source_file"], e["source"], e["line"], e["id"]))


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


def test_constant_query_concatenation_and_reflection_keep_real_source_hash():
    code = """class Queries {
      static final String FIELD = 'Name';
      void run() {
        String q = 'SELECT ' + FIELD + ' FROM Account';
        q += ' LIMIT 1';
        Database.query(q);
        String typeName = 'Tar' + 'get'; Type.forName(typeName);
      }
    }"""
    graph = build_graph([*schema(), source("ApexClass", "Queries", code), source("ApexClass", "Target", "class Target {}")])
    assert ("Queries.run()", "Account.Name") in edges(graph, "reads")
    assert ("Queries.run()", "Target") in edges(graph, "reflects")
    import hashlib
    query_edges = [e for e in graph["edges"] if e["relation"] == "queries"]
    assert query_edges and all(e["source_sha"] == hashlib.sha256(code.encode()).hexdigest() for e in query_edges)
    assert not any(d["code"].startswith("dynamic_") for d in graph["diagnostics"])


@pytest.mark.parametrize("mutation", ["q = fragment;", "if (flag) { q = fragment; }", "{ q = fragment; }", "while (flag) { q = fragment; }"])
def test_runtime_query_mutations_never_reuse_an_obsolete_constant(mutation):
    code = "class Queries { void run(String fragment, Boolean flag) { String q = 'SELECT Name FROM Account'; " + mutation + " Database.query(q); } }"
    graph = build_graph([*schema(), source("ApexClass", "Queries", code)])
    assert not edges(graph, "queries")
    assert any(d["code"] == "dynamic_query_unresolved" for d in graph["diagnostics"])


def test_mutable_class_query_field_is_not_a_compile_time_constant():
    graph = build_graph([*schema(), source("ApexClass", "Queries", "class Queries { String q = 'SELECT Name FROM Account'; void run(){ Database.query(q); } }")])
    assert not edges(graph, "queries")
    assert any(d["code"] == "dynamic_query_unresolved" for d in graph["diagnostics"])


def test_generic_metadata_keeps_node_and_reports_coverage():
    graph = build_graph([source("NewSalesforceType", "Whatever", "<NewSalesforceType><thing>value</thing></NewSalesforceType>")])
    assert graph["coverage"][0]["level"] == "structural"
    assert graph["nodes"][0]["id"] == node_id("NewSalesforceType", "Whatever")


@pytest.mark.parametrize("kind", ["ApexClass", "ApexTrigger", "ApexPage", "ApexComponent"])
def test_salesforce_hidden_body_is_availability_not_a_syntax_error(kind):
    graph = build_graph([source(kind, "PackageCode", "(hidden)")])
    assert graph["coverage"][0]["level"] == "catalog"
    assert graph["diagnostics"][0]["code"] == "source_hidden_by_salesforce"
    assert graph["edges"] == []


def test_bundle_coverage_is_not_determined_by_css_sorting_first():
    graph = build_graph([
        source("LightningComponentBundle", "card", ".card {}", "lwc/card/card.css"),
        source("LightningComponentBundle", "card", "import x from '@salesforce/schema/Account.Name';", "lwc/card/card.js"),
    ])
    node = next(n for n in graph["nodes"] if n["id"] == node_id("LightningComponentBundle", "card"))
    assert node["coverage"] == "semantic"


def describe_source(name, fields, children=None):
    return source("CustomObject", name, json.dumps({"name": name, "fields": fields,
        "childRelationships": children or []}, indent=2), f"salesforce-api/sobjects/{name}/describe.json", source_kind="api")


def test_real_describe_binds_standard_parent_and_child_relationships():
    graph = build_graph([
        describe_source("Account", [{"name": "Name", "type": "string"}],
                        [{"relationshipName": "Contacts", "childSObject": "Contact", "field": "AccountId"}]),
        describe_source("Contact", [{"name": "Name", "type": "string"},
                        {"name": "AccountId", "type": "reference", "relationshipName": "Account", "referenceTo": ["Account"]}]),
        source("ReportType", "Contacts", "<ReportType><baseObject>Account</baseObject><sections><columns><table>Account.Contacts</table><field>Account.Name</field></columns></sections></ReportType>"),
        source("Report", "Folder/Names", "<Report><reportType>Contacts__c</reportType><columns><field>Account.Contacts$Name</field></columns></Report>"),
    ])
    assert ("Contacts", "Account.Name") in edges(graph)
    assert ("Folder/Names", "Contact.Name") in edges(graph)
    assert ("Folder/Names", "Contacts") in edges(graph)
    assert all(e["source_sha"] for e in graph["edges"])


def test_describe_never_invents_field_names_and_rejects_wrong_identity():
    graph = build_graph([describe_source("Contact", [{"name": "AccountId", "relationshipName": "Account", "referenceTo": ["Account"]}])])
    assert not any(n["name"] == "Contact.Account" for n in graph["nodes"])
    bad = source("CustomObject", "Account", '{"name":"Contact","fields":[]}', "salesforce-api/sobjects/Account/describe.json")
    assert build_graph([bad])["diagnostics"][0]["code"] == "describe_identity_mismatch"


def test_flexipage_record_merge_scope_uses_its_declared_object():
    graph = build_graph([*schema(), source("FlexiPage", "Card", "<FlexiPage><sobjectType>Account</sobjectType><value>{!Record.Score__c}</value></FlexiPage>")])
    assert ("Card", "Account.Score__c") in edges(graph, "reads")
    assert not any("Account.Record" in n["name"] for n in graph["nodes"])


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


@pytest.mark.parametrize("body", [
    "Hello {!Account.Score__c}",
    "<html><body>Score: {!Account.Score__c}<br>Thanks</body></html>",
    "Score: {{{Account.Score__c}}}",
    "Score: {{Account.Score__c}}",
])
def test_email_template_body_links_custom_fields_with_exact_evidence(body):
    graph = build_graph([*schema(), source("EmailTemplate", "Sales/Score", body,
                                         "email/Sales/Score.email")])
    assert ("Sales/Score", "Account.Score__c") in edges(graph, "reads")
    edge = next(e for e in graph["edges"] if e["source"] == node_id("EmailTemplate", "Sales/Score"))
    assert edge["source_file"] == "email/Sales/Score.email"
    assert edge["source_location"] == "L1"
    assert graph["stats"]["coverage"]["partial"] == 0


def test_visualforce_email_context_and_relationship_traversal():
    graph = build_graph([*schema(), catalog("CustomField", "Contact.Email"), catalog("CustomObject", "Contact"),
        source("EmailTemplate", "Sales/Score", '''<messaging:emailTemplate recipientType="Contact" relatedToType="Account" subject="{!relatedTo.Name}">
<messaging:plainTextEmailBody>
Score: {!relatedTo.Score__c}; parent: {!relatedTo.Parent__r.Name}; recipient: {!recipient.Email}
</messaging:plainTextEmailBody></messaging:emailTemplate>''', "email/Sales/Score.email")])
    for target in ["Account.Name", "Account.Score__c", "Contact.Email"]:
        assert ("Sales/Score", target) in edges(graph, "reads")
    assert ("Sales/Score", "Account.Parent__c") in edges(graph, "traverses")
    assert next(e for e in graph["edges"] if e["target_name"] == "Account.Score__c" and e["source"] == node_id("EmailTemplate", "Sales/Score"))["line"] == 3


def test_lightning_email_context_binds_across_sidecar_and_unknown_recipient_stays_unresolved():
    graph = build_graph([*schema(),
        source("EmailTemplate", "Sales/Score", "Score: {{{RelatedTo.Score__c}}}; {{{Recipient.Email}}}", "email/Sales/Score.email"),
        source("EmailTemplate", "Sales/Score", "<EmailTemplate><relatedEntityType>Account</relatedEntityType></EmailTemplate>", "email/Sales/Score.email-meta.xml")])
    assert ("Sales/Score", "Account.Score__c") in edges(graph, "reads")
    assert next(e for e in graph["edges"] if e["target_name"] == "Recipient.Email")["resolution"] == "unresolved"


def test_email_prose_comments_and_formula_string_literals_do_not_invent_links():
    graph = build_graph([*schema(), source("EmailTemplate", "Sales/Score",
        "Account.Score__c\n<!-- {!Account.Score__c} -->\n{!IF(true, 'Account.Score__c', 'none')}", "email/Sales/Score.email")])
    assert not any(e["source"] == node_id("EmailTemplate", "Sales/Score") for e in graph["edges"])


def test_case_status_real_cross_metadata_pattern():
    graph = build_graph([
        catalog("CustomField", "Case.Status"),
        source("Layout", "Case-Case Layout", "<Layout><layoutSections><layoutColumns><layoutItems>\n<field>Status</field></layoutItems></layoutColumns></layoutSections></Layout>"),
        source("Flow", "Technical_Case", "<Flow><recordCreates><name>Insert_case</name><object>Case</object><inputAssignments>\n<field>Status</field><value><stringValue>New</stringValue></value></inputAssignments></recordCreates></Flow>"),
        source("EmailTemplate", "Support/CaseStatus", "Status: {!Case.Status}", "email/Support/CaseStatus.email"),
        source("PermissionSet", "Support", "<PermissionSet><fieldPermissions><field>Case.Status</field><readable>true</readable></fieldPermissions></PermissionSet>"),
    ])
    assert ("Case-Case Layout", "Case.Status") in edges(graph, "references_field")
    assert ("Technical_Case.Insert_case", "Case.Status") in edges(graph, "writes")
    assert ("Support/CaseStatus", "Case.Status") in edges(graph, "reads")
    assert ("Support", "Case.Status") in edges(graph, "grants_access")


def test_flow_assignments_text_templates_and_flexipage_field_items():
    graph = build_graph([catalog("CustomField", "Case.Status"),
        source("Flow", "CaseFlow", '''<Flow><start><object>Case</object></start>
<assignments><name>SetStatus</name><assignmentItems><assignToReference>$Record.Status</assignToReference></assignmentItems></assignments>
<textTemplates><name>Message</name><text>New status: {!$Record.Status}</text></textTemplates></Flow>'''),
        source("FlexiPage", "CasePage", "<FlexiPage><sobjectType>Case</sobjectType><flexiPageRegions><itemInstances><fieldInstance><fieldItem>Record.Status</fieldItem></fieldInstance></itemInstances></flexiPageRegions></FlexiPage>")])
    assert ("CaseFlow.SetStatus", "Case.Status") in edges(graph, "writes")
    assert ("CaseFlow", "Case.Status") in edges(graph, "reads")
    assert ("CasePage", "Case.Status") in edges(graph, "references_field")


def test_apex_test_classes_keep_real_calls_and_field_writes():
    graph = build_graph([*schema(),
        source("ApexClass", "Scorer", "public class Scorer { public static void run(Account a) {} }"),
        source("ApexClass", "ScorerTest", "@isTest private class ScorerTest { @isTest static void exercise() { Account a = new Account(); a.Score__c = 5; Scorer.run(a); } }")])
    assert ("ScorerTest.exercise()", "Scorer.run(Account)") in edges(graph, "calls")
    assert ("ScorerTest.exercise()", "Account.Score__c") in edges(graph, "writes")
    assert all(n.get("is_test") for n in graph["nodes"] if n["name"].startswith("ScorerTest"))


def test_reports_dashboards_workflow_alerts_and_value_sets_form_evidenced_paths():
    graph = build_graph([*schema(), catalog("GlobalValueSet", "Scores"),
        source("CustomField", "Account.Rating__c", "<CustomField><valueSet><valueSetName>Scores</valueSetName></valueSet></CustomField>"),
        source("ReportType", "ScoredAccounts", "<ReportType><baseObject>Account</baseObject><sections><columns><table>Account</table><field>Score__c</field></columns></sections></ReportType>"),
        source("Report", "Sales/Scored", "<Report><reportType>ScoredAccounts</reportType><columns><field>Account.Score__c</field></columns></Report>"),
        source("Dashboard", "Sales/Pipeline", "<Dashboard><dashboardComponent><report>Sales/Scored</report></dashboardComponent></Dashboard>"),
        source("EmailTemplate", "Sales/Score", "Your score is {!Account.Score__c}", "email/Sales/Score.email"),
        source("Workflow", "Account", "<Workflow><alerts><fullName>ScoreAlert</fullName><template>Sales/Score</template></alerts><rules><fullName>ScoreRule</fullName><actions><name>ScoreAlert</name><type>Alert</type></actions></rules></Workflow>"),
    ])
    for pair in [("Sales/Pipeline", "Sales/Scored"), ("Sales/Scored", "ScoredAccounts"),
                 ("ScoredAccounts", "Account.Score__c"), ("Account.Rating__c", "Scores"),
                 ("Account.ScoreAlert", "Sales/Score"), ("Account.ScoreRule", "Account.ScoreAlert")]:
        assert pair in edges(graph)


def test_custom_metadata_values_reference_declared_type_fields():
    graph = build_graph([catalog("CustomObject", "Config__mdt"), catalog("CustomField", "Config__mdt.Score__c"),
        source("CustomMetadata", "Config.Default", "<CustomMetadata><values><field>Score__c</field><value>5</value></values></CustomMetadata>")])
    assert ("Config.Default", "Config__mdt.Score__c") in edges(graph, "references_field")


def test_child_relationship_index_preserves_case_insensitive_polymorphic_bindings():
    graph = build_graph([
        source("CustomObject", "Child__c", "<CustomObject><fields><fullName>Parent__c</fullName><type>Lookup</type><referenceTo>Account</referenceTo><referenceTo>Contact</referenceTo><relationshipName>Children__r</relationshipName></fields><fields><fullName>Name</fullName><type>Text</type></fields></CustomObject>"),
        source("EmailTemplate", "Children", "{!ACCOUNT.children__r.Name} {!Contact.Children__r.Name}", "email/Children.email"),
    ])
    assert ("Children", "Child__c.Name") in edges(graph, "reads")
    assert len([e for e in graph["edges"] if e["source"] == node_id("EmailTemplate", "Children") and e["resolution"] == "resolved"]) == 2


@pytest.mark.parametrize("kind", sorted(registry()))
def test_each_registered_metadata_type_has_addressable_identity_and_coverage(kind):
    graph = build_graph([source(kind, "Component", f"<{kind}/>")])
    assert node_id(kind, "Component") in {n["id"] for n in graph["nodes"]}
    assert graph["coverage"][0]["metadata_type"] == kind
    assert graph["coverage"][0]["level"] in {"semantic", "structural", "partial"}
    assert all(e["source_file"] and e["source_location"] for e in graph["edges"])
