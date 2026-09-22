"""Reviewed PromptVersion value and application-identity contracts.

Metadata API 62.0 describeValueType(PromptVersion), verified 2026-09-22:
customApplication is a CustomApplication foreign key; experience is the
Lightning/Site enum. Provider schema XML SHA-256:
6183df718d3e54da9a8e680d89e4ad105358fa405826b9f7a8966fce5986a956.
This is a static, reviewed adapter, not a claim that arbitrary string slots
without isForeignKey are free of dependencies. No record lookup is performed.
"""
from __future__ import annotations

from datetime import date
import re

from .model import salesforce_id

MAX_VERSIONS = 256
REQUIRED = frozenset({"body", "displayType", "masterLabel", "title", "versionNumber"})
INTEGERS = frozenset({"delayDays", "stepNumber", "timesToDisplay", "versionNumber"})
BOOLEANS = frozenset({"isPublished", "shouldDisplayActionButton", "shouldIgnoreGlobalDelay"})
DATES = frozenset({"startDate", "endDate", "publishedDate"})
ENUMS = {
    "experience": frozenset({"Lightning", "Site"}),
    "displayType": frozenset({"DockedComposer", "FloatingPanel", "Walkthrough", "Targeted"}),
    "displayPosition": frozenset({"TopLeft", "TopCenter", "TopRight", "BottomLeft", "BottomCenter", "BottomRight", "MiddleLeft", "MiddleCenter", "MiddleRight"}),
    "elementRelativePosition": frozenset({"TopLeft", "TopCenter", "TopRight", "LeftTop", "LeftCenter", "LeftBottom", "RightTop", "RightCenter", "RightBottom", "BottomLeft", "BottomCenter", "BottomRight"}),
    "imageLocation": frozenset({"Top", "Bottom", "Left", "Right"}),
    "themeColor": frozenset({"Theme1", "Theme2", "Theme3", "Theme4"}),
    "themeSaturation": frozenset({"Dark", "Light"}),
    "userAccess": frozenset({"Everyone", "SpecificPermissions"}),
    "userProfileAccess": frozenset({"Everyone", "SpecificProfiles"}),
}


def validate_prompt_version(version, *, issue, scalar, children):
    # All version properties are singleton in the provider schema. Strings
    # may be empty, but required values, numbers, enums and IDs may not be.
    names = {n.tag for n in version.children} | REQUIRED
    for tag in sorted(names):
        if tag == "uiFormulaRule":
            if len(children(version, tag)) > 1:
                issue("metadata_reference_ambiguous_scalar", version, property=tag)
            continue
        required = tag in REQUIRED or tag in INTEGERS or tag in BOOLEANS or tag in DATES or tag in ENUMS
        n = scalar(version, tag, required=tag in REQUIRED or (required and bool(children(version, tag))))
        if not n:
            continue
        value = n.text.strip()
        valid = True
        if tag in ENUMS:
            valid = value in ENUMS[tag]
        elif tag in BOOLEANS:
            valid = value in {"true", "false", "1", "0"}
        elif tag in INTEGERS:
            valid = bool(re.fullmatch(r"[+-]?[0-9]{1,10}", value)) and -2147483648 <= int(value) <= 2147483647
        elif tag in DATES:
            # Canonical metadata date, optionally carrying an XSD timezone.
            match = re.fullmatch(r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:Z|([+-])([0-9]{2}):([0-9]{2}))?", value)
            valid = bool(match)
            if match:
                try:
                    date.fromisoformat(match[1])
                except ValueError:
                    valid = False
                if match[2]:
                    hour, minute = int(match[3]), int(match[4])
                    valid = valid and hour <= 14 and minute <= 59 and (hour < 14 or minute == 0)
        if not valid:
            issue("prompt_value_unsupported", n, property=tag)


def application_references(version, *, issue, scalar, ref, children):
    def item(tag):
        return scalar(version, tag, required=bool(children(version, tag)))

    modern = item("customApplication")
    legacy = item("targetAppDeveloperName")
    namespace = item("targetAppNamespacePrefix")
    legacy_name = legacy.text.strip() if legacy else ""
    if namespace:
        if not legacy:
            issue("prompt_application_context_missing", namespace)
        else:
            legacy_name = namespace.text.strip() + "__" + legacy_name
    if modern and legacy and modern.text.strip().casefold() != legacy_name.casefold():
        # An ID and name might identify the same app, but that isn't established
        # at parse time. Do not silently choose a field or invent equivalence.
        issue("prompt_application_context_conflict", modern)
        return
    for n, name, contract in (
        (modern, modern.text.strip() if modern else "", "MetadataAPI.PromptVersion.customApplication"),
        (legacy, legacy_name, "MetadataAPI.PromptVersion.targetAppDeveloperName"),
    ):
        if not n:
            continue
        attrs = {"identity_contract": "prompt_app", "schema_contract": contract}
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            if n is modern and salesforce_id(name):
                attrs["target_salesforce_id"] = name
            else:
                issue("prompt_application_identity_invalid", n)
                continue
        ref(n, "CustomApplication", name=name, **attrs)
