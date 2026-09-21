"""Generate the parser's type registry from Salesforce SDR's official registry.

Usage: python scripts/build_salesforce_registry.py /path/to/metadataRegistry.json
"""
import hashlib
import json
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_bytes()
raw = json.loads(source)
types = {}
for value in raw["types"].values():
    if not value.get("directoryName"):
        continue
    types[value["name"]] = {
        "directory": value["directoryName"], "suffix": value.get("suffix", ""),
        "in_folder": bool(value.get("inFolder")),
        "adapter": value.get("strategies", {}).get("adapter", "default"),
        "children": {k: {"name": v["name"], "directory": v.get("directoryName", ""),
                         "suffix": v.get("suffix", ""),
                         "xml_element": v.get("xmlElementName", v.get("directoryName", "")),
                         "ignore_parent_name": bool(v.get("ignoreParentName"))}
                     for k, v in value.get("children", {}).get("types", {}).items()},
    }
dest = Path(__file__).resolve().parents[1] / "graphify/salesforce/registry.json"
dest.write_text(json.dumps({"source": "@salesforce/source-deploy-retrieve",
                            "source_version": sys.argv[2] if len(sys.argv) > 2 else "unknown",
                            "source_sha256": hashlib.sha256(source).hexdigest(),
                            "copyright": "Copyright (c) 2026 Salesforce, Inc. All rights reserved.",
                            "license": "Apache-2.0; normalized derivative, see NOTICE",
                            "types": types}, sort_keys=True, indent=2) + "\n")
print(f"Generated {len(types)} metadata types")
