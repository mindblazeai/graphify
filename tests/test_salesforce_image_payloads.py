"""Original binary image bytes, exact descriptor binding and safe re-binding."""
from copy import deepcopy
from dataclasses import replace
import hashlib
from io import BytesIO
import json
import struct
import zlib

from PIL import Image, ImageFile, PngImagePlugin
import pytest

from graphify.salesforce import Source, build_graph, node_id, scan_project
from graphify.salesforce import image_payloads


def pixels(fmt="PNG", size=(3, 2), **options):
    output = BytesIO()
    Image.new("RGB", size, (12, 34, 56)).save(output, format=fmt, **options)
    return output.getvalue()


def document(fmt="PNG", **overrides):
    name = "Brand/Logo.png" if fmt == "PNG" else "Brand/Logo.jpg"
    body = Source(f"documents/{name}", "", "Document", name,
                  source_kind="binary", binary_content=pixels(fmt))
    xml = Source(body.path + "-meta.xml", "<Document><internalUseOnly>false</internalUseOnly><public>true</public><name>Display name</name></Document>", "Document", name)
    return replace(body, **overrides), xml


def folder():
    return Source("catalog/DocumentFolder/Brand", "", "DocumentFolder", "Brand", source_kind="catalog")


def doc_node(graph):
    return next(n for n in graph["nodes"] if n["kind"] == "Document")


def codes(graph):
    return {d["code"] for d in graph["diagnostics"]}


@pytest.mark.parametrize("fmt", ["PNG", "JPEG"])
def test_verified_pixels_and_descriptor_keep_raw_hashes_without_serializing_bytes(fmt):
    body, xml = document(fmt)
    graph = build_graph([body, xml, folder()], include_facts=True)
    node = doc_node(graph)
    assert node["coverage"] == "semantic"
    assert node["content_analysis"] == "raster_image"
    assert node["payload_analysis"] == {"format": fmt.lower(), "width": 3, "height": 2, "frames": 1,
        "bytes": len(body.binary_content), "source_file": body.path, "source_sha": hashlib.sha256(body.binary_content).hexdigest()}
    assert node["source_sha"] == hashlib.sha256(xml.content.encode()).hexdigest()
    assert node["source_file"] == xml.path
    assert node["binding_evidence"] == [{"source_file": body.path, "source_sha": body.actual_sha, "line": 1, "status": "captured"}]
    assert {(e["relation"], e["resolution"]) for e in graph["edges"]} == {("belongs_to", "resolved")}
    assert all(f["coverage"]["level"] == "partial" for f in graph["facts"].values() if f["coverage"]["metadata_type"] == "Document")
    assert "binary_content" not in json.dumps(graph)


def test_folder_identity_is_independent_not_synthesized_from_the_filename():
    body, xml = document()
    graph = build_graph([body, xml])
    assert doc_node(graph)["coverage"] == "partial"
    assert "metadata_identity_unverified" in codes(graph)
    edge, = graph["edges"]
    assert edge["resolution"] == "unresolved" and edge["target_kind"] == "DocumentFolder"


@pytest.mark.parametrize("change", [
    {"content": "<Document><public>true</public></Document>"},
    {"content": "<Document><internalUseOnly>unknown</internalUseOnly><public>true</public></Document>"},
    {"content": "<Document><internalUseOnly>false</internalUseOnly><public>true</public><public>false</public></Document>"},
    {"content": "<Document><internalUseOnly>false</internalUseOnly><public>true</public><future>Case.Status</future></Document>"},
    {"content": "<Document><internalUseOnly>false</internalUseOnly><public>true</public><content>YQ==</content></Document>"},
    {"content": "<Document><internalUseOnly>false</internalUseOnly><public>true</public><fullName>Other</fullName></Document>"},
    {"path": "other/Logo.png-meta.xml"}, {"namespace": "package"}, {"full_name": "Brand/Other.png"},
])
def test_pixels_never_clear_other_descriptor_problems(change):
    body, xml = document()
    graph = build_graph([body, replace(xml, **change), folder()])
    assert doc_node(graph)["coverage"] == "partial"


@pytest.mark.parametrize("data", [b"", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff\xd9", b"<svg/>", b"PK\x03\x04archive", pixels()[:-5], pixels("JPEG")[:-5], pixels() + b"trailing", pixels("JPEG") + b"trailing"])
def test_invalid_unsupported_truncated_and_appended_payloads_stay_partial(data):
    body, xml = document(binary_content=data)
    graph = build_graph([body, xml, folder()])
    assert doc_node(graph)["coverage"] == "partial"
    assert "payload_analysis" not in doc_node(graph)


def test_misleading_extension_or_wrong_hash_does_not_close_coverage():
    body, xml = document(binary_content=pixels("JPEG"))
    assert doc_node(build_graph([body, xml, folder()]))["coverage"] == "partial"
    body, xml = document(content_sha="0" * 64)
    graph = build_graph([body, xml, folder()])
    assert "asset_payload_hash_mismatch" in codes(graph)
    assert doc_node(graph)["coverage"] == "partial"


@pytest.mark.parametrize("limit,value", [("MAX_IMAGE_BYTES", 2), ("MAX_IMAGE_PIXELS", 5), ("MAX_IMAGE_DIMENSION", 2)])
def test_input_and_decoded_pixel_bounds(monkeypatch, limit, value):
    monkeypatch.setattr(image_payloads, limit, value)
    body, xml = document()
    assert doc_node(build_graph([body, xml, folder()]))["coverage"] == "partial"


def test_ancillary_text_is_data_and_bounded_before_decoding(monkeypatch):
    info = PngImagePlugin.PngInfo()
    info.add_text("metadata", "Case.Status {!Contact.Secret__c}", zip=True)
    body, xml = document(binary_content=pixels(pnginfo=info))
    graph = build_graph([body, xml, folder()])
    assert doc_node(graph)["coverage"] == "semantic"
    assert len(graph["edges"]) == 1 and "Secret__c" not in json.dumps(graph)
    monkeypatch.setattr(image_payloads, "MAX_ANCILLARY_BYTES", 8)
    assert doc_node(build_graph([body, xml, folder()]))["coverage"] == "partial"


def test_crc_corruption_unknown_critical_chunks_and_animation_fail_closed():
    data = pixels()
    variants = [data[:30] + bytes([data[30] ^ 1]) + data[31:]]
    for kind in [b"UNKN", b"acTL"]:
        chunk = kind + b"\0" * 8
        variants.append(data[:33] + struct.pack(">I", 8) + chunk + struct.pack(">I", zlib.crc32(chunk)) + data[33:])
    for variant in variants:
        body, xml = document(binary_content=variant)
        assert doc_node(build_graph([body, xml, folder()]))["coverage"] == "partial"


def test_permissive_shared_decoder_settings_cannot_bypass_validation(monkeypatch):
    monkeypatch.setattr(ImageFile, "LOAD_TRUNCATED_IMAGES", True)
    body, xml = document()
    assert doc_node(build_graph([body, xml, folder()]))["coverage"] == "partial"


def test_actual_bytes_participate_in_fingerprints_and_cached_pairing_reopens():
    body, xml = document()
    before = build_graph([body, xml, folder()], include_facts=True)
    saved = deepcopy(before["facts"])
    omitted = replace(body, binary_content=None, content_sha=body.actual_sha)
    same = build_graph([omitted, xml, folder()], previous_facts=saved)
    assert same["stats"]["reused"] == 3 and doc_node(same)["coverage"] == "semantic"
    changed = replace(body, binary_content=pixels(size=(2, 2)))
    assert body.fingerprint != changed.fingerprint
    for sources in ([body, folder()], [body, xml], [replace(body, binary_content=b"broken"), xml, folder()]):
        invalid = build_graph(sources, previous_facts=saved, include_facts=True)
        assert doc_node(invalid)["coverage"] == "partial"
        restored = build_graph([body, xml, folder()], previous_facts=invalid["facts"])
        assert doc_node(restored)["coverage"] == "semantic"
    assert saved == before["facts"]


def test_duplicate_malformed_body_is_not_ignored_in_pairing():
    body, xml = document()
    graph = build_graph([body, replace(body, binary_content=b"bad"), xml, folder()])
    assert doc_node(graph)["coverage"] == "partial"
    assert "payload_analysis" not in doc_node(graph)


def test_cli_keeps_document_extensions_and_original_binary_bytes(tmp_path):
    body, xml = document()
    for source in [body, xml]:
        path = tmp_path / source.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source.binary_content if source.binary_content is not None else source.content.encode())
    found = scan_project(tmp_path)
    assert len(found) == 2
    parsed = next(s for s in found if s.source_kind == "binary")
    assert parsed.binary_content == body.binary_content and parsed.content == ""
    assert parsed.full_name == body.full_name
    assert doc_node(build_graph([*found, folder()]))["coverage"] == "semantic"


@pytest.mark.parametrize("mime,valid", [("image/png", True), ("image/jpeg", False), ("application/octet-stream", False)])
def test_static_resources_require_independent_mime_contract(mime, valid):
    body = Source("staticresources/Logo.resource", "", "StaticResource", "Logo", source_kind="binary", binary_content=pixels())
    xml = Source(body.path + "-meta.xml", f"<StaticResource><cacheControl>Public</cacheControl><contentType>{mime}</contentType></StaticResource>", "StaticResource", "Logo")
    graph = build_graph([body, xml])
    assert graph["nodes"][0]["coverage"] == ("semantic" if valid else "partial")


def test_filtered_package_and_wrong_source_kind_do_not_acquire_image_coverage():
    body, xml = document()
    graph = build_graph([replace(body, source_kind="source"), xml, folder()])
    assert "binary_source_kind_mismatch" in codes(graph)
    assert doc_node(graph)["coverage"] == "partial"
    graph = build_graph([body, xml, folder()], node_filter=lambda n: n["kind"] != "DocumentFolder")
    assert doc_node(graph)["coverage"] == "partial"


def test_lazy_loader_is_used_only_for_missing_facts_and_preserves_parse_counts():
    body, xml = document()
    omitted = replace(body, binary_content=None, content_sha=body.actual_sha)
    calls = []
    def loader(source):
        if source.source_kind != "binary":
            return source
        calls.append(source.path)
        return replace(source, binary_content=body.binary_content)
    first = build_graph([omitted, xml, folder()], load_source=loader, include_facts=True)
    assert calls == [body.path] and first["stats"]["parsed"] == 3 and first["stats"]["reused"] == 0
    calls.clear()
    same = build_graph([omitted, xml, folder()], load_source=loader, previous_facts=first["facts"])
    assert not calls and same["stats"]["reused"] == 3 and doc_node(same)["coverage"] == "semantic"


@pytest.mark.parametrize("change", [{"full_name": "Other"}, {"path": "other.png"}, {"source_kind": "source"}, {"content_sha": "0" * 64}, {"namespace": "pkg"}])
def test_lazy_loader_cannot_replace_inventoried_identity_or_hash(change):
    body, _ = document()
    with pytest.raises(ValueError, match="inventoried source"):
        build_graph([body], load_source=lambda source: replace(source, **change))
