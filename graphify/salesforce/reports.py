"""Verified Analytics report-type column aliases, not report execution data."""
from __future__ import annotations

import json
import re
from urllib.parse import unquote

from .model import Facts


def parse_report_type_status(facts: Facts) -> None:
    try:
        data = json.loads(facts.source.content)
    except ValueError:
        data = None
    requested = unquote(facts.source.path.split("/reportTypes/", 1)[-1].rsplit("/", 1)[0])
    if (not isinstance(data, dict) or data.get("type") != requested
            or facts.source.full_name not in {requested, requested.removesuffix("__c")}
            or data.get("status") not in {"complete", "failed", "not_returned", "inaccessible"}):
        facts.level = "partial"
        facts.issue("report_type_describe_status_invalid")
        return
    facts.nodes[facts.source.component_id]["report_type_api_status"] = data
    if data["status"] != "complete":
        facts.level = "partial"
        facts.issue("report_type_describe_" + data["status"], http_status=data.get("httpStatus"))


def parse_report_type_describe(facts: Facts) -> None:
    try:
        data = json.loads(facts.source.content)
    except ValueError:
        facts.level = "partial"
        facts.issue("report_type_describe_parse_error")
        return
    requested = unquote(facts.source.path.split("/reportTypes/", 1)[-1].rsplit("/", 1)[0])
    metadata = data.get("reportMetadata", {}) if isinstance(data, dict) else {}
    report_type = metadata.get("reportType", {}) if isinstance(metadata, dict) else {}
    definition = data.get("reportTypeMetadata", {}) if isinstance(data, dict) else {}
    if (not isinstance(report_type, dict) or report_type.get("type") != requested
            or facts.source.full_name not in {requested, requested.removesuffix("__c")}
            or not isinstance(definition, dict) or not isinstance(definition.get("categories"), list)):
        facts.level = "partial"
        facts.issue("report_type_describe_identity_mismatch")
        return
    component = facts.nodes[facts.source.component_id]
    component["aliases"] = [requested]
    component["schema_roots"] = sorted({obj["apiName"] for obj in definition.get("objects", [])
                                        if isinstance(obj, dict) and isinstance(obj.get("apiName"), str)
                                        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", obj["apiName"])})
    component["report_columns_source"] = {"source_file": facts.source.path, "source_sha": facts.source_sha}
    columns = {}
    cursor = 0
    for category in definition["categories"]:
        if not isinstance(category, dict) or not isinstance(category.get("columns"), dict):
            continue
        for alias, info in category["columns"].items():
            if not isinstance(info, dict) or not isinstance(alias, str) or len(alias) > 1024:
                continue
            paths = list(dict.fromkeys(info.get(key) for key in ("fullyQualifiedName", "entityColumnName")
                                      if isinstance(info.get(key), str) and
                                      re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+", info[key])))
            # Computed columns (RowCount/CDF1/etc.) are not sObject fields.
            if not paths:
                continue
            match = re.search(re.escape(json.dumps(alias)) + r"\s*:", facts.source.content[cursor:])
            offset = cursor + match.start() if match else 0
            if match:
                cursor += match.end()
            columns.setdefault(alias.casefold(), []).append({"paths": paths, "line": facts.source.content.count("\n", 0, offset) + 1})
    component["report_columns"] = columns
    # Available columns are definitions, NOT evidence that a report uses them.
    # Only a report's actual XML references generate field-usage edges.
    facts.level = "semantic"
