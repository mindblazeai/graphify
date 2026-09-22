"""Original ContentAsset pixels and envelopes form independent current proofs."""
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
import json

from PIL import Image, PngImagePlugin
import pytest

from graphify.salesforce import Source, build_graph, node_id, scan_project


def asset(fmt="PNG", *, extra="", version="1", filename=None, namespace=""):
    output = BytesIO()
    Image.new("RGB", (3, 2), (10, 20, 30)).save(output, format=fmt)
    path = "contentassets/Logo.asset"
    body = Source(path, "", "ContentAsset", "Logo", namespace=namespace,
                  source_kind="binary", binary_content=output.getvalue())
    filename = filename or ("Original logo.png" if fmt == "PNG" else "Original logo.jpg")
    xml = Source(path + "-meta.xml", f"""<ContentAsset>
  <language>en_US</language>
  <masterLabel>Display name</masterLabel>
  {extra}
  <versions><version><number>{version}</number><pathOnClient>{filename}</pathOnClient></version></versions>
</ContentAsset>""", "ContentAsset", "Logo", namespace=namespace)
    return body, xml


def owner(graph):
    return next(n for n in graph["nodes"] if n["id"] == node_id("ContentAsset", "Logo"))


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
@pytest.mark.parametrize("declared", ["", "<format>Original</format>"])
@pytest.mark.parametrize("visible", ["", "<isVisibleByExternalUsers>false</isVisibleByExternalUsers>",
                                      "<isVisibleByExternalUsers>true</isVisibleByExternalUsers>",
                                      "<isVisibleByExternalUsers>0</isVisibleByExternalUsers>",
                                      "<isVisibleByExternalUsers>1</isVisibleByExternalUsers>"])
def test_original_single_version_keeps_exact_descriptor_and_byte_evidence(fmt, declared, visible):
    body, xml = asset(fmt, extra=declared + visible)
    graph = build_graph([body, xml], include_facts=True)
    node = owner(graph)
    assert node["coverage"] == "semantic" and node["content_analysis"] == "raster_image"
    assert node["source_file"] == xml.path and node["source_sha"] == xml.actual_sha
    assert node["source_files"] == [xml.path, body.path]
    assert node["binding_evidence"] == [{"source_file": body.path, "source_sha": body.actual_sha, "line": 1, "status": "captured"}]
    assert node["payload_analysis"] == {"format": fmt.lower(), "width": 3, "height": 2, "frames": 1,
        "bytes": len(body.binary_content), "source_file": body.path, "source_sha": body.actual_sha, "asset_version": "1"}
    assert not graph["edges"] and not graph["diagnostics"]
    assert {f["coverage"]["level"] for f in graph["facts"].values()} == {"partial"}
    assert "binary_content" not in json.dumps(graph) and "Original logo" not in json.dumps(graph)


@pytest.mark.parametrize("access", ["VIEWER", "COLLABORATOR", "INFERRED"])
@pytest.mark.parametrize("managing", ["", "<isManagingWorkspace>false</isManagingWorkspace>", "<isManagingWorkspace>0</isManagingWorkspace>"])
def test_org_sharing_is_literal_configuration_not_effective_user_access(access, managing):
    graph = build_graph(asset(extra=f"<relationships><organization><access>{access}</access>{managing}</organization></relationships>"))
    assert owner(graph)["coverage"] == "semantic" and not graph["edges"]


@pytest.mark.parametrize("before,after", [
    ("<language>en_US</language>", ""), ("<masterLabel>Display name</masterLabel>", ""),
    ("<language>en_US</language>", "<language/>"),
    ("<masterLabel>Display name</masterLabel>", "<masterLabel/>"),
    ("<language>en_US</language>", "<language>en_US</language><language>fr</language>"),
    ("<masterLabel>Display name</masterLabel>", "<masterLabel>One</masterLabel><masterLabel>Two</masterLabel>"),
    ("<number>1</number>", ""), ("<number>1</number>", "<number/>"),
    ("<number>1</number>", "<number>0</number>"), ("<number>1</number>", "<number>-1</number>"),
    ("<number>1</number>", "<number>1.0</number>"), ("<number>1</number>", "<number>1000000000</number>"),
    ("<number>1</number>", "<number>1</number><number>2</number>"),
    ("<pathOnClient>Original logo.png</pathOnClient>", ""),
    ("<pathOnClient>Original logo.png</pathOnClient>", "<pathOnClient/>"),
    ("<pathOnClient>Original logo.png</pathOnClient>", "<pathOnClient>logo.jpg</pathOnClient>"),
    ("<pathOnClient>Original logo.png</pathOnClient>", "<pathOnClient>logo.pdf</pathOnClient>"),
    ("<pathOnClient>Original logo.png</pathOnClient>", "<pathOnClient>logo.png</pathOnClient><pathOnClient>other.png</pathOnClient>"),
    ("</version>", "<zipEntry>nested.png</zipEntry></version>"),
    ("</version>", "<future>Case.Status</future></version>"),
    ("</versions>", "<version><number>2</number><pathOnClient>another.png</pathOnClient></version></versions>"),
    ("</versions>", "</versions><versions/>"),
    ("<ContentAsset>", "<Document>"),
])
def test_pixels_never_hide_invalid_incomplete_or_ambiguous_envelopes(before, after):
    body, xml = asset()
    graph = build_graph([body, replace(xml, content=xml.content.replace(before, after))])
    assert owner(graph)["coverage"] == "partial" and graph["diagnostics"]


@pytest.mark.parametrize("extra", [
    "<format/>", "<format>Other</format>", "<format>original</format>",
    "<format>ZippedVersions</format>", "<format>Original</format><format>Original</format>",
    "<fullName>Other</fullName>", "<fullName/>", "<content>AA==</content>",
    "<future>Case.Status</future>", "<isVisibleByExternalUsers>yes</isVisibleByExternalUsers>",
    "<isVisibleByExternalUsers/>", "<isVisibleByExternalUsers>true</isVisibleByExternalUsers><isVisibleByExternalUsers>false</isVisibleByExternalUsers>",
    "<relationships/><relationships/>",
    "<relationships><organization><access>VIEWER</access></organization><organization><access>VIEWER</access></organization></relationships>",
    "<relationships><organization><access>Unknown</access></organization></relationships>",
    "<relationships><organization><access>VIEWER</access><isManagingWorkspace>yes</isManagingWorkspace></organization></relationships>",
    "<relationships><organization><access>VIEWER</access><name>Org</name></organization></relationships>",
])
def test_other_descriptor_gaps_survive_a_valid_image(extra):
    graph = build_graph(asset(extra=extra))
    assert owner(graph)["coverage"] == "partial" and graph["diagnostics"]


@pytest.mark.parametrize("kind", ["emailTemplate", "insightsApplication", "network", "workspace"])
def test_reserved_relationship_identity_is_never_guessed(kind):
    body, xml = asset(extra=f"<relationships><{kind}><access>VIEWER</access><name>Example</name></{kind}></relationships>")
    graph = build_graph([body, xml, Source("catalog/EmailTemplate/Example", "", "EmailTemplate", "Example", source_kind="catalog")])
    assert owner(graph)["coverage"] == "partial" and "asset_link_identity_unverified" in codes(graph)
    assert not graph["edges"]


@pytest.mark.parametrize("change", [{"namespace": "pkg"}, {"full_name": "Other"}, {"path": "other/Logo.asset-meta.xml"}])
def test_owner_namespace_and_adjacent_path_are_required(change):
    body, xml = asset()
    graph = build_graph([body, replace(xml, **change)])
    assert owner(graph)["coverage"] == "partial" and "payload_analysis" not in owner(graph)


@pytest.mark.parametrize("data", [b"", b"bad", b"BMimage", b"GIF89a", b"PK\x03\x04", b"%PDF-1.7", b"<svg/>"])
def test_unverified_formats_are_not_complete(data):
    body, xml = asset()
    graph = build_graph([replace(body, binary_content=data), xml])
    assert owner(graph)["coverage"] == "partial" and "asset_payload_not_analyzed" in codes(graph)


def test_client_filename_label_and_embedded_image_strings_are_never_dependency_names():
    body, xml = asset(filename="{!Contact.Secret__c}.png", extra="<fullName>Logo</fullName>")
    info = PngImagePlugin.PngInfo()
    info.add_text("metadata", "Case.Status {!Contact.Secret__c}", zip=True)
    output = BytesIO()
    Image.new("RGB", (2, 2)).save(output, format="PNG", pnginfo=info)
    graph = build_graph([replace(body, binary_content=output.getvalue()), xml])
    assert owner(graph)["coverage"] == "semantic" and not graph["edges"]
    assert "Secret__c" not in json.dumps(graph)


def test_cached_facts_rebind_after_descriptor_payload_or_version_changes_and_restore():
    body, xml = asset()
    before = build_graph([body, xml], include_facts=True)
    saved = deepcopy(before["facts"])
    omitted = replace(body, binary_content=None, content_sha=body.actual_sha)
    same = build_graph([omitted, xml], previous_facts=saved)
    assert same["stats"]["reused"] == 2 and owner(same)["coverage"] == "semantic"
    for sources in ([body], [xml], [replace(body, binary_content=b"bad"), xml],
                    [body, replace(xml, content=xml.content.replace("<versions>", "<format>ZippedVersions</format><versions>"))]):
        invalid = build_graph(sources, previous_facts=saved, include_facts=True)
        assert owner(invalid)["coverage"] == "partial"
        restored = build_graph([body, xml], previous_facts=invalid["facts"])
        assert owner(restored)["coverage"] == "semantic"
    assert saved == before["facts"]
    _, next_version = asset(version="2")
    updated = build_graph([body, next_version], previous_facts=saved)
    assert updated["stats"]["reused"] == 1 and owner(updated)["payload_analysis"]["asset_version"] == "2"


@pytest.mark.parametrize("duplicate_index", [0, 1])
def test_duplicate_source_identity_cannot_prove_payload(duplicate_index):
    sources = list(asset())
    graph = build_graph([*sources, sources[duplicate_index]])
    assert owner(graph)["coverage"] == "partial" and "payload_analysis" not in owner(graph)


def test_real_incoming_theme_and_outgoing_network_refs_survive_and_scope_still_applies():
    body, xml = asset(extra="<originNetwork>Customer Site</originNetwork>")
    theme = Source("brandingSets/Theme.brandingSet", "<BrandingSet><brandingSetProperty><propertyName>BRAND_IMAGE</propertyName><propertyValue>/file-asset/Logo?v=1</propertyValue></brandingSetProperty></BrandingSet>", "BrandingSet", "Theme")
    network = Source("catalog/Network/Customer Site", "", "Network", "Customer Site", source_kind="catalog")
    graph = build_graph([body, xml, theme, network])
    assert owner(graph)["coverage"] == "semantic"
    assert {(e["target_kind"], e["resolution"]) for e in graph["edges"]} == {("Network", "resolved"), ("ContentAsset", "resolved")}
    scoped = build_graph([body, xml, theme, network], node_filter=lambda n: n["kind"] != "ContentAsset")
    assert all(n.get("external") or n["kind"] != "ContentAsset" for n in scoped["nodes"])
    assert next(e for e in scoped["edges"] if e["target_kind"] == "ContentAsset")["resolution"] == "unresolved"


def test_cli_retains_original_asset_bytes_and_metadata_identity(tmp_path):
    body, xml = asset()
    for source in (body, xml):
        path = tmp_path / source.path
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(source.binary_content if source.binary_content is not None else source.content.encode())
    sources = scan_project(tmp_path)
    assert len(sources) == 2 and {s.full_name for s in sources} == {"Logo"}
    assert next(s for s in sources if s.source_kind == "binary").binary_content == body.binary_content
    assert owner(build_graph(sources))["coverage"] == "semantic"
