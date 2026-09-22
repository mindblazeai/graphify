from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Callable

from .model import ENGINE_VERSION, SCHEMA_VERSION, Facts, Source, node_id, salesforce_id
from .registry import identify

MAX_SOURCE_BYTES = 32 * 1024 * 1024


def extract_facts(source: Source) -> dict:
    facts = Facts(source)
    if source.source_kind == "catalog":
        facts.level = "catalog"
        facts.issue("source_not_retrieved", metadata_type=source.metadata_type)
    elif source.metadata_type in {"ApexClass", "ApexTrigger", "ApexPage", "ApexComponent"} and source.content.strip() == "(hidden)":
        # Tooling returns this literal for inaccessible managed code. It is
        # an availability signal, not malformed Apex/XML or an empty program.
        facts.level = "catalog"
        facts.nodes[source.component_id]["source_kind"] = "hidden"
        facts.issue("source_hidden_by_salesforce", metadata_type=source.metadata_type)
    elif source.source_kind == "binary":
        facts.issue("binary_content_not_parsed", metadata_type=source.metadata_type)
    elif len(source.content.encode()) > MAX_SOURCE_BYTES:
        facts.level = "unparsed"
        facts.issue("source_size_limit", max_bytes=MAX_SOURCE_BYTES)
    elif source.path.startswith("salesforce-api/sobjects/") and source.path.endswith("/describe.json"):
        from .schema import parse_describe
        parse_describe(facts)
    elif source.metadata_type == "PermissionSet" and source.path.startswith("salesforce-api/permissions/"):
        from .permissions import parse_permissions
        parse_permissions(facts)
    elif source.metadata_type == "ReportType" and source.path.startswith("salesforce-api/reportTypes/") and source.path.endswith("/describe.json"):
        from .reports import parse_report_type_describe
        parse_report_type_describe(facts)
    elif source.metadata_type == "ReportType" and source.path.startswith("salesforce-api/reportTypes/") and source.path.endswith("/status.json"):
        from .reports import parse_report_type_status
        parse_report_type_status(facts)
    elif source.metadata_type == "StaticResource" and source.path.endswith(".resource"):
        from .asset_payloads import parse_resource_payload
        parse_resource_payload(facts)
    elif source.path.endswith((".cls", ".trigger", ".soql", ".sosl")):
        from .apex import parse_apex
        parse_apex(facts)
    elif source.path.endswith((".js", ".ts")):
        from .lightning import parse_javascript
        parse_javascript(facts)
    elif (source.metadata_type == "EmailTemplate" and not source.path.endswith("-meta.xml")) or (
        source.path.endswith((".html", ".cmp", ".app", ".page", ".component", ".evt", ".intf"))
        and source.metadata_type in {"LightningComponentBundle", "AuraDefinitionBundle", "ApexPage", "ApexComponent"}
    ):
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
                include_facts: bool = False, node_filter: Callable[[dict], bool] | None = None) -> dict:
    previous_facts = previous_facts or {}
    facts_by_path = {}
    fact_sequence = []
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
        fact_sequence.append(fact)
    from .asset_payloads import paired_asset_facts
    for fact in paired_asset_facts(fact_sequence):
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
                for k in ("reference_to", "relationship_name", "data_type", "annotations",
                          "related_object", "recipient_object", "is_test", "child_relationships",
                          "parent_relationship_name", "report_type", "report_columns", "report_columns_source", "report_type_api_status", "schema_roots",
                          "salesforce_id", "permission_api_status", "permission_api_status_source", "apex_fields"):
                    if node.get(k):
                        existing[k] = node[k]
                if node.get("aliases"):
                    existing["aliases"] = sorted(set(existing.get("aliases", [])) | set(node["aliases"]))
                # CSS/sidecars can sort before the semantic file in a bundle.
                # Keep any partial/unparsed warning, otherwise the best parser
                # actually used for that component determines its coverage.
                rank = {"catalog": 0, "structural": 1, "semantic": 2, "partial": 3, "unparsed": 4}
                if rank.get(node.get("coverage"), 0) > rank.get(existing.get("coverage"), 0):
                    existing["coverage"] = node["coverage"]
        references.extend(fact["references"])
        diagnostics.extend(fact["diagnostics"])
        # Corpus-dependent identity checks below must not poison reusable syntax
        # coverage when a declaration disappears and later becomes available.
        coverage.append(dict(fact["coverage"]))

    # Parent files/Describe responses may declare package-owned children even
    # when the parent itself is in scope. Filter declarations BEFORE binding;
    # incoming references retain unresolved evidence, never an out-of-scope
    # target's source. Syntax facts stay reusable when the scope changes.
    if node_filter is not None:
        nodes = {nid: n for nid, n in nodes.items() if node_filter(n)}
        references = [ref for ref in references if ref["source"] in nodes]

    # listMetadata may expose a leaf-folder name while a verified retrieve
    # returns its full hierarchy. Preserve the catalog identity/deep link and
    # bind the exact source-path alias; never strip folders or guess basenames.
    for source in sources:
        if source.source_kind != "source" or source.metadata_type not in {"Report", "Dashboard", "EmailTemplate", "Document"}:
            continue
        identified = identify(source.path)
        if identified and identified[0] == source.metadata_type and identified[1].casefold() != source.full_name.casefold():
            node = nodes.get(source.component_id)
            if node is not None:
                node["aliases"] = sorted(set(node.get("aliases", [])) | {identified[1]})

    index: dict[tuple[str, str], list[dict]] = {}
    methods: dict[str, list[dict]] = {}
    by_salesforce_id: dict[str, list[dict]] = {}
    fields = [n for n in nodes.values() if n["kind"] == "CustomField"]
    for n in nodes.values():
        for name in {n["name"].casefold(), *(a.casefold() for a in n.get("aliases", []))}:
            index.setdefault((n["kind"].casefold(), name), []).append(n)
        if n["kind"] == "ApexMethod":
            methods.setdefault((n["owner_type"] + "." + n["member_name"]).casefold(), []).append(n)

    def lookup(kind: str, name: str, namespace="") -> list[dict]:
        kinds = [kind]
        if kind == "Type":
            kinds = ["ApexClass", "ApexInterface", "ApexEnum", "CustomObject"]
        names = [name]
        if kind in {"ApexClass", "ApexTrigger"} and "__" in name:
            # Metadata XML uses namespace__Class while Apex uses namespace.Class.
            names.insert(0, name.replace("__", ".", 1))
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

    # FieldDefinition.DurableId is Object.00N... (standard objects) or
    # 01I....00N... (custom objects), not a bare CustomField ID. Keep the
    # independently supplied catalog identity even when a richer declaration
    # inside its parent XML replaces the catalog node. Only the current scoped
    # inventory can supply identities: an excluded package stays unresolved.
    object_ids: dict[str, set[str]] = {}
    for source in sources:
        sfid = salesforce_id(source.salesforce_id)
        if source.metadata_type == "CustomObject" and sfid and sfid.startswith("01I"):
            object_ids.setdefault(source.component_id, set()).add(sfid)
    identities: dict[str, dict[str, str]] = {}
    identity_conflicts = set()
    for n in nodes.values():
        sfid = salesforce_id(n.get("salesforce_id"))
        if sfid and n["id"] == n.get("component_id"):
            identities.setdefault(n["id"], {})[sfid] = n["salesforce_id"]
    for source in sources:
        n = nodes.get(source.component_id)
        if n is None:
            continue
        raw = source.salesforce_id
        sfid = salesforce_id(raw)
        if not sfid and source.metadata_type == "CustomField" and isinstance(raw, str):
            parent, dot, field_id = raw.partition(".")
            obj, separator, _ = source.full_name.rpartition(".")
            fid = salesforce_id(field_id)
            if dot and separator and fid and fid.startswith("00N"):
                parents = lookup("CustomObject", obj, source.namespace)
                if len(parents) == 1:
                    parent_id = salesforce_id(parent)
                    if parent.startswith("01I"):
                        matches = parent_id is not None and object_ids.get(parents[0]["id"]) == {parent_id}
                    else:
                        matches = parent.casefold() == parents[0]["name"].casefold()
                    if matches:
                        sfid, raw = fid, field_id
        if sfid:
            identities.setdefault(n["id"], {})[sfid] = raw
    for nid, candidates in identities.items():
        node = nodes[nid]
        if len(candidates) != 1:
            # Conflicting catalog/source identities must not resolve either ID.
            node.pop("salesforce_id", None)
            identity_conflicts.add((node["component_id"], node["source_file"]))
            diagnostics.append({"code": "metadata_salesforce_identity_conflict",
                                "source_file": node["source_file"], "line": node["line"],
                                "metadata_type": node["kind"]})
            continue
        sfid, raw = next(iter(candidates.items()))
        node["salesforce_id"] = raw
        by_salesforce_id.setdefault(sfid, []).append(node)

    # A large org can have tens of thousands of relationship references.
    # Scanning every field for each segment made binding quadratic (over 100M
    # dict reads in the live corpus). Build the reverse schema index once.
    child_relationships: dict[tuple[str, str], set[str]] = {}
    parent_relationships: dict[tuple[str, str], list[dict]] = {}
    for node in nodes.values():
        for relationship in node.get("child_relationships", []):
            child_relationships.setdefault((node["name"].casefold(), relationship["relationshipName"].casefold()), set()).add(relationship["childSObject"])
    for f in fields:
        parent_name = f.get("parent_relationship_name", "").casefold()
        if parent_name:
            parent_relationships.setdefault((f["name"].split(".")[0].casefold(), parent_name), []).append(f)
        relationship = f.get("relationship_name", "").casefold()
        if not relationship:
            continue
        for parent in f.get("reference_to", []):
            child_relationships.setdefault((parent.casefold(), relationship), set()).add(f["name"].split(".")[0])

    def child_objects(name: str) -> list[str]:
        obj, _, rel = name.rpartition(".")
        return sorted(child_relationships.get((obj.casefold(), rel.casefold()), set()))

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
                described = parent_relationships.get((obj.casefold(), part.casefold()), [])
                if last:
                    matched.extend(exact or described)
                    continue
                for f in described:
                    next_objects.extend(f.get("reference_to", []))
                    if intermediates is not None:
                        intermediates.append(f)
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

    def resolve(ref, intermediates=None):
        kind, name, ns = ref["target_kind"], ref["target_name"], ref.get("namespace", "")
        if kind == "Audience" and "target_audience_container" in ref:
            return [n for n in lookup(kind, name, ns)
                    if n.get("audience_container", "").casefold() == ref["target_audience_container"].casefold()]
        if kind == "NotificationType":
            # Standard/provider notification names are not custom declarations.
            return lookup("CustomNotificationType", name, ns)
        if kind == "NotificationApp":
            # Metadata API's ECA precedence rule applies to exact API names.
            return lookup("ExternalClientApplication", name, ns) or lookup("ConnectedApp", name, ns)
        if "metadata_name_or_id" in ref:
            return lookup(kind, name, ns) + [n for n in by_salesforce_id.get(salesforce_id(ref["metadata_name_or_id"]), []) if n["kind"] == kind]
        if "target_salesforce_id" in ref:
            # Never fall back to a display name or ID prefix. These declarations
            # were independently supplied in the same scoped source inventory.
            matches = [n for n in by_salesforce_id.get(salesforce_id(ref["target_salesforce_id"]), [])
                       if kind == "SalesforceMetadataId" or n["kind"] == kind]
            if ref.get("target_object"):
                objects = {n["name"].casefold() for n in lookup("CustomObject", ref["target_object"], ns)}
                matches = [n for n in matches if n["name"].rsplit(".", 1)[0].casefold() in objects]
            return matches
        if kind == "ReportColumn":
            report = nodes.get(ref["source"], {})
            type_name = report.get("report_type", "")
            ref["report_type"] = type_name
            types = lookup("ReportType", type_name, ns) or (lookup("ReportType", type_name[:-3], ns) if type_name.endswith("__c") else [])
            raw = ref.get("report_column", name)
            candidates = {raw.casefold(), raw.replace("$", ".").casefold(), raw.replace("$", "").casefold()}
            resolved, evidence, routes = [], [], []
            for report_type in types:
                mappings = report_type.get("report_columns", {})
                aliases = {raw.casefold()} if raw.casefold() in mappings else candidates
                for alias in sorted(aliases):
                    for entry in mappings.get(alias, []):
                        for path in entry["paths"]:
                            path_fields = []
                            found = field_path(path, ns, path_fields)
                            if found:
                                resolved.extend(found)
                                routes.append(path_fields)
                                evidence.append({**report_type["report_columns_source"], "line": entry["line"],
                                                 "status": report_type.get("report_type_api_status", {}).get("status", "captured")})
                                break
            if resolved:
                ref["binding_evidence"] = sorted({(e["source_file"], e["line"]): e for e in evidence}.values(), key=lambda e: (e["source_file"], e["line"]))
                unique_routes = {tuple(f["id"] for f in route): route for route in routes}
                if intermediates is not None and len(unique_routes) == 1 and len({n["id"] for n in resolved}) == 1:
                    intermediates.extend(next(iter(unique_routes.values())))
                return resolved
            # Preserve previously supported explicit object.field XML, but
            # never infer an object from a bare report alias or a label.
            return field_path(raw.replace("$", "."), ns, intermediates)
        if kind == "FieldPath":
            found = field_path(name, ns, intermediates)
            if raw := ref.get("field_name_or_id"):
                # ServiceChannel permits either a standard field name or a
                # custom field ID. Resolve against independently supplied names
                # AND exact IDs; do not infer an ID from its prefix or length.
                objects = {n["name"].casefold() for n in lookup("CustomObject", ref.get("field_object", ""), ns)}
                found.extend(n for n in by_salesforce_id.get(salesforce_id(raw), [])
                             if n["kind"] == "CustomField" and n["name"].rsplit(".", 1)[0].casefold() in objects)
            if not found and ref.get("context_object") and not lookup("CustomObject", name.split(".")[0], ns):
                found = field_path(ref["context_object"] + "." + name, ns, intermediates)
            return found
        if kind == "ReportType" and name.endswith("__c"):
            return lookup(kind, name, ns) or lookup(kind, name[:-3], ns)
        if kind == "TemplateField":
            alias, _, path = name.partition(".")
            component = nodes.get(ref["source"], {})
            obj = component.get("related_object" if alias.casefold() == "relatedto" else "recipient_object")
            return field_path(obj + "." + path, ns) if obj else []
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

    # Receiver syntax stays in reusable source facts; its type depends on the
    # CURRENT scoped declaration inventory, never on a previous binding result.
    from .apex_types import ReceiverBinder
    binder = ReceiverBinder(nodes, lookup, resolve_method, field_path, parent_types)
    completed = set()
    def reference_key(ref):
        target = ref["target_name"]
        signature = ()
        if ref["target_kind"] == "ApexMethod":
            found = resolve_method(ref)
            if len(found) == 1:
                target = found[0]["id"]
            else:
                signature = (ref.get("arity"),tuple(ref.get("argument_types",[])))
        return (ref["source"],ref["target_kind"],target,ref["relation"],ref["source_file"],
                ref["line"],ref.get("apex_member"),signature)
    known_references = {reference_key(ref) for ref in references}
    for fact in facts_by_path.values():
        for use in fact.get("apex_deferred", []):
            if use["source"] not in nodes:
                continue
            done, inferred = binder.bind(use)
            if done:
                completed.add(use["key"])
            for ref in inferred:
                key = reference_key(ref)
                if key not in known_references:
                    references.append(ref)
                    known_references.add(key)
    if completed:
        diagnostics = [d for d in diagnostics if d.get("deferred_key") not in completed]
        complete_sources = {
            (node_id(f["coverage"]["metadata_type"], f["coverage"]["full_name"]), f["coverage"]["source_file"])
            for f in facts_by_path.values() if f.get("apex_deferred")
            and f["coverage"]["level"] == "partial"
            and all(d.get("deferred_key") in completed for d in f["diagnostics"])
        }
        for entry in coverage:
            if (node_id(entry["metadata_type"],entry["full_name"]),entry["source_file"]) in complete_sources:
                entry["level"] = "semantic"
        still_partial = {node_id(c["metadata_type"],c["full_name"]) for c in coverage
                         if c["level"] in {"partial","unparsed"}}
        for node in nodes.values():
            if ((node.get("component_id"),node.get("source_file")) in complete_sources
                    and node.get("component_id") not in still_partial and node.get("coverage")=="partial"):
                node["coverage"] = "semantic"

    edges = {}
    unverified = identity_conflicts
    identity_issues = {}
    for original in references:
        ref = dict(original)
        if section := ref.get("permission_section"):
            component = nodes[ref["source"]]
            status = component.get("permission_api_status", {})
            state = (status.get("sections", {}).get(section, {}).get("status", "unknown")
                     if status.get("status") in {"complete", "partial"} else status.get("status", "unknown"))
            ref["permission_capture_status"] = state
            if proof := component.get("permission_api_status_source"):
                ref["binding_evidence"] = [{**proof, "status": state}]
        # A relationship traversal depends on its lookup field as well as the
        # final field. Preserve both without treating them as ambiguous targets.
        intermediate_fields = []
        matches = resolve(ref, intermediate_fields)
        for intermediate in {f["id"]: f for f in intermediate_fields}.values():
            edge = {**ref, "target": intermediate["id"], "relation": "traverses",
                    "resolution": "resolved", "confidence": "INFERRED", "weight": 1.0,
                    "_origin": "salesforce"}
            key = hashlib.sha256(json.dumps(edge, sort_keys=True).encode()).hexdigest()[:32]
            edges[key] = {"id": key, **edge}
        matches = list({m["id"]: m for m in matches}.values())
        resolution = "resolved" if len(matches) == 1 else "ambiguous" if matches else "unresolved"
        if len(matches) == 1:
            target = matches[0]["id"]
            if "target_salesforce_id" in ref:
                ref["target_name"] = matches[0]["name"]
        else:
            kind = ref["target_kind"]
            name = ref["target_name"]
            context = ref.get("report_type", "") + ":" if kind == "ReportColumn" else ""
            target = node_id("UnresolvedReference", f"{ref.get('namespace', '')}:{kind}:{context}{name}")
            if "target_salesforce_id" in ref:
                # node_id intentionally folds API names, but must not collapse
                # unknown record IDs which differ only in case.
                exact = salesforce_id(ref["target_salesforce_id"]) or ref["target_salesforce_id"]
                target = node_id("UnresolvedReference", kind) + ":id:" + hashlib.sha256(exact.encode()).hexdigest()[:32]
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
        if ref.get("identity_contract") and resolution != "resolved":
            component = nodes[ref["source"]]["component_id"]
            unverified.add((component, ref["source_file"]))
            identity_issues[key] = {"code": "metadata_identity_unverified", "source_file": ref["source_file"],
                                    "line": ref["line"], "identity_contract": ref["identity_contract"],
                                    "target_kind": ref["target_kind"], "target_name": ref["target_name"]}
        if resolution == "resolved" and ref["relation"] == "owned_by_profile":
            # A verified ProfileId makes the backing permission set a structural
            # member of that profile. Profile exploration can follow its grants
            # just as class exploration follows its declared methods.
            inverse = {**edge, "source": target, "target": ref["source"],
                       "target_kind": "PermissionSet", "target_name": nodes[ref["source"]]["name"],
                       "target_salesforce_id": nodes[ref["source"]]["salesforce_id"], "relation": "contains"}
            inverse_key = hashlib.sha256(json.dumps(inverse, sort_keys=True).encode()).hexdigest()[:32]
            edges[inverse_key] = {"id": inverse_key, **inverse}

    # Translation aliases and asset paths require verified metadata identity;
    # syntactically plausible strings alone cannot close their coverage gap.
    diagnostics.extend(identity_issues.values())
    if unverified:
        for entry in coverage:
            key = (node_id(entry["metadata_type"], entry["full_name"]), entry["source_file"])
            if key in unverified and entry["level"] == "semantic":
                entry["level"] = "partial"
        for node in nodes.values():
            if (node.get("component_id"), node.get("source_file")) in unverified and node.get("coverage") == "semantic":
                node["coverage"] = "partial"

    totals = {level: sum(c["level"] == level for c in coverage)
              for level in ("semantic", "structural", "catalog", "partial", "unparsed")}
    result = {
        "schema_version": SCHEMA_VERSION, "engine_version": ENGINE_VERSION,
        # Keep repeated source identities adjacent. Hash-ordering scattered
        # each file's evidence across the entire snapshot, defeating bounded
        # compression dictionaries and making cold network loads needlessly big.
        "nodes": sorted(nodes.values(), key=lambda n: (n.get("source_file", ""), n.get("line", 0), n["id"])),
        "edges": sorted(edges.values(), key=lambda e: (e.get("source_file", ""), e["source"], e.get("line", 0), e["id"])),
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
