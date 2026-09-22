"""Static schema reflection, with identities rather than return-type guesses.

No Apex execution or record queries. Record-type display labels can be translated
and are deliberately NOT treated as developer names. Runtime selectors, missing
declarations and unreviewed reflection calls remain unresolved receiver gaps.
"""
from functools import lru_cache
from importlib.resources import files
import hashlib
import json

from .apex_types import Value, platform_type
from .model import salesforce_id


@lru_cache(maxsize=1)
def signatures():
    data = json.loads(files(__package__).joinpath("apex_schema.json").read_text())
    return {(owner.casefold(), method.casefold(), static, len(args)): returns
            for owner, method, static, args, returns in data["methods"]}


class SchemaBinder:
    def __init__(self, binder):
        self.binder = binder
        self.record_types_by_id = {}
        self.record_types_by_name = {}
        self.objects_by_name = {}
        for node in binder.nodes.values():
            if node["kind"] == "CustomObject":
                self.objects_by_name.setdefault(self.digest(node["name"]), []).append(node)
            if node["kind"] == "RecordType":
                obj, _, name = node["name"].rpartition(".")
                self.record_types_by_name.setdefault((obj.casefold(), self.digest(name)), []).append(node)
            if node["kind"] == "RecordType" and (identity := salesforce_id(node.get("salesforce_id"))):
                self.record_types_by_id.setdefault(self.digest(identity), []).append(node)

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def evidence(self, receiver, node):
        # Catalog-only identities are useful targets, but aren't captured source
        # excerpts. Never manufacture a secondary source proof for a catalog row.
        return receiver.evidence + ((self.binder.proof(node),) if node.get("source_kind", "source") == "source" else ())

    def reference(self, receiver, node, line):
        evidence = self.evidence(receiver, node)
        self.binder.reference(node["kind"], node["name"], "reflects", line,
                              binding_evidence=self.binder.evidence(evidence))
        return evidence

    def object(self, receiver, name, typ, line):
        found = self.binder.lookup("CustomObject", name, self.binder.use.get("namespace", ""))
        if len(found) != 1:
            self.binder.reference("CustomObject", name, "reflects", line)
            return None
        node = found[0]
        return Value(typ, evidence=self.reference(receiver, node, line), schema=("object", node["name"]))

    def field(self, receiver, member, line):
        typ, key = receiver.name.casefold(), member.casefold()
        if typ == "system.schema" and receiver.static is True and key == "sobjecttype":
            return True, Value("schema.sobjecttype", True)
        if typ == "schema.sobjecttype" and receiver.static is True:
            # Schema.SObjectType.Account is a DescribeSObjectResult; the reverse
            # spelling Account.SObjectType is an SObjectType token.
            return True, self.object(receiver, member, "schema.describesobjectresult", line)
        if key == "sobjecttype" and receiver.static is True:
            objects = self.binder.lookup("CustomObject", receiver.name, self.binder.use.get("namespace", ""))
            if objects:
                return True, self.object(receiver, receiver.name, "schema.sobjecttype", line)
        if receiver.schema or typ.startswith("schema."):
            return True, None
        return False, None

    def select(self, receiver, member, arguments, line):
        if member not in {"get", "containskey"} or len(arguments) != 1 or not arguments[0] or arguments[0].selector is None:
            return None
        key = arguments[0].selector
        kind, *context = receiver.schema
        if kind == "global":
            found = self.objects_by_name.get(key, [])
            if len(found) != 1:
                return None
            result = self.object(receiver, found[0]["name"], "schema.sobjecttype", line)
        elif kind == "record_types":
            obj, mode = context
            if mode == "getrecordtypeinfosbydevelopername":
                # String map keys are case-sensitive, unlike Apex identifiers.
                found = self.record_types_by_name.get((obj.casefold(), key), [])
            elif mode == "getrecordtypeinfosbyid":
                found = [n for n in self.record_types_by_id.get(key, [])
                         if n["name"].rpartition(".")[0].casefold() == obj.casefold()]
            else:
                return None  # ByName uses localized labels, never API names.
            if len(found) != 1:
                return None
            node = found[0]
            result = Value("schema.recordtypeinfo", evidence=self.reference(receiver, node, line),
                           schema=("record_type", node["name"]))
        else:
            return None
        return Value("System.Boolean", evidence=result.evidence) if result and member == "containskey" else result

    def call(self, receiver, member, arguments, line):
        typ, key = receiver.name.casefold(), member.casefold()
        if receiver.schema and receiver.schema[0] in {"global", "record_types"}:
            return True, self.select(receiver, key, arguments, line)
        if not (typ == "system.schema" or typ.startswith("schema.")):
            return False, None
        returns = signatures().get((typ, key, receiver.static, len(arguments)))
        if not returns:
            return True, None
        if typ == "system.schema" and key == "getglobaldescribe":
            return True, Value(platform_type(returns), schema=("global",))
        # Knowing a Schema return type is insufficient: Id.getSObjectType() and
        # runtime Schema variables don't prove which declaration was used.
        if not receiver.schema:
            return True, None
        context = receiver.schema
        if typ == "schema.describesobjectresult" and key.startswith("getrecordtypeinfosby"):
            context = ("record_types", receiver.schema[1], key)
        elif not returns.casefold().startswith("schema."):
            context = ()
        return True, Value(platform_type(returns), evidence=receiver.evidence, schema=context)
