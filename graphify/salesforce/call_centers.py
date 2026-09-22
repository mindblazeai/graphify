"""Open CTI configuration references, never runtime or User-record discovery.

Contracts: Salesforce Open CTI guide, call-def-file-{required,optional,sample}.
Only documented settings are interpreted in their documented section or the
flat customSettings JSON representation. Custom adapter semantics remain gaps.
Configured URLs do not establish which adapter actually ran (Canvas/failover).
"""
from __future__ import annotations

from collections import Counter, defaultdict
import json
import re
from urllib.parse import urlsplit


GENERAL = frozenset({
    "reqAdapterUrl", "reqStandbyUrl", "reqTimeout", "reqUseApi",
    "reqSalesforceCompatibilityMode", "reqSoftphoneHeight", "reqSoftphoneWidth",
    "reqCanvasApiName", "reqCanvasNamespace", "reqInternalName", "reqDisplayName",
})
DIALING = frozenset({"reqOutsidePrefix", "reqLongDistPrefix", "reqInternationalPrefix"})
MAX_JSON_BYTES = 65536
MAX_SETTINGS = 128
MAX_VALUE = 4096


def _flat_settings(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result or not isinstance(value, str):
                raise ValueError("non-flat or duplicate settings")
            result[key] = value
        return result

    if len(text) > MAX_JSON_BYTES or len(text.encode()) > MAX_JSON_BYTES:
        raise ValueError("settings size limit")
    result = json.loads(text, object_pairs_hook=unique)
    if not isinstance(result, dict) or len(result) > MAX_SETTINGS:
        raise ValueError("settings shape limit")
    if any(len(key) > 128 or len(value) > MAX_VALUE for key, value in result.items()):
        raise ValueError("settings value limit")
    return result


def parse_open_cti(facts, root, *, issue, scalar, ref, children):
    # A tuple holds only the in-memory value and original XML evidence. Settings
    # (which may contain vendor credentials) never enter serialized facts.
    entries = defaultdict(list)
    invalid = set()

    def add(key, value, node):
        if len(value) > MAX_VALUE:
            issue("call_center_setting_value_unsupported", node, property=key)
            invalid.add(key)
        else:
            entries[key].append((value.strip(), node))

    for tag, key in (("adapterUrl", "reqAdapterUrl"), ("displayName", "reqDisplayName")):
        node = scalar(root, tag, required=bool(children(root, tag)))
        if node:
            add(key, node.text, node)
        elif children(root, tag):
            invalid.add(key)

    groups = children(root, "sections")
    if len(groups) > MAX_SETTINGS:
        issue("call_center_settings_limit", root)
        groups = []
    names = [scalar(group, "name", required=True) for group in groups]
    counts = Counter(n.text.strip() for n in names if n)
    for group, name in zip(groups, names):
        scalar(group, "label", required=True)
        section = name.text.strip() if name else ""
        allowed = GENERAL if section == "reqGeneralInfo" else DIALING if section == "reqDialingOptions" else frozenset()
        duplicate_section = counts[section] > 1
        if duplicate_section or not allowed:
            issue("call_center_section_unverified", group)
        items = children(group, "items")
        if len(items) > MAX_SETTINGS:
            issue("call_center_settings_limit", group)
            continue
        seen = set()
        for item in items:
            key_node = scalar(item, "name", required=True)
            scalar(item, "label", required=True)
            key = key_node.text.strip() if key_node else ""
            values = children(item, "value")
            # An empty dialing prefix means no prefix, not missing metadata.
            value = values[0] if len(values) == 1 and not values[0].children else None
            if value is None:
                scalar(item, "value", required=True)
            if key not in allowed:
                issue("call_center_custom_setting_unverified", item)
                continue
            if key in seen or duplicate_section or value is None:
                invalid.add(key)
                issue("call_center_setting_ambiguous", item, property=key)
            else:
                add(key, value.text, value)
            seen.add(key)

    custom = scalar(root, "customSettings", required=bool(children(root, "customSettings")))
    if custom:
        try:
            settings = _flat_settings(custom.text)
        except (ValueError, RecursionError, UnicodeError):
            issue("call_center_custom_settings_unverified", custom)
        else:
            for key, value in settings.items():
                if key not in GENERAL | DIALING:
                    issue("call_center_custom_setting_unverified", custom)
                else:
                    add(key, value, custom)

    for key, values in entries.items():
        if len({v for v, _ in values}) != 1:
            invalid.add(key)
            issue("call_center_setting_conflict", values[0][1], property=key)

    def get(key):
        return entries[key][0][0] if key not in invalid and entries.get(key) else None

    def bad(key, code="call_center_setting_value_unsupported"):
        issue(code, entries[key][0][1] if entries.get(key) else root, property=key)

    # Voice-only contactCenterChannels need no Open CTI settings.
    if not any(key in entries or key in invalid for key in GENERAL - {"reqDisplayName"}):
        return
    mode = get("reqSalesforceCompatibilityMode") or "Classic"
    if mode not in {"Classic", "Lightning", "Classic_and_Lightning"}:
        bad("reqSalesforceCompatibilityMode")
    api = get("reqUseApi")
    if api not in {"true", "false"}:
        bad("reqUseApi")
    elif api == "false":
        bad("reqUseApi", "call_center_legacy_adapter_unverified")
    canvas = any(get(key) is not None for key in ("reqCanvasApiName", "reqCanvasNamespace"))
    if canvas:
        issue("call_center_canvas_adapter_unverified", root)
    for key in ("reqSoftphoneHeight", "reqSoftphoneWidth"):
        value = get(key)
        bounds = (240, 2560) if key == "reqSoftphoneHeight" else (200, 1920)
        minimum, maximum = bounds if mode in {"Lightning", "Classic_and_Lightning"} else (1, 2147483647)
        if value is None or not re.fullmatch(r"[0-9]{1,10}", value) or not minimum <= int(value) <= maximum:
            bad(key)
    if (name := get("reqInternalName")) is not None and name != facts.source.full_name:
        bad("reqInternalName", "call_center_identity_mismatch")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,39}", facts.source.full_name):
        issue("call_center_identity_unverified", root)
    if (display := get("reqDisplayName")) is not None and (not display or len(display) > 1000):
        bad("reqDisplayName")
    standby, timeout = get("reqStandbyUrl"), get("reqTimeout")
    if (standby is not None) != (timeout is not None):
        issue("call_center_standby_timeout_incomplete", root)
    if timeout is not None and (not re.fullmatch(r"[0-9]{1,10}", timeout) or int(timeout) > 2147483647):
        bad("reqTimeout")

    for key, role in (("reqAdapterUrl", "primary"), ("reqStandbyUrl", "standby")):
        value = get(key)
        if value is None:
            if key == "reqAdapterUrl" and not canvas:
                bad(key, "call_center_adapter_missing")
            continue
        page = re.fullmatch(r"/apex/([A-Za-z_][A-Za-z0-9_]{0,254})", value)
        if page:
            proof = [{"source_file": facts.source.path, "source_sha": facts.source_sha, "line": n.line}
                     for _, n in entries[key]]
            # Keep every mirror's original source line without duplicate edges.
            proof = list({p["line"]: p for p in proof}.values())
            ref(entries[key][0][1], "ApexPage", "references", page[1],
                identity_contract="call_center_open_cti_adapter", cti_role=role,
                cti_setting=key, binding_evidence=proof[1:])
            continue
        # Absolute HTTP(S) adapters are external locations, never guessed local
        # pages. No URL is fetched or serialized, including query credentials.
        try:
            url = urlsplit(value)
            external = (url.scheme in {"http", "https"} and bool(url.hostname)
                        and url.username is None and url.password is None
                        and (url.port is None or 0 < url.port <= 65535)
                        and not re.search(r"[\s\\{}]|%7[bBdD]|\$", value))
        except ValueError:
            external = False
        if not external:
            bad(key, "call_center_adapter_url_unverified")
        elif mode in {"Lightning", "Classic_and_Lightning"} and url.scheme != "https":
            bad(key, "call_center_adapter_https_required")
