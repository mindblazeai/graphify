"""Emit an apply_patch patch from version-pinned Salesforce WSDL JSON files.

Usage: python scripts/build_salesforce_settings_literals.py DOCUMENT_DIRECTORY
The directory must contain the selected <MetadataType>.json documentation files
from the revision below, not Salesforce org source. Only boolean, numeric and
the five reviewed closed-enum slots qualify. Strings/complex types are excluded;
they need separate semantic adapters. Generated contracts require no network at
runtime. Review the patch and run the synthetic per-slot regression suite.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import pprint
import sys
import xml.etree.ElementTree as ET

REVISION = "c217b703b3e5a3c279f1a510d8703161b14bd0a5"
BASE = f"https://github.com/forcedotcom/sf-skills/tree/{REVISION}/skills/platform-metadata-api-context-get/assets/metadata_api"
ENUM_SLOTS = {
    ("MyDomainSettings", "domainPartition"),
    ("MyDomainSettings", "edgeRoutingMethod"),
    ("MyDomainSettings", "instancedUrlRedirectHandling"),
    ("MyDomainSettings", "myDomainSuffix"),
    ("SocialCustomerServiceSettings", "caseSubjectOption"),
}
X = "{http://www.w3.org/2001/XMLSchema}"


def contracts(directory):
    result, hashes = {}, {}
    for path in sorted(directory.glob("*Settings.json")):
        raw = path.read_bytes()
        doc = json.loads(raw)
        fragment = doc.get("wsdl_segment", "").removeprefix("```xml").removesuffix("```").strip()
        tree = ET.fromstring('<root xmlns:xsd="http://www.w3.org/2001/XMLSchema">' + fragment + '</root>')
        definitions = {n.attrib.get("name"): n for n in tree}
        root = definitions[path.stem]
        fields = {}
        for field in root.iter(X + "element"):
            name, typ = field.attrib["name"], field.attrib["type"]
            values = ()
            if typ in {"xsd:boolean", "xsd:int", "xsd:double"}:
                typ = typ.removeprefix("xsd:")
            elif (path.stem, name) in ENUM_SLOTS:
                definition = definitions[typ.removeprefix("tns:")]
                values = tuple(n.attrib["value"] for n in definition.iter(X + "enumeration"))
                assert values
                typ = "enum"
            else:
                continue
            # These reviewed Settings slots are all scalar. A future repeated
            # slot must receive an explicit parser/cardinality contract first.
            assert field.attrib.get("maxOccurs", "1") == "1"
            fields[name] = (typ, field.attrib.get("minOccurs", "1") != "0", values)
        if fields:
            result[path.stem] = fields
            hashes[path.stem] = hashlib.sha256(raw).hexdigest()
    assert result and len(result) < 256
    return result, hashes


def main():
    result, hashes = contracts(Path(sys.argv[1]))
    content = '\n'.join([
        '"""Generated, reviewed Settings scalar contracts; do not edit by hand.',
        '',
        'Source: ' + BASE,
        'Only explicit WSDL boolean/numeric fields and reviewed enum slots.',
        'Unknown fields, strings and complex values never become literals by shape.',
        'Regenerate with scripts/build_salesforce_settings_literals.py.',
        '"""',
        '',
        'DOCUMENT_REVISION = ' + repr(REVISION),
        'DOCUMENT_SHA256 = ' + pprint.pformat(hashes, width=110, sort_dicts=True),
        '',
        '# root -> field -> (XML scalar type, required, closed enum values)',
        'SCALAR_CONTRACTS = ' + pprint.pformat(result, width=115, sort_dicts=True),
        '',
    ])
    print('*** Begin Patch\n*** Add File: graphify/salesforce/settings_literals.py')
    print('\n'.join('+' + line for line in content.splitlines()))
    print('*** End Patch')


if __name__ == '__main__':
    main()
