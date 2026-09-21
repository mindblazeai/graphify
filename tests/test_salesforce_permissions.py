import json

import pytest

from graphify.salesforce import Source, build_graph, node_id

PSET, PROFILE, APEX = "0PS000000000001", "00e000000000001", "01p000000000001"
NAME = "XProfilePermissions"
OWNER = {"Id": PSET, "Name": NAME, "NamespacePrefix": None, "IsOwnedByProfile": True,
         "ProfileId": PROFILE, "LastModifiedDate": "2026-09-21T00:00:00Z"}


def captured(section, records=None, **changes):
    data = {"apiVersion": "v63.0", "permissionSet": OWNER, "complete": True}
    if records is not None:
        data["records"] = records
    if section == "status":
        data = {"apiVersion": "v63.0", "permissionSetId": PSET, "fullName": NAME, "status": "complete",
                "sections": {s: {"status": "complete"} for s in
                             ("PermissionSet", "FieldPermissions", "ObjectPermissions", "SetupEntityAccess")}}
    data.update(changes)
    return Source(f"salesforce-api/permissions/{PSET}/{section}.json", json.dumps(data, indent=2),
                  "PermissionSet", NAME, source_kind="api", salesforce_id=PSET)


def catalog(kind, name, sfid=None):
    return Source(f"catalog/{kind}/{name}", "", kind, name, source_kind="catalog", salesforce_id=sfid)


def field_record(**changes):
    return {"Id": "0FP000000000001", "ParentId": PSET + "AAA", "SobjectType": "Account",
            "Field": "Account.Score__c", "PermissionsRead": True, "PermissionsEdit": False, **changes}


def test_direct_field_grants_profile_identity_and_source_lines_are_preserved():
    graph = build_graph([catalog("Profile", "Admin", PROFILE + "AAA"), catalog("CustomField", "Account.Score__c"),
                         captured("PermissionSet"), captured("FieldPermissions", [field_record()]), captured("status")])
    grant = next(e for e in graph["edges"] if e["relation"] == "grants_access")
    assert grant["target"] == node_id("CustomField", "Account.Score__c")
    assert grant["permissions"] == {"readable": True, "editable": False}
    assert grant["permission_capture_status"] == "complete" and grant["line"] > 1
    assert '"Id"' in captured("FieldPermissions", [field_record()]).content.splitlines()[grant["line"] - 1]
    assert grant["source_sha"] and grant["binding_evidence"][0]["source_sha"]
    assert any(e["source"] == node_id("Profile", "Admin") and e["target"] == node_id("PermissionSet", NAME)
               and e["relation"] == "contains" for e in graph["edges"])
    assert any(d["code"] == "permission_api_direct_grants_only" for d in graph["diagnostics"])


def test_setup_access_binds_actual_catalog_ids_not_types_or_name_guesses():
    record = {"Id": "0SA000000000001", "ParentId": PSET, "SetupEntityId": APEX + "AAA", "SetupEntityType": "ApexClass"}
    graph = build_graph([catalog("ApexClass", "ActualClass", APEX), captured("SetupEntityAccess", [record]), captured("status")])
    edge = next(e for e in graph["edges"] if e["relation"] == "grants_access")
    assert edge["target"] == node_id("ApexClass", "ActualClass") and edge["target_name"] == "ActualClass"
    assert edge["target_salesforce_id"] == APEX + "AAA"
    missing = build_graph([captured("SetupEntityAccess", [record]), captured("status")])
    assert missing["edges"][0]["resolution"] == "unresolved"
    assert not any(n["kind"] == "ApexClass" and not n.get("external") for n in missing["nodes"])


def test_record_ids_are_case_sensitive_and_unknown_id_nodes_do_not_collapse():
    upper, lower = "01p00000000000A", "01p00000000000a"
    records = [{"Id": f"0SA00000000000{i}", "ParentId": PSET, "SetupEntityId": target, "SetupEntityType": "ApexClass"}
               for i, target in enumerate([upper, lower])]
    graph = build_graph([catalog("ApexClass", "Upper", upper), captured("SetupEntityAccess", records)])
    assert sum(e["resolution"] == "resolved" for e in graph["edges"]) == 1
    missing = build_graph([captured("SetupEntityAccess", records)])
    assert len({e["target"] for e in missing["edges"]}) == 2


def test_colliding_catalog_identities_stay_ambiguous():
    record = {"Id": "0SA000000000001", "ParentId": PSET, "SetupEntityId": APEX, "SetupEntityType": "ApexClass"}
    graph = build_graph([catalog("ApexClass", "One", APEX), catalog("ApexClass", "Two", APEX),
                         captured("SetupEntityAccess", [record])])
    assert graph["edges"][0]["resolution"] == "ambiguous"


def test_object_grant_retains_view_all_fields_without_inventing_individual_field_grants():
    record = {"Id": "0OP000000000001", "ParentId": PSET, "SobjectType": "Account", "PermissionsRead": True,
              "PermissionsCreate": False, "PermissionsEdit": False, "PermissionsDelete": False,
              "PermissionsViewAllRecords": False, "PermissionsModifyAllRecords": False, "PermissionsViewAllFields": True}
    graph = build_graph([catalog("CustomObject", "Account"), catalog("CustomField", "Account.Score__c"),
                         captured("ObjectPermissions", [record])])
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["permissions"]["viewAllFields"] is True
    assert graph["edges"][0]["target"] == node_id("CustomObject", "Account")


@pytest.mark.parametrize("changes", [
    {"ParentId": "0PS000000000002"}, {"Field": "Other.Score__c"}, {"PermissionsRead": "true"},
    {"PermissionsEdit": None}, {"Id": "not-an-id"},
])
def test_malformed_or_wrong_owner_grants_are_not_emitted(changes):
    graph = build_graph([captured("FieldPermissions", [field_record(**changes)])])
    assert not graph["edges"] and graph["coverage"][0]["level"] == "partial"


@pytest.mark.parametrize("changes", [
    {"permissionSet": {**OWNER, "Id": "0PS000000000002"}}, {"permissionSet": {**OWNER, "Name": "Wrong"}},
    {"permissionSet": {**OWNER, "IsOwnedByProfile": False}}, {"complete": False}, {"apiVersion": "v62.0"},
    {"permissionSet": {**OWNER, "NamespacePrefix": {"invalid": True}}},
])
def test_wrong_source_identity_or_incomplete_captures_do_not_claim_grants(changes):
    graph = build_graph([captured("FieldPermissions", [field_record()], **changes)])
    assert not graph["edges"] and graph["coverage"][0]["level"] == "partial"


@pytest.mark.parametrize("name", [NAME, "pkg__" + NAME])
def test_namespaced_owner_accepts_canonical_name_without_duplicating_prefix(name):
    owner = {**OWNER, "Name": name, "NamespacePrefix": "pkg"}
    source = captured("PermissionSet", permissionSet=owner)
    source = Source(source.path, source.content, source.metadata_type, "pkg__" + NAME,
                    namespace="pkg", source_kind="api", salesforce_id=PSET)
    graph = build_graph([catalog("Profile", "Admin", PROFILE), source])
    assert any(e["relation"] == "owned_by_profile" and e["resolution"] == "resolved" for e in graph["edges"])


def test_disabled_permissions_are_configuration_references_not_access_grants():
    graph = build_graph([catalog("CustomField", "Account.Score__c"),
        captured("FieldPermissions", [field_record(PermissionsRead=False)]),
        Source("permissionsets/Disabled.xml", "<PermissionSet><fieldPermissions><field>Account.Score__c</field>"
               "<readable>false</readable><editable>false</editable></fieldPermissions></PermissionSet>", "PermissionSet", "Disabled")])
    assert not any(e["relation"] == "grants_access" for e in graph["edges"])
    assert sum(e["relation"] == "configures_access" for e in graph["edges"]) == 2


def test_record_ids_only_belong_to_component_roots_and_invalidate_fingerprints():
    source = Source("classes/Real.cls", "class Real { void run() {} }", "ApexClass", "Real", salesforce_id=APEX)
    graph = build_graph([source], include_facts=True)
    assert all("salesforce_id" not in n for n in graph["nodes"] if n["kind"] == "ApexMethod")
    changed = Source(source.path, source.content, source.metadata_type, source.full_name, salesforce_id="01p000000000002")
    assert changed.fingerprint != source.fingerprint
    assert build_graph([changed], previous_facts=graph["facts"])["stats"]["reused"] == 0


def test_failed_section_retains_old_evidence_and_rebinds_status_without_mutating_facts():
    fields = captured("FieldPermissions", [field_record()])
    sources = [catalog("CustomField", "Account.Score__c"), fields]
    initial = build_graph([*sources, captured("status")], include_facts=True)
    failed = captured("status", status="partial", sections={"FieldPermissions": {"status": "inaccessible"}})
    graph = build_graph([*sources, failed], previous_facts=initial["facts"])
    assert graph["stats"]["reused"] == 2
    assert graph["edges"][0]["permission_capture_status"] == "inaccessible"
    assert graph["edges"][0]["binding_evidence"][0]["status"] == "inaccessible"
    assert all("permission_capture_status" not in r for f in initial["facts"].values() for r in f["references"])
    recovered = build_graph([*sources, captured("status")], previous_facts=initial["facts"])
    assert recovered["edges"][0]["permission_capture_status"] == "complete"
