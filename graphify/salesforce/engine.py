from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .model import ENGINE_VERSION, SCHEMA_VERSION, Facts, Source, node_id
from .registry import identify

MAX_SOURCE_BYTES = 32 * 1024 * 1024


def extract_facts(source: Source) -> dict:
    facts = Facts(source)
    if source.source_kind == "catalog":
        facts.level = "catalog"
        facts.issue("source_not_retrieved", metadata_type=source.metadata_type)
    elif source.source_kind == "binary":
        facts.issue("binary_content_not_parsed", metadata_type=source.metadata_type)
    elif len(source.content.encode()) > MAX_SOURCE_BYTES:
        facts.level = "unparsed"
        facts.issue("source_size_limit", max_bytes=MAX_SOURCE_BYTES)
    elif source.path.endswith((".cls", ".trigger", ".soql", ".sosl")):
        from .apex import parse_apex
        parse_apex(facts)
    elif source.path.endswith((".js", ".ts")):
        from .lightning import parse_javascript
        parse_javascript(facts)
    elif source.path.endswith((".html", ".cmp", ".app", ".page", ".component", ".evt", ".intf")) and source.metadata_type in {"LightningComponentBundle", "AuraDefinitionBundle", "ApexPage", "ApexComponent"}:
        from .lightning import parse_markup
        parse_markup(facts)
    elif source.content.lstrip().startswith("<"):
        from .metadata import parse_metadata
        parse_metadata(facts)
    elif source.path.endswith(".json"):
        from .metadata import parse_json_metadata
        parse_json_metadata(facts)
    else:
        # All retrievable files retain an identity and honest coverage even
        # when their content is binary, JSON, CSS, or a new Salesforce format.
        facts.issue("generic_content_adapter", metadata_type=source.metadata_type)
    return facts.result()


def build_graph(sources: list[Source], *, previous_facts: dict | None = None,
                include_facts: bool = False) -> dict:
    previous_facts = previous_facts or {}
    facts_by_path = {}
    nodes: dict[str, dict] = {}
    references = []
    diagnostics = []
    coverage = []
    reused = 0
    for source in sorted(sources, key=lambda s: (s.path, s.metadata_type, s.full_name)):
        # Multiple catalog entities may own slices of the same XML file.
        key = source.component_id + ":" + source.path
        old = previous_facts.get(key)
        if old and old.get("fingerprint") == source.fingerprint:
            fact = old
            reused += 1
        else:
            fact = extract_facts(source)
        facts_by_path[key] = fact
        for value in fact["nodes"]:
            node = dict(value)
            existing = nodes.get(node["id"])
            if not existing or existing.get("source_kind") == "catalog":
                nodes[node["id"]] = node
            else:
                # Bundles share a component node; append annotations and
                # source files without making the last file erase the first.
                existing.setdefault("source_files", [existing["source_file"]])
                if node["source_file"] not in existing["source_files"]:
                    existing["source_files"].append(node["source_file"])
                for k in ("reference_to", "relationship_name", "data_type", "annotations"):
                    if node.get(k):
                        existing[k] = node[k]
        references.extend(fact["references"])
        diagnostics.extend(fact["diagnostics"])
        coverage.append(fact["coverage"])

    index: dict[tuple[str, str], list[dict]] = {}
    methods: dict[str, list[dict]] = {}
    fields = [n for n in nodes.values() if n["kind"] == "CustomField"]
    for n in nodes.values():
        index.setdefault((n["kind"].casefold(), n["name"].casefold()), []).append(n)
        if n["kind"] == "ApexMethod":
            methods.setdefault((n["owner_type"] + "." + n["member_name"]).casefold(), []).append(n)

    def lookup(kind: str, name: str, namespace="") -> list[dict]:
        kinds = [kind]
        if kind == "Type":
            kinds = ["ApexClass", "ApexInterface", "ApexEnum", "CustomObject"]
        names = [name]
        if namespace and not name.startswith(namespace + "."):
            names.insert(0, namespace + "." + name)
            if kind in {"CustomObject", "CustomField", "CustomPermission", "Type"}:
                names.insert(0, namespace + "__" + name)
            if kind == "CustomField" and "." in name:
                obj, member = name.split(".", 1)
                names.insert(0, obj + "." + namespace + "__" + member)
                if obj.endswith(("__c", "__mdt", "__e")):
                    names.insert(0, namespace + "__" + obj + "." + namespace + "__" + member)
        if kind in {"CustomLabel", "CustomPermission", "StaticResource"} and "." in name:
            names.insert(0, name.replace(".", "__", 1))
        for candidate in names:
            out = [n for k in kinds for n in index.get((k.casefold(), candidate.casefold()), [])]
            if kind == "Type":
                out = [n for n in out if not n.get("container_only")]
            if out:
                return out
        return []

    def child_objects(name: str) -> list[str]:
        obj, _, rel = name.rpartition(".")
        return sorted({f["name"].split(".")[0] for f in fields
                       if obj.casefold() in [x.casefold() for x in f.get("reference_to", [])]
                       and f.get("relationship_name", "").casefold() == rel.casefold()})

    def field_path(name: str, namespace: str, intermediates: list | None = None) -> list[dict]:
        parts = name.split(".")
        if len(parts) < 2:
            return []
        objects = [parts[0]]
        # Some references are namespace.Class.member; object namespaces use
        # ns__Object__c and therefore remain a single segment here.
        for i, part in enumerate(parts[1:], 1):
            last = i == len(parts) - 1
            next_objects = []
            matched = []
            for obj in objects:
                exact = lookup("CustomField", obj + "." + part, namespace)
                if last:
                    matched.extend(exact)
                    continue
                names = [part]
                if part.endswith("__r"):
                    names.append(part[:-3] + "__c")
                else:
                    names.append(part + "Id")
                for fname in names:
                    for f in lookup("CustomField", obj + "." + fname, namespace):
                        next_objects.extend(f.get("reference_to", []))
                        if intermediates is not None:
                            intermediates.append(f)
                next_objects.extend(child_objects(obj + "." + part))
            if last:
                return list({n["id"]: n for n in matched}.values())
            objects = sorted(set(next_objects))
            if not objects:
                return []
        return []

    parent_types: dict[str, list[str]] = {}
    for ref in references:
        if ref["relation"] in {"extends", "implements"} and ref["source"] in nodes:
            owner = nodes[ref["source"]]["name"].casefold()
            parent_types.setdefault(owner, []).append(ref["target_name"])

    def resolve_method(ref):
        name, ns = ref["target_name"], ref.get("namespace", "")
        direct = lookup("ApexMethod", name, ns)
        if direct:
            return direct
        owner, sep, member = name.rpartition(".")
        if not sep:
            return []
        owners = [n["name"] for n in lookup("Type", owner, ns)] or [owner]
        seen = set()
        while owners:
            next_owners = []
            found = []
            for cls in owners:
                if cls.casefold() in seen:
                    continue
                seen.add(cls.casefold())
                found.extend(methods.get((cls + "." + member).casefold(), []))
                next_owners.extend(parent_types.get(cls.casefold(), []))
            if "arity" in ref:
                found = [n for n in found if len(n["parameters"]) == ref["arity"]]
                for i, typ in enumerate(ref.get("argument_types", [])):
                    if typ:
                        found = [n for n in found if n["parameters"][i].casefold() == typ.casefold()]
            if found:
                return found
            owners = next_owners
        return []

    aura_controllers = {}
    for ref in references:
        if ref["relation"] == "controller" and ref["target_kind"] == "ApexClass":
            aura_controllers.setdefault(ref["source"], []).append(ref["target_name"])

    def resolve(ref):
        kind, name, ns = ref["target_kind"], ref["target_name"], ref.get("namespace", "")
        if kind == "FieldPath":
            return field_path(name, ns)
        if kind == "ChildRelationship":
            return [n for obj in child_objects(name) for n in lookup("CustomObject", obj, ns)]
        if kind == "ApexMethod":
            return resolve_method(ref)
        if kind == "InvocableApex":
            classes = lookup("ApexClass", name, ns)
            names = {n["name"].casefold() for n in classes}
            return [n for group in methods.values() for n in group
                    if n["owner_type"].casefold() in names and
                    "invocablemethod" in [a.casefold() for a in n.get("annotations", [])]]
        if kind == "AuraAction":
            component = nodes.get(ref["source"], {}).get("component_id", ref["source"])
            return [n for controller in aura_controllers.get(component, [])
                    for n in resolve_method({**ref, "target_name": controller + "." + name})]
        return lookup(kind, name, ns)

    edges = {}
    for ref in references:
        # A relationship traversal depends on its lookup field as well as the
        # final field. Preserve both without treating them as ambiguous targets.
        intermediate_fields = []
        if ref["target_kind"] == "FieldPath":
            field_path(ref["target_name"], ref.get("namespace", ""), intermediate_fields)
        for intermediate in {f["id"]: f for f in intermediate_fields}.values():
            edge = {**ref, "target": intermediate["id"], "relation": "traverses",
                    "resolution": "resolved", "confidence": "INFERRED", "weight": 1.0,
                    "_origin": "salesforce"}
            key = hashlib.sha256(json.dumps(edge, sort_keys=True).encode()).hexdigest()[:32]
            edges[key] = {"id": key, **edge}
        matches = resolve(ref)
        matches = list({m["id"]: m for m in matches}.values())
        resolution = "resolved" if len(matches) == 1 else "ambiguous" if matches else "unresolved"
        if len(matches) == 1:
            target = matches[0]["id"]
        else:
            kind = ref["target_kind"]
            name = ref["target_name"]
            target = node_id("UnresolvedReference", f"{ref.get('namespace', '')}:{kind}:{name}")
            nodes.setdefault(target, {
                "id": target, "kind": kind, "name": name, "label": name,
                "external": True, "resolution": resolution,
                "candidates": [n["id"] for n in matches], "file_type": "code",
            })
        confidence = "AMBIGUOUS" if resolution == "ambiguous" else "EXTRACTED"
        if ref["relation"] in {"calls", "invokes", "reads", "writes"}:
            confidence = "INFERRED" if resolution == "resolved" else confidence
        edge = {**ref, "target": target, "resolution": resolution,
                "confidence": confidence, "weight": 1.0, "_origin": "salesforce"}
        key = hashlib.sha256(json.dumps(edge, sort_keys=True).encode()).hexdigest()[:32]
        edges[key] = {"id": key, **edge}

    totals = {level: sum(c["level"] == level for c in coverage)
              for level in ("semantic", "structural", "catalog", "partial", "unparsed")}
    result = {
        "schema_version": SCHEMA_VERSION, "engine_version": ENGINE_VERSION,
        "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
        "edges": sorted(edges.values(), key=lambda e: e["id"]),
        "coverage": coverage, "diagnostics": diagnostics,
        "stats": {"sources": len(sources), "parsed": len(sources) - reused,
                  "reused": reused, "nodes": len(nodes), "edges": len(edges),
                  "coverage": totals,
                  "resolved": sum(e["resolution"] == "resolved" for e in edges.values()),
                  "unresolved": sum(e["resolution"] == "unresolved" for e in edges.values()),
                  "ambiguous": sum(e["resolution"] == "ambiguous" for e in edges.values())},
    }
    if include_facts:
        result["facts"] = facts_by_path
    return result


def scan_project(root: Path) -> list[Source]:
    root = root.resolve()
    sources = []
    skipped_dirs = {".git", "node_modules", ".venv", "graphify-out", "dist", ".sfdx", ".sf"}
    paths = []
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in skipped_dirs and not (Path(folder) / d).is_symlink())
        paths.extend(Path(folder) / name for name in sorted(files))
    for path in sorted(paths):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        component = identify(rel)
        if component:
            if path.stat().st_size > MAX_SOURCE_BYTES:
                # A bounded marker avoids loading a huge static resource just
                # to identify it. Binary resources retain structural coverage.
                sources.append(Source(rel, "", *component, source_kind="binary"))
            else:
                data = path.read_bytes()
                try:
                    text = data.decode("utf-8")
                    source_kind = "source" if "\x00" not in text else "binary"
                except UnicodeDecodeError:
                    text, source_kind = "", "binary"
                sources.append(Source(rel, text, *component, source_kind=source_kind))
    return sources
