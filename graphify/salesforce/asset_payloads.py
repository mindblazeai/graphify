"""Bounded, non-executing resource payload analysis and descriptor pairing.

CSV is literal tabular data, not Apex, a merge template, or a metadata manifest.
An apparently valid table alone is not a contract: only an independently parsed
StaticResource descriptor declaring CSV can enable this analysis. No cell values
are retained in facts or treated as component names.
"""
from __future__ import annotations

import csv
from copy import deepcopy
from io import StringIO

MAX_CSV_BYTES = 2 * 1024 * 1024
MAX_CSV_ROWS = 100_000
MAX_CSV_COLUMNS = 256
MAX_CSV_CELL_CHARS = 16_384
CSV_MIME_TYPES = frozenset({"text/csv", "application/csv"})


def _quoted_fields_valid(text: str) -> bool:
    # csv.reader(strict=True) still accepts quotes in an unquoted field. Reject
    # that ambiguous form and text after a closing quote before parsing cells.
    state = "start"
    size = 0
    for char in text:
        if char == "\0":
            return False
        if state == "quoted":
            state = "closed" if char == '"' else "quoted"
            size += 1
        elif state == "closed" and char == '"':
            state = "quoted"
            size += 1
        elif char in ",\r\n":
            state, size = "start", 0
        elif state == "start":
            state = "quoted" if char == '"' else "unquoted"
            size = 1
        elif state == "closed" or char == '"':
            return False
        else:
            size += 1
        if size > MAX_CSV_CELL_CHARS:
            return False
    return state != "quoted"


def parse_resource_payload(facts) -> None:
    facts.level = "partial"
    facts.issue("asset_payload_descriptor_unverified")
    text = facts.source.content.removeprefix("\ufeff")
    if len(facts.source.content.encode()) > MAX_CSV_BYTES:
        facts.issue("asset_payload_size_limit", max_bytes=MAX_CSV_BYTES)
        return
    if not _quoted_fields_valid(text):
        facts.issue("asset_payload_csv_invalid")
        return
    rows = columns = 0
    try:
        for row in csv.reader(StringIO(text, newline=""), strict=True):
            rows += 1
            if (rows > MAX_CSV_ROWS or not row or len(row) > MAX_CSV_COLUMNS
                    or (columns and len(row) != columns)):
                raise ValueError("CSV bounds or inconsistent rows")
            columns = len(row)
        if rows < 2 or columns < 2:
            raise ValueError("Not the supported tabular CSV shape")
    except (csv.Error, ValueError):
        facts.issue("asset_payload_csv_invalid")
        return
    facts.asset_payload = {"format": "csv", "rows": rows, "columns": columns,
                           "source_file": facts.source.path, "source_sha": facts.source_sha}


def paired_asset_facts(facts: list[dict]) -> list[dict]:
    """Return effective facts without changing reusable single-source facts.

    Removing/changing a descriptor must reopen the gap even when its payload
    facts are reused. Exact component identity, namespace and adjacent source
    path are all required; MIME is never inferred from a basename or content.
    """
    descriptors = {}
    payload_counts = {}
    for fact in facts:
        if fact.get("asset_payload"):
            key = (fact["coverage"]["full_name"], fact["coverage"]["source_file"])
            payload_counts[key] = payload_counts.get(key, 0) + 1
        descriptor = fact.get("asset_descriptor")
        if descriptor and fact["coverage"]["metadata_type"] == "StaticResource":
            key = (fact["coverage"]["full_name"], descriptor["payload_path"])
            descriptors.setdefault(key, []).append(fact)
    replacements = {}
    for fact in facts:
        payload = fact.get("asset_payload")
        if not payload or payload.get("format") != "csv" or fact["coverage"]["metadata_type"] != "StaticResource":
            continue
        key = (fact["coverage"]["full_name"], fact["coverage"]["source_file"])
        matches = descriptors.get(key, [])
        if len(matches) != 1 or payload_counts[key] != 1:
            continue
        descriptor = matches[0]
        if descriptor["asset_descriptor"]["content_type"] not in CSV_MIME_TYPES:
            continue
        owner = fact["nodes"][0]
        primary = descriptor["nodes"][0]
        if owner["id"] != primary["id"] or owner["namespace"] != primary["namespace"]:
            continue
        proof = {k: primary[k] for k in ("source_file", "source_sha", "line")}
        for original, code in ((fact, "asset_payload_descriptor_unverified"),
                               (descriptor, "asset_payload_not_analyzed")):
            updated = deepcopy(original)
            updated["diagnostics"] = [d for d in updated["diagnostics"] if d["code"] != code]
            level = "partial" if updated["diagnostics"] else "semantic"
            updated["coverage"]["level"] = level
            for node in updated["nodes"]:
                node.update(proof, coverage=level, content_analysis="literal_csv",
                            source_files=[proof["source_file"], payload["source_file"]],
                            payload_analysis=dict(payload),
                            binding_evidence=[{**{k: payload[k] for k in ("source_file", "source_sha")},
                                               "line": 1, "status": "captured"}])
            replacements[id(original)] = updated
    return [replacements.get(id(fact), fact) for fact in facts]
