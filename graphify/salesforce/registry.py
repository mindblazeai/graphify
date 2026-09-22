from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path, PurePosixPath


@lru_cache(maxsize=1)
def registry() -> dict[str, dict]:
    return json.loads(Path(__file__).with_name("registry.json").read_text())["types"]


def identify(path: str) -> tuple[str, str] | None:
    """Handle arbitrary package roots, MDAPI format, bundles, and DX children."""
    parts = PurePosixPath(path.replace("\\", "/")).parts
    for i, folder in enumerate(parts[:-1]):
        candidates = [(kind, cfg) for kind, cfg in registry().items()
                      if cfg["directory"] == folder]
        if not candidates:
            continue
        rest = parts[i + 1:]
        for kind, cfg in candidates:
            if cfg.get("adapter") in {"bundle", "uiBundles"} and len(rest) > 1:
                return kind, rest[0]
        if folder in {"lwc", "aura", "experiences", "digitalExperiences"} and len(rest) > 1:
            kind = {"lwc": "LightningComponentBundle", "aura": "AuraDefinitionBundle",
                    "experiences": "ExperienceBundle",
                    "digitalExperiences": "DigitalExperienceBundle"}[folder]
            return kind, rest[0]
        # Decomposed children such as objects/Account/fields/Foo.field-meta.xml.
        for kind, cfg in candidates:
            if len(rest) >= 3:
                for child in cfg["children"].values():
                    if child["directory"] == rest[1]:
                        suffix = "." + child["suffix"]
                        name = rest[-1].removesuffix("-meta.xml").removesuffix(suffix)
                        return child["name"], name if child.get("ignore_parent_name") else rest[0] + "." + name
        filename = rest[-1].removesuffix("-meta.xml")
        for kind, cfg in sorted(candidates, key=lambda x: -len(x[1]["suffix"])):
            suffix = cfg["suffix"]
            if suffix and filename.endswith("." + suffix):
                name = filename[:-(len(suffix) + 1)]
                if cfg["in_folder"] and len(rest) > 1:
                    name = "/".join((*rest[:-1], name))
                return kind, name
        # Document bodies use their original filename extension, not a fixed
        # '.document' suffix. Preserve the folder and extension; descriptor/body
        # matching and actual payload validation happen in the parser.
        if folder == "documents" and len(rest) > 1:
            return "Document", "/".join((*rest[:-1], filename))
        if folder == "documents" and len(rest) == 1 and rest[0].endswith("-meta.xml"):
            return "DocumentFolder", filename
    if parts:
        suffix = PurePosixPath(parts[-1]).suffix.lower()
        if suffix in {".cls", ".trigger", ".soql", ".sosl"}:
            return {".cls": "ApexClass", ".trigger": "ApexTrigger",
                    ".soql": "SOQL", ".sosl": "SOSL"}[suffix], PurePosixPath(parts[-1]).stem
    return None
