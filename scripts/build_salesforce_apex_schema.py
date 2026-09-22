"""Pin only reviewed reflection signatures from the metadata-only v67 capture."""
import hashlib
import json
from pathlib import Path
import sys

EXPECTED_SHA = "3a8c609cccb49b2383c13a6157bdc8b802a981ff4473cfc7d95c7b5fef6a9cda"
REVIEWED = {
    "System.Schema": {"getGlobalDescribe"},
    "Schema.SObjectType": {"getDescribe"},
    "Schema.DescribeSObjectResult": set("""getName getLocalName getLabel getLabelPlural getKeyPrefix
        getSObjectType getRecordTypeInfosByDeveloperName getRecordTypeInfosById getRecordTypeInfosByName
        isAccessible isCreateable isCustom isCustomSetting isDeletable isDeprecatedAndHidden
        isFeedEnabled isMergeable isMruEnabled isQueryable isSearchable isUndeletable isUpdateable
        hashCode toString""".split()),
    "Schema.RecordTypeInfo": set("""getDeveloperName getName getRecordTypeId isActive isAvailable
        isDefaultRecordTypeMapping isMaster hashCode toString""".split()),
}


def build(raw):
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_SHA:
        raise ValueError("Provider schema changed; review it before changing the pin")
    declarations = json.loads(raw)["publicDeclarations"]
    methods = []
    for owner, names in REVIEWED.items():
        namespace, name = owner.split(".")
        found = set()
        for entry in declarations[namespace][name]["methods"]:
            parameters = entry["argTypes"]
            assert parameters == [p["type"] for p in entry["parameters"]]
            assert isinstance(entry["isStatic"], bool)
            if entry["name"] in names and not parameters:
                found.add(entry["name"])
                methods.append([owner, entry["name"], entry["isStatic"], parameters, entry["returnType"]])
        assert found == names, (owner, names - found)
    return {"source": "Salesforce Tooling API v67.0 /completions?type=apex", "source_sha256": digest,
            "scope": "Reviewed reflection signatures; binding additionally requires scoped metadata identities",
            "semantics": "https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/salesforce_apex_reference_guide.pdf",
            "methods": sorted(methods, key=lambda x: (x[0], x[1]))}


if __name__ == "__main__":
    result = build(Path(sys.argv[1]).read_bytes())
    dest = Path(__file__).resolve().parents[1] / "graphify/salesforce/apex_schema.json"
    dest.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"methods": len(result["methods"]), "sha256": result["source_sha256"]}))
