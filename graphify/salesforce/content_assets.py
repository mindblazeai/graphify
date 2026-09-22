"""ContentAsset envelope contracts; never resolve client filenames as metadata.

Only Original, single-version raster files can pair with the image validator.
ZippedVersions needs an independent archive/version analysis, not a successful
decode of one image. Reserved relationship names do not establish identities.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath


def parse_content_asset(facts, root, *, issue, scalar, ref, children):
    for tag in ("language", "masterLabel"):
        scalar(root, tag, required=True)
    explicit = scalar(root, "fullName", required=bool(children(root, "fullName")))
    if explicit and explicit.text.strip() != facts.source.full_name:
        issue("asset_descriptor_identity_mismatch", explicit)
    if children(root, "content"):
        issue("asset_inline_content_unverified")

    def boolean(node, tag):
        value = scalar(node, tag, required=bool(children(node, tag)))
        if value and value.text.strip() not in {"true", "false", "1", "0"}:
            issue("asset_boolean_invalid", value, property=tag)

    boolean(root, "isVisibleByExternalUsers")
    ref(scalar(root, "originNetwork"), "Network", "belongs_to")
    relationships = children(root, "relationships")
    if len(relationships) > 1:
        issue("asset_relationships_ambiguous")
    for container in relationships:
        if len(children(container, "organization")) > 1:
            issue("asset_organization_link_ambiguous", container)
        for link in container.children:
            if link.tag not in {"emailTemplate", "insightsApplication", "network", "organization", "workspace"}:
                continue
            access = scalar(link, "access", required=True)
            if access and access.text.strip() not in {"VIEWER", "COLLABORATOR", "INFERRED"}:
                issue("asset_link_access_unsupported", access)
            boolean(link, "isManagingWorkspace")
            # The Metadata API explicitly reserves link.name for future use.
            name = scalar(link, "name")
            if link.tag != "organization" or name:
                issue("asset_link_identity_unverified", link)

    declared_format = scalar(root, "format", required=bool(children(root, "format")))
    original = not children(root, "format") or (declared_format and declared_format.text.strip() == "Original")
    if declared_format and declared_format.text.strip() not in {"Original", "ZippedVersions"}:
        issue("asset_format_unsupported", declared_format)
    versions = children(root, "versions")
    if len(versions) != 1 or not children(versions[0], "version"):
        issue("asset_versions_missing_or_ambiguous")
    version_entries = [entry for container in versions for entry in children(container, "version")]
    if original and len(version_entries) > 1:
        issue("asset_original_version_ambiguous")
    if not original:
        issue("asset_version_packaging_not_analyzed")
    version_data = []
    for entry in version_entries:
        number = scalar(entry, "number", required=True)
        path = scalar(entry, "pathOnClient", required=True)
        zip_entry = scalar(entry, "zipEntry")
        if original and zip_entry:
            issue("asset_original_zip_entry_conflict", zip_entry)
        if number and not re.fullmatch(r"[1-9][0-9]{0,8}", number.text.strip()):
            issue("asset_version_number_unsupported", number)
        version_data.append((number, path))

    if (original and len(versions) == 1 and len(version_data) == 1
            and facts.source.path.endswith(".asset-meta.xml")):
        number, path = version_data[0]
        if number and path:
            from .image_payloads import IMAGE_EXTENSIONS, IMAGE_MIME_TYPES
            image_format = IMAGE_EXTENSIONS.get(PurePosixPath(path.text.strip()).suffix.lower())
            facts.asset_descriptor = {
                "payload_path": facts.source.path.removesuffix("-meta.xml"),
                "content_type": next(iter(IMAGE_MIME_TYPES[image_format])) if image_format else "",
                "asset_version": number.text.strip(),
            }
