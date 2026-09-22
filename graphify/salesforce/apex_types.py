"""Bounded expression typing against scoped declarations and pinned signatures.

This never executes Apex. Per-file expression facts remain unbound so removing
or changing a declaration re-evaluates callers even when their source is cached.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
import json
import re


def split_type(value):
    value = re.sub(r"\s+", "", value)
    if value.endswith("[]"):
        return "List", [value[:-2]]
    base, sep, tail = value.partition("<")
    if not sep:
        return base, []
    if not tail.endswith(">"):
        return "", []
    args, start, depth = [], 0, 0
    for i, char in enumerate(tail[:-1]):
        depth += (char == "<") - (char == ">")
        if depth < 0:
            return "", []
        if char == "," and not depth:
            args.append(tail[start:i])
            start = i + 1
    return (base, args + [tail[start:-1]]) if depth == 0 else ("", [])


def canonical(value):
    return re.sub(r"(?<![\w.])system\.", "", re.sub(r"\s+", "", value).casefold())


@lru_cache(maxsize=1)
def platform_methods():
    data = json.loads(files(__package__).joinpath("apex_platform.json").read_text())
    result = {}
    for owner, method, static, parameters, returns in data["methods"]:
        result.setdefault((owner.casefold(), method.casefold(), static), []).append((parameters, returns))
    return result


PLATFORM_TYPES = {"void", "object", "string", "blob", "boolean", "integer", "long", "double", "decimal",
                  "date", "datetime", "time", "url", "pagereference", "id", "list", "set", "map",
                  "exception", "apexpages", "encodingutil", "system", "schema"}

SCHEMA_TYPES = {"sobjecttype", "describesobjectresult", "recordtypeinfo"}


def platform_type(raw):
    base, args = split_type(raw)
    name = base.casefold() if base.casefold().startswith("schema.") else "System." + base.casefold().removeprefix("system.")
    return name + ("<" + ",".join(platform_type(a) for a in args) + ">" if args else "")


def compatible(actual, expected):
    # Keep the System qualifier: a customer class called String is NOT the
    # platform String accepted by a native overload.
    a, e = (re.sub(r"\s+", "", value).casefold() for value in (actual, expected))
    return bool(e) and (not a or a == "null" or e in {"system.any", "system.object"}
                        or a == e or {a, e} == {"system.string", "system.id"})


@dataclass(frozen=True)
class Value:
    name: str
    static: bool | None = False
    evidence: tuple = ()
    schema: tuple = ()
    selector: str | None = None


class ReceiverBinder:
    def __init__(self, nodes, lookup, resolve_method, field_path, parents):
        from .apex_schema import SchemaBinder
        self.nodes, self.lookup = nodes, lookup
        self.resolve_method, self.field_path, self.parents = resolve_method, field_path, parents
        self.use = None
        self.references = []
        self.schema = SchemaBinder(self)

    def qualify(self, name, owner, depth=0):
        if not isinstance(name, str) or not name or len(name) > 1024 or depth > 20:
            return ""
        base, args = split_type(name)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", base):
            return ""
        # Explicit System qualification cannot bind a similarly named customer
        # class; unqualified identifiers must first consider scoped declarations.
        if not base.casefold().startswith("system."):
            scopes = [owner]
            while "." in scopes[-1]:
                scopes.append(scopes[-1].rpartition(".")[0])
            for candidate in [*(scope + "." + base for scope in scopes if scope), base]:
                found = self.lookup("Type", candidate, self.use.get("namespace", ""))
                if found:
                    return found[0]["name"] if len(found) == 1 and not args else ""
        short = base.casefold().removeprefix("system.")
        if short.startswith("schema.") and short.removeprefix("schema.") in SCHEMA_TYPES and not args:
            return short if self.qualify("Schema", owner, depth + 1) == "System.schema" else ""
        if short not in PLATFORM_TYPES:
            # An independently declared schema/class identity is required. An
            # absent class name cannot masquerade as a platform type.
            return ""
        if args:
            if short not in {"list", "set", "map"} or len(args) != (2 if short == "map" else 1):
                return ""
            qualified = [self.qualify(arg, owner, depth + 1) for arg in args]
            if not all(qualified):
                return ""
            return "System." + short + "<" + ",".join(qualified) + ">"
        return "System." + short

    @staticmethod
    def proof(node, line=None):
        return {"source_file":node["source_file"], "source_sha":node["source_sha"],
                "line":line or node["line"], "status":"captured"}

    def reference(self, kind, name, relation, line=None, **extra):
        use = self.use
        ref = {key: use[key] for key in ("source", "source_file", "source_sha", "namespace")}
        ref.update(target_kind=kind, target_name=name, relation=relation,
                   line=line or use["line"], source_location=f"L{line or use['line']}", **extra)
        self.references.append(ref)

    @staticmethod
    def evidence(proofs):
        return sorted({(p["source_file"],p["line"]):p for p in proofs}.values(),
                      key=lambda p: (p["source_file"],p["line"]))

    def expression(self, expr, owner, depth=0):
        if depth > 20 or not isinstance(expr, list) or not expr:
            return None
        kind = expr[0]
        if kind == "null":
            return Value("null")
        if kind == "metadata_key":
            return Value("System.String", selector=expr[1])
        if kind == "type":
            name = self.qualify(expr[1], owner)
            return Value(name, expr[2]) if name else None
        if kind == "index":
            value = self.expression(expr[1], owner, depth + 1)
            base, args = split_type(value.name if value else "")
            return Value(args[0], evidence=value.evidence) if canonical(base) == "list" and len(args) == 1 else None
        if kind in {"field", "call"}:
            if kind == "field":
                def static_path(value):
                    if value[0] == "type" and value[2] is True:
                        return value[1]
                    if value[0] == "field" and (prefix := static_path(value[1])):
                        return prefix + "." + value[2]
                    return ""
                # Package/nested type tokens are not instance properties.
                # Only a real scoped declaration (or explicit System type)
                # can validate the full dotted spelling.
                if path := static_path(expr):
                    qualified = self.qualify(path, owner)
                    if qualified:
                        return Value(qualified, True)
            receiver = self.expression(expr[1], owner, depth + 1)
            if not receiver:
                return None
            if kind == "field":
                return self.field(receiver, expr[2], "reads_member", expr[3])
            arguments = [self.expression(arg, owner, depth + 1) for arg in expr[3]]
            return self.call(receiver, expr[2], arguments, expr[4])
        return None

    def field(self, receiver, name, relation, line):
        handled, result = self.schema.field(receiver, name, line)
        if handled:
            return result
        owners = self.lookup("Type", receiver.name, self.use.get("namespace", ""))
        seen = set()
        while owners:
            declarations, inherited = [], []
            for owner in owners:
                if owner["id"] in seen:
                    continue
                seen.add(owner["id"])
                for member in owner.get("apex_fields", {}).get(name.casefold(), []):
                    if receiver.static is True and not member["static"]:
                        continue
                    declarations.append((owner, member))
                for parent in self.parents.get(owner["name"].casefold(), []):
                    inherited.extend(self.lookup("Type", parent, owner.get("namespace", "")))
            if declarations:
                if len(declarations) != 1:
                    return None
                owner, member = declarations[0]
                typ = self.qualify(member["type"], owner["name"])
                if not typ:
                    return None
                proof = self.proof(owner, member["line"])
                if owner["component_id"] != self.nodes[self.use["source"]]["component_id"]:
                    self.reference(owner["kind"], owner["name"], relation, line,
                                   apex_member=member["name"],
                                   binding_evidence=self.evidence((*receiver.evidence,proof)))
                return Value(typ, evidence=receiver.evidence + (proof,))
            owners = inherited
        return None

    def call(self, receiver, member, arguments, line):
        handled, result = self.schema.call(receiver, member, arguments, line)
        if handled:
            return result
        raw_types = [arg.name if arg else "" for arg in arguments]
        base, generic = split_type(receiver.name)
        collection = canonical(base)
        key = member.casefold()
        result = ""
        if collection == "list" and len(generic) == 1:
            if key == "get" and len(arguments) == 1 and compatible(raw_types[0], "System.Integer"):
                result = generic[0]
        if collection == "map" and len(generic) == 2:
            if key == "get" and len(arguments) == 1 and compatible(raw_types[0], generic[0]):
                result = generic[1]
            elif key == "values" and not arguments:
                result = "System.List<" + generic[1] + ">"
            elif key == "keyset" and not arguments:
                result = "System.Set<" + generic[0] + ">"
            elif key == "put" and len(arguments) == 2 and all(compatible(a,e) for a,e in zip(raw_types,generic)):
                result = generic[1]
            elif key == "containskey" and len(arguments) == 1 and compatible(raw_types[0], generic[0]):
                result = "System.Boolean"
        if collection in {"list", "map", "set"} and generic:
            if key in {"clone", "deepclone"} and not arguments:
                result = receiver.name
            elif key == "size" and not arguments:
                result = "System.Integer"
            elif key == "isempty" and not arguments:
                result = "System.Boolean"
        if result and receiver.static is not True:
            return Value(result, evidence=receiver.evidence)
        if receiver.name.casefold().startswith("system."):
            matches = []
            for static in ([True,False] if receiver.static is None else [receiver.static]):
                matches.extend(ret for params, ret in platform_methods().get((receiver.name.casefold(),key,static), [])
                               if len(params)==len(raw_types) and all(compatible(a,platform_type(e)) for a,e in zip(raw_types,params)))
            returns = {platform_type(ret) for ret in matches}
            # Generic platform return arguments belong to System, not a caller
            # namespace. Qualification is applied with explicit System leaves.
            if len(returns) == 1 and next(iter(returns)):
                return Value(next(iter(returns)), evidence=receiver.evidence)
            return None
        candidates = self.resolve_method({"target_name":receiver.name + "." + member,
            "namespace":self.use.get("namespace", ""), "arity":len(raw_types),
            "argument_types":[]})
        candidates = [n for n in candidates if receiver.static is None or n.get("is_static",False)==receiver.static]
        candidates = [n for n in candidates if n.get("apex_signature_verified")]
        candidates = [n for n in candidates if all(compatible(a, self.qualify(p,n["owner_type"]))
                                                   for a,p in zip(raw_types,n["parameters"]))]
        if len(candidates) != 1:
            return None
        method = candidates[0]
        typ = self.qualify(method.get("return_type", ""), method["owner_type"])
        if not typ:
            return None
        proof = self.proof(method)
        self.reference("ApexMethod", method["name"], "calls", line,
                       arity=len(raw_types), argument_types=raw_types,
                       binding_evidence=self.evidence((*receiver.evidence,proof)))
        return Value(typ, evidence=receiver.evidence + (proof,))

    def bind(self, use):
        self.use, self.references = use, []
        if use.get("bounded_out"):
            return False, []
        owner = use["lexical_owner"]
        receiver = self.expression(use["receiver"], owner)
        if not receiver:
            return False, self.references
        if use["operation"] == "call":
            args = [self.expression(arg, owner) for arg in use["arguments"]]
            return self.call(receiver, use["member"], args, use["line"]) is not None, self.references
        if use["operation"] == "field":
            handled, result = self.schema.field(receiver, use["member"], use["line"])
            if handled:
                return result is not None, self.references
            fields = self.field_path(receiver.name + "." + use["member"], use.get("namespace", ""))
            if len(fields) == 1:
                evidence = self.evidence(receiver.evidence)
                self.reference("FieldPath", receiver.name + "." + use["member"], use["relation"],
                               **({"binding_evidence":evidence} if evidence else {}))
                return True, self.references
            relation = "writes_member" if use["relation"] == "writes" else "reads_member"
            return self.field(receiver, use["member"], relation, use["line"]) is not None, self.references
        return False, self.references
