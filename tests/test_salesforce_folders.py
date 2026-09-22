from copy import deepcopy

import pytest

from graphify.salesforce import Source, build_graph, node_id, scan_project


def folder(body, name="Brand"):
    return Source(f"documents/{name}-meta.xml", f"<DocumentFolder>{body}</DocumentFolder>", "DocumentFolder", name)


@pytest.mark.parametrize("access", ["ReadOnly", "ReadWrite"])
def test_public_document_folder_is_independently_declared_not_a_document(access):
    src = folder(f"<accessType>Public</accessType><name>Brand Documents</name><publicFolderAccess>{access}</publicFolderAccess>")
    graph = build_graph([src])
    node, = graph["nodes"]
    assert node["id"] == node_id("DocumentFolder", "Brand") and node["coverage"] == "semantic"
    assert node["source_file"] == src.path and node["source_sha"] == src.actual_sha
    assert node["accessType"] == "Public" and node["publicFolderAccess"] == access
    assert not graph["edges"] and not graph["diagnostics"]


@pytest.mark.parametrize("body", [
    "<accessType>Public</accessType>", "<name/>", "<name>A</name><name>B</name>",
    "<name>A</name><fullName>Other</fullName>", "<name>A</name><accessType>Open</accessType>",
    "<name>A</name><publicFolderAccess>Write</publicFolderAccess>",
    "<name>A</name><accessType/>", "<name>A</name><publicFolderAccess/>",
    "<name>A</name><future>Case.Status</future>", "<name>A</name><sharedTo><allInternalUsers>true</allInternalUsers></sharedTo>",
    "<name>A</name><folderShares/>",
])
def test_incomplete_or_unknown_folder_contract_is_not_semantic(body):
    graph = build_graph([folder(body)])
    assert graph["nodes"][0]["coverage"] == "partial" and graph["diagnostics"]


@pytest.mark.parametrize("kind", ["User", "Manager", "ManagerAndSubordinatesInternal", "PartnerUser", "CustomerPortalUser"])
def test_user_shares_remain_explicit_metadata_only_boundaries(kind):
    graph = build_graph([folder(f"<name>Public</name><folderShares><accessLevel>View</accessLevel><sharedTo>recipient@example.invalid</sharedTo><sharedToType>{kind}</sharedToType></folderShares>")])
    assert graph["nodes"][0]["coverage"] == "partial" and not graph["edges"]
    assert any(d["code"] == "metadata_only_identity_boundary" for d in graph["diagnostics"])


@pytest.mark.parametrize("kind,target", [("Group", "Group"), ("Role", "Role"), ("RoleAndSubordinates", "Role"), ("RoleAndSubordinatesInternal", "Role")])
def test_metadata_share_targets_require_real_scoped_identity_and_rebind(kind, target):
    src = folder(f"<name>Brand</name><folderShares><accessLevel>View</accessLevel><sharedTo>Support</sharedTo><sharedToType>{kind}</sharedToType></folderShares>")
    known = Source("catalog/Support", "", target, "Support", source_kind="catalog")
    good = build_graph([src, known], include_facts=True)
    saved = deepcopy(good["facts"])
    edge, = good["edges"]
    assert edge["resolution"] == "resolved" and edge["target"] == known.component_id
    assert edge["relation"] == "shared_with"
    gone = build_graph([src], previous_facts=saved, include_facts=True)
    assert gone["edges"][0]["resolution"] == "unresolved"
    assert next(n for n in gone["nodes"] if n["id"] == src.component_id)["coverage"] == "partial"
    restored = build_graph([src, known], previous_facts=gone["facts"])
    assert next(n for n in restored["nodes"] if n["id"] == src.component_id)["coverage"] == "semantic"
    assert saved == good["facts"]


def test_folder_labels_and_access_values_are_not_dependency_expressions():
    graph = build_graph([folder("<name>{!Case.Status}</name><accessType>Public</accessType>")])
    assert not graph["edges"] and graph["nodes"][0]["coverage"] == "semantic"


@pytest.mark.parametrize("path", ["documents/Brand-meta.xml", "documents/Brand.documentFolder-meta.xml"])
def test_cli_recognizes_mdapi_and_source_format_folder_without_creating_document(tmp_path, path):
    src = folder("<name>Brand</name><accessType>Public</accessType>")
    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(src.content)
    sources = scan_project(tmp_path)
    assert [(s.metadata_type, s.full_name) for s in sources] == [("DocumentFolder", "Brand")]
    assert build_graph(sources)["nodes"][0]["coverage"] == "semantic"
