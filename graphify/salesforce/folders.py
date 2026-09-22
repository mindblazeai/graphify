"""Document folders: original Metadata API XML, not inferred directory names.

Contracts: Metadata API describeValueType(DocumentFolder), v67, and the
Salesforce-generated Folder type (forcedotcom/wsdl). User/manager recipients
are an explicit metadata-only boundary, never a reason to query User records.
"""
from __future__ import annotations

SHAPES = {
    "DocumentFolder": {
        "": "accessType folderShares name publicFolderAccess sharedTo",
        "folderShares": "accessLevel sharedTo sharedToType",
        "sharedTo": "allCustomerPortalUsers allInternalUsers allPartnerUsers channelProgramGroup channelProgramGroups group groups guestUser managerSubordinates managers portalRole portalRoleAndSubordinates queue role roleAndSubordinates roleAndSubordinatesInternal roles rolesAndSubordinates territories territoriesAndSubordinates territory territoryAndSubordinates",
    },
}
METADATA_RECIPIENTS = {"Group": "Group", "Role": "Role", "RoleAndSubordinates": "Role",
                       "RoleAndSubordinatesInternal": "Role"}
USER_RECIPIENTS = frozenset({"User", "PartnerUser", "CustomerPortalUser", "Manager", "ManagerAndSubordinatesInternal"})


def parse_folder(facts, root, *, issue, scalar, ref, children):
    owner = facts.nodes[facts.source.component_id]
    scalar(root, "name", required=True)  # Display label, never a target identity.
    explicit = scalar(root, "fullName", required=bool(children(root, "fullName")))
    if explicit and explicit.text.strip() != facts.source.full_name:
        issue("folder_identity_mismatch", explicit)
    for tag, allowed in (("accessType", {"Shared", "Public", "Hidden", "PublicInternal"}),
                         ("publicFolderAccess", {"ReadOnly", "ReadWrite"})):
        value = scalar(root, tag, required=bool(children(root, tag)))
        if value:
            if value.text.strip() not in allowed:
                issue("folder_access_value_unsupported", value, property=tag)
            else:
                owner[tag] = value.text.strip()
    for share in children(root, "folderShares"):
        access = scalar(share, "accessLevel", required=True)
        target = scalar(share, "sharedTo", required=True)
        kind = scalar(share, "sharedToType", required=True)
        if access and access.text.strip() not in {"View", "EditAllContents", "Manage"}:
            issue("folder_share_access_unsupported", access)
        if not kind or not target:
            continue
        recipient_type = kind.text.strip()
        if recipient_type in METADATA_RECIPIENTS:
            ref(target, METADATA_RECIPIENTS[recipient_type], "shared_with",
                metadata_name_or_id=target.text.strip(), identity_contract="folder_share",
                share_type=recipient_type, access_level=access.text.strip() if access else None)
        elif recipient_type in USER_RECIPIENTS:
            issue("metadata_only_identity_boundary", target, reference_type=recipient_type)
        else:
            issue("folder_share_identity_unverified", target, reference_type=recipient_type)
    # These legacy polymorphic string arrays require their own provider value
    # contract. A public-folder envelope does not excuse unresolved sharing.
    for shared in children(root, "sharedTo"):
        issue("folder_legacy_sharing_unverified", shared)
