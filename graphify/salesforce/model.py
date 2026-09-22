from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import quote

SCHEMA_VERSION = 1
ENGINE_VERSION = "salesforce-19"


def salesforce_id(value) -> str | None:
    """Salesforce's first 15 ID characters are case-SENSITIVE, unlike names."""
    return value[:15] if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?", value) else None


def node_id(kind: str, name: str) -> str:
    # Apex and Salesforce API identifiers are case-insensitive. Preserve their
    # original spelling on nodes, but never make casing a second identity.
    return f"sf:{kind.lower()}:{quote(name.casefold(), safe='._-')}"


@dataclass(frozen=True)
class Source:
    path: str
    content: str
    metadata_type: str
    full_name: str
    namespace: str = ""
    source_kind: str = "source"
    # Trusted storage may supply the already-verified content hash when an
    # unchanged source body is omitted and its existing facts will be reused.
    content_sha: str | None = None
    salesforce_id: str | None = None

    @property
    def component_id(self) -> str:
        return node_id(self.metadata_type, self.full_name)

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(
            [ENGINE_VERSION, self.path, self.content_sha or hashlib.sha256(self.content.encode()).hexdigest(), self.metadata_type,
             self.full_name, self.namespace, self.source_kind, self.salesforce_id],
            ensure_ascii=False, separators=(",", ":"),
        ).encode()).hexdigest()


class Facts:
    """Serializable per-file facts. References are resolved after all declarations."""

    def __init__(self, source: Source):
        self.source = source
        self.source_sha = hashlib.sha256(source.content.encode()).hexdigest()
        self.nodes: dict[str, dict] = {}
        self.references: list[dict] = []
        self.diagnostics: list[dict] = []
        self.apex_deferred: list[dict] = []
        self.level = "structural"
        self.declare(source.metadata_type, source.full_name, line=1,
                     source_kind=source.source_kind)

    def declare(self, kind: str, name: str, line: int = 1, **attributes) -> str:
        nid = node_id(kind, name)
        self.nodes[nid] = {
            "id": nid, "kind": kind, "name": name, "label": name,
            "component_id": self.source.component_id,
            "metadata_type": self.source.metadata_type,
            "full_name": self.source.full_name,
            "namespace": self.source.namespace,
            "source_file": self.source.path, "source_location": f"L{line}",
            "source_sha": self.source_sha,
            "line": line, "file_type": "code", "external": False,
            **attributes,
        }
        if nid == self.source.component_id and salesforce_id(self.source.salesforce_id):
            self.nodes[nid]["salesforce_id"] = self.source.salesforce_id
        return nid

    def ref(self, source_id: str, kind: str, name: str, relation: str,
            line: int = 1, **attributes) -> None:
        if not name or len(name) > 1024:
            return
        self.references.append({
            "source": source_id, "target_kind": kind, "target_name": name,
            "relation": relation, "source_file": self.source.path,
            "source_sha": self.source_sha,
            "source_location": f"L{line}", "line": line,
            "namespace": self.source.namespace, **attributes,
        })

    def issue(self, code: str, line: int = 1, **details) -> None:
        self.diagnostics.append({"code": code, "source_file": self.source.path,
                                 "line": line, **details})

    def result(self) -> dict:
        for node in self.nodes.values():
            node["coverage"] = self.level
        return {"fingerprint": self.source.fingerprint,
                "nodes": list(self.nodes.values()), "references": self.references,
                **({"apex_deferred": self.apex_deferred} if self.apex_deferred else {}),
                "diagnostics": self.diagnostics,
                "coverage": {"source_file": self.source.path,
                             "metadata_type": self.source.metadata_type,
                             "full_name": self.source.full_name,
                             "level": self.level,
                             "source_kind": self.source.source_kind}}
