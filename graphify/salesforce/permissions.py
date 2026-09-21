"""Captured direct permission grants, not effective user-access evaluation."""
from __future__ import annotations

import json
import re

from .model import Facts, salesforce_id

API_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
PERMISSIONS = {
    "FieldPermissions": {"PermissionsRead": "readable", "PermissionsEdit": "editable"},
    "ObjectPermissions": {"PermissionsRead": "allowRead", "PermissionsCreate": "allowCreate",
                          "PermissionsEdit": "allowEdit", "PermissionsDelete": "allowDelete",
                          "PermissionsViewAllRecords": "viewAllRecords", "PermissionsModifyAllRecords": "modifyAllRecords",
                          "PermissionsViewAllFields": "viewAllFields"},
}
STATES = {"complete", "partial", "failed", "not_returned", "inaccessible"}


def parse_permissions(facts: Facts) -> None:
    parts = facts.source.path.split("/")
    sfid = salesforce_id(facts.source.salesforce_id)
    try:
        data = json.loads(facts.source.content)
    except ValueError:
        data = None
    if (len(parts) != 4 or not sfid or salesforce_id(parts[2]) != sfid
            or not isinstance(data, dict) or data.get("apiVersion") != "v63.0"):
        facts.level = "partial"
        facts.issue("permission_api_identity_invalid")
        return
    section = parts[-1].removesuffix(".json")
    component = facts.nodes[facts.source.component_id]
    lines = facts.source.content.splitlines()
    if section == "status":
        if (salesforce_id(data.get("permissionSetId")) != sfid or data.get("fullName") != facts.source.full_name
                or data.get("status") not in STATES or not isinstance(data.get("sections"), dict)
                or any(not isinstance(v, dict) or v.get("status") not in STATES for v in data["sections"].values())):
            facts.level = "partial"
            facts.issue("permission_api_status_invalid")
            return
        component["permission_api_status"] = data
        component["permission_api_status_source"] = {"source_file": facts.source.path, "source_sha": facts.source_sha,
            "line": next((i for i, line in enumerate(lines, 1) if re.match(r'\s*"status"\s*:', line)), 1)}
        if data["status"] != "complete":
            facts.level = "partial"
            facts.issue("permission_api_" + data["status"], sections=data["sections"])
        return

    owner = data.get("permissionSet")
    if not isinstance(owner, dict):
        owner = {}
    name = owner.get("Name")
    namespace = owner.get("NamespacePrefix")
    if namespace and (not isinstance(namespace, str) or not API_NAME.fullmatch(namespace)):
        facts.level = "partial"
        facts.issue("permission_api_owner_invalid")
        return
    if namespace and isinstance(name, str) and not name.startswith(namespace + "__"):
        name = namespace + "__" + name
    if (data.get("complete") is not True or salesforce_id(owner.get("Id")) != sfid
            or name != facts.source.full_name or owner.get("IsOwnedByProfile") is not True
            or not salesforce_id(owner.get("ProfileId"))):
        facts.level = "partial"
        facts.issue("permission_api_owner_invalid")
        return
    if section == "PermissionSet":
        facts.ref(facts.source.component_id, "Profile", owner["ProfileId"], "owned_by_profile",
                  target_salesforce_id=owner["ProfileId"], permission_section=section)
        facts.level = "partial"
        facts.issue("permission_api_direct_grants_only")
        return
    if section not in {*PERMISSIONS, "SetupEntityAccess"} or not isinstance(data.get("records"), list):
        facts.level = "partial"
        facts.issue("permission_api_section_invalid")
        return
    facts.level = "semantic"
    # Linear-time line indexing: count('\n', 0, offset) for each of thousands
    # of records would repeatedly rescan the same large permission document.
    locations = {}
    for i, line in enumerate(lines, 1):
        if match := re.match(r'\s*"Id"\s*:\s*"([A-Za-z0-9]+)"', line):
            locations[match[1]] = i
    seen = set()
    for row in data["records"]:
        rid = salesforce_id(row.get("Id")) if isinstance(row, dict) else None
        line = locations.get(row.get("Id"), 1) if isinstance(row, dict) else 1
        if not rid or rid in seen or salesforce_id(row.get("ParentId")) != sfid:
            facts.level = "partial"
            facts.issue("permission_api_record_invalid", line)
            continue
        seen.add(rid)
        if section == "SetupEntityAccess":
            target = row.get("SetupEntityId")
            if not salesforce_id(target) or not isinstance(row.get("SetupEntityType"), str):
                facts.level = "partial"
                facts.issue("permission_api_setup_target_invalid", line)
                continue
            facts.ref(facts.source.component_id, "SalesforceMetadataId", target, "grants_access", line,
                      target_salesforce_id=target, setup_entity_type=row["SetupEntityType"],
                      permission_section=section, permissions={"enabled": True})
            continue
        mapping = PERMISSIONS[section]
        obj = row.get("SobjectType")
        field = row.get("Field", "")
        # Some standard FieldPermissions rows return a bare field API name.
        # SobjectType supplies its explicit context; binding still requires an
        # independently declared field, never a synthesized declaration.
        if isinstance(field, str) and API_NAME.fullmatch(field) and isinstance(obj, str):
            field = obj + "." + field
        if (not isinstance(obj, str) or not API_NAME.fullmatch(obj)
                or any(type(row.get(key)) is not bool for key in mapping)
                or (section == "FieldPermissions" and (not isinstance(field, str)
                    or not field.startswith(obj + ".") or not API_NAME.fullmatch(field[len(obj) + 1:])))):
            facts.level = "partial"
            facts.issue("permission_api_grant_invalid", line)
            continue
        permissions = {name: row[key] for key, name in mapping.items()}
        facts.ref(facts.source.component_id, "CustomField" if section == "FieldPermissions" else "CustomObject",
                  field if section == "FieldPermissions" else obj,
                  "grants_access" if any(permissions.values()) else "configures_access", line,
                  permission_section=section, permissions=permissions)
