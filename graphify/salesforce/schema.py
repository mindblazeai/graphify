"""Facts from captured Salesforce REST describe responses (never inferred names)."""
from __future__ import annotations

import json
import re

from .model import Facts


def parse_describe(facts: Facts) -> None:
    try:
        data = json.loads(facts.source.content)
    except ValueError:
        facts.level = "partial"
        facts.issue("describe_parse_error")
        return
    if (not isinstance(data, dict) or data.get("name") != facts.source.full_name
            or not isinstance(data.get("fields"), list)):
        facts.level = "partial"
        facts.issue("describe_identity_mismatch")
        return
    facts.level = "semantic"
    component = facts.nodes[facts.source.component_id]
    component["child_relationships"] = [
        {k: rel[k] for k in ("relationshipName", "childSObject", "field")}
        for rel in data.get("childRelationships", [])
        if isinstance(rel, dict) and all(isinstance(rel.get(k), str) and rel[k]
            for k in ("relationshipName", "childSObject", "field"))
    ]
    cursor = 0
    for field in data["fields"]:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            continue
        name = field["name"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        # Stored, pretty-printed API response: anchor the real declaration.
        pattern = r'"name"\s*:\s*' + re.escape(json.dumps(name))
        match = re.search(pattern, facts.source.content[cursor:])
        offset = cursor + match.start() if match else 0
        if match:
            cursor += match.end()
        line = facts.source.content.count("\n", 0, offset) + 1
        full = data["name"] + "." + name
        targets = [v for v in field.get("referenceTo", []) if isinstance(v, str)]
        nid = facts.declare("CustomField", full, line, source_kind="api",
                            data_type=field.get("type", ""), reference_to=targets,
                            parent_relationship_name=field.get("relationshipName") or "")
        facts.ref(facts.source.component_id, "CustomField", full, "contains", line)
        for target in targets:
            facts.ref(nid, "CustomObject", target, "references_object", line)
