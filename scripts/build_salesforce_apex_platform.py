"""Pin reviewed data-return signatures from Tooling API Apex system completions.

Input: an explicitly captured v67 /tooling/completions?type=apex response.
No API call, customer code, bodies, documentation or arbitrary namespaces are
copied. This is a signature catalog, not complete platform semantics.
"""
import hashlib
import json
from pathlib import Path
import re
import sys

EXPECTED_SHA = "3a8c609cccb49b2383c13a6157bdc8b802a981ff4473cfc7d95c7b5fef6a9cda"
CLASSES = "Date Datetime Time String Blob Boolean Integer Long Double Decimal Exception Url PageReference ApexPages EncodingUtil System Id".split()
DATA_TYPES = {s.casefold() for s in "void ANY Object String Blob Boolean Integer Long Double Decimal Date Datetime Time Url PageReference Id List Set Map".split()}

def supported_type(value):
    return isinstance(value, str) and len(value) <= 256 and all(
        part.casefold().removeprefix("system.") in DATA_TYPES
        for part in re.findall(r"[A-Za-z_][A-Za-z0-9_.]*", value))

def build(raw):
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_SHA:
        raise ValueError("Provider schema changed; review it before changing the pin")
    data = json.loads(raw)["publicDeclarations"]["System"]
    methods = []
    for cls in CLASSES:
        for entry in data[cls]["methods"]:
            parameters = entry["argTypes"]
            assert parameters == [p["type"] for p in entry["parameters"]]
            assert isinstance(entry["isStatic"], bool)
            if (supported_type(entry["returnType"]) and entry["returnType"].casefold() not in {"any", "object", "void"}
                    and all(supported_type(p) for p in parameters)):
                methods.append(["System." + cls, entry["name"], entry["isStatic"], parameters, entry["returnType"]])
    return {"source": "Salesforce Tooling API v67.0 /completions?type=apex", "source_sha256": digest,
            "scope": "Reviewed System data-return signatures only; not metadata reflection or runtime evaluation",
            "methods": sorted(methods, key=lambda x: (x[0], x[1], str(x[3])))}

if __name__ == "__main__":
    result = build(Path(sys.argv[1]).read_bytes())
    dest = Path(__file__).resolve().parents[1] / "graphify/salesforce/apex_platform.json"
    dest.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"methods":len(result["methods"]),"sha256":result["source_sha256"]}))
