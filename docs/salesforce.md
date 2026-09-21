# Salesforce dependency graph (LivingContext fork)

This fork adds `graphify.salesforce`, a deterministic, model-free graph engine. It does not contact Salesforce or upload source to Graphify. Install the optional Salesforce grammar pack:

```sh
uv sync --extra salesforce
uv run graphify-salesforce /path/to/salesforce-project --output graph.json
# Equivalent: python -m graphify.salesforce /path/to/salesforce-project
```

Use the Salesforce command for cross-file metadata binding; the normal Graphify Apex extractor also uses the AST adapter when the extra is installed. Without the extra, the legacy extractor retains its regex fallback and reports that limitation. The general Graphify command does not automatically run this whole-org metadata pipeline.

## Coverage is explicit, not a blanket completeness claim

| Surface | Adapter |
| --- | --- |
| Apex classes/interfaces/enums/triggers | sfapex Tree-sitter AST; scopes, type references, inheritance, signatures/overloads, calls, DML, annotations |
| SOQL/SOSL | Embedded Apex grammar, object/field reads, nested relationships; literals and bounded constant-string composition; runtime-dependent queries remain unresolved |
| LWC | JS/TS AST imports, declared methods, imported Apex calls, component rendering |
| Aura | Markup controllers/events, client methods, helper and server actions |
| Visualforce | Controllers, extensions, object/field expressions, referenced components/resources |
| Email templates | Classic `{!Object.Field}`, Lightning `{{{Object.Field}}}` / `{{Object.Field}}`, Visualforce recipient/related-record context, relationship traversal and sidecar-declared related entity; untyped recipient aliases stay unresolved |
| Metadata XML | Identity and validated structure for all registry types, plus known object/field/formula/Flow/workflow/permission/report/layout and other reference tags |
| JSON bundles | Parsed JSON and explicitly typed component-reference properties, with JSON-path provenance |
| Captured REST schema | Real describe fields, parent lookup names and child relationship names, including standard/compound fields absent from retrieved object XML |
| Captured Analytics report types | Type-scoped column aliases bind actual report XML references to independently declared fields; API column availability alone does not create a usage |
| Captured profile-backed permission sets | REST direct field/object/setup grants; verified owner/profile and setup metadata IDs; disabled permissions remain configuration references |
| Future or binary formats | Identity and structural/catalog coverage; no invented semantics |

The checked-in type registry is generated from Salesforce's official source-deploy-retrieve registry (533 top-level types at generation, plus child types). It recognizes MDAPI and decomposed DX files under arbitrary package roots. Explicit `Source` inputs can name types newer than that registry. Updating the registry does **not** imply complete new-type semantics.

Coverage levels are `semantic` (specialized static rules ran), `structural` (generic format/identity adapter), `catalog` (identity without source), `partial` (syntax errors, dynamic references, or known retrieval limitations), and `unparsed` (size limit). "Semantic" means supported static rules, not a full Salesforce compiler or complete runtime behavior. Profiles are always marked partial because retrieve manifests constrain the permissions Salesforce returns.

Dynamic Apex, reflection, polymorphic/indirect dispatch, runtime-generated queries, arbitrary formula evaluation, effective permission aggregation, installed-package source restrictions, and arbitrary JS dataflow are not fully resolved. Unknown and ambiguous bindings remain first-class external nodes, with candidate IDs when available. No-edge results are never safe-delete authorization.

## Python integration and stable graph contract

```python
from graphify.salesforce import Source, build_graph, scan_project

sources = scan_project(project_path)  # pathlib.Path; skips dependencies and symlinks
graph = build_graph(sources, include_facts=True)
cache = graph.pop("facts")
# Reuse unchanged syntax; rebind every reference against the current corpus.
next_graph = build_graph(sources, previous_facts=cache, include_facts=True)
```

Database integrations can construct `Source(path, content, metadata_type, full_name, namespace="", source_kind="source", salesforce_id=None)` directly. Use `source_kind="catalog"` for missing bodies, never synthetic empty code as if it were real source. An optional catalog Salesforce ID belongs only to the root component and participates in its source fingerprint. Its first 15 characters are case-sensitive; unlike metadata names, record IDs are never case-folded or guessed from a prefix.

Graph schema version 1 has `nodes`, `edges`, `coverage`, `diagnostics`, `stats`, and optional `facts`. IDs use case-insensitive Salesforce identities, not checkout paths. Nodes include ownership and file/line evidence; edges include relationship, `resolved`/`unresolved`/`ambiguous` binding, and confidence. Lookup traversals retain both the intermediate lookup-field dependency and the final field. Function overloads have signature-specific IDs. Graphs are per org; the caller must enforce tenant/connection boundaries.

Engine `salesforce-7` includes source SHA-256 on nodes and references for hash-verified evidence previews, per-node coverage, and `is_test` on Apex test declarations/methods. Test-class calls and field access remain ordinary static evidence, not proof that tests ran or covered a line. Flow assignments distinguish writes from reads; merge expressions inside declarative text, FlexiPage record context, ReportType table/relationship scopes, report dollar-delimited field paths, CustomMetadata value fields and global-value-set references are extracted explicitly. An identity-only component can have proven incoming usages even when it has no standalone source file.

Profile-backed permission captures use `PermissionSet` API sources at `salesforce-api/permissions/<catalog Salesforce ID>/{PermissionSet,FieldPermissions,ObjectPermissions,SetupEntityAccess,status}.json`. API version `v63.0` includes `PermissionsViewAllFields`; this broad object permission does not invent an individual grant for every field. Owner identity, `IsOwnedByProfile`, ProfileId, record IDs, ParentId and boolean permission flags are validated. Object/field grants require independently declared targets. Setup access resolves only against exact IDs supplied by the scoped catalog; missing IDs stay unresolved and collisions ambiguous. A verified profile owner adds a structural Profile → backing PermissionSet member link, allowing profile exploration to reach its grants. Explicitly disabled permissions in REST and XML use `configures_access`, not `grants_access`.

Permission edges retain both the capture hash/line and the latest per-section status hash/line. A failed refresh preserves old evidence but marks affected links with its capture status; consumers must not show it as current. A complete API capture means only that all requested direct permission records were returned. It is not a full Profile retrieve or effective-user-access calculation: assignments, groups, muting, license constraints, implicit access and runtime context are not inferred. Profile-backed permission components remain partial.

Report-type Analytics responses use `ReportType` sources at `salesforce-api/reportTypes/<URL-encoded API type>/describe.json`. The requested identity must match the response and the catalog identity (custom report types may omit the API-only `__c` suffix). Columns are scoped to that report type; exact aliases win over dollar-delimiter normalization, and conflicting mappings remain ambiguous. Binding tries verified relationship paths and actual declared sObject fields, never display labels. Edges retain both report XML provenance and `binding_evidence` for the captured mapping. Computed aliases are not invented fields. A sibling `status.json` marks failed/inaccessible checks partial while preserving prior evidence, including its status on affected bindings. Storage adapters should omit column dictionaries from interactive node payloads.

Foldered metadata retains its catalog identity when a verified source path contains a fuller folder hierarchy. The exact path-derived name is indexed as an alias for cross-file references. Basenames are never guessed; colliding aliases remain ambiguous. Storage integrations must verify returned file ownership before constructing a `Source`.

Apex collection receivers retain their complete generic type. Platform list/map/set calls do not become fabricated methods on the element's object. Indexed elements, typed `get()` results, map `values()`, nested collections and collection DML retain their actual field/object dependencies. Unknown custom-method return receivers produce `apex_receiver_type_unresolved` and partial coverage rather than invented field paths. Collection overloads remain distinct from element overloads.

Nodes and edges have deterministic source-local ordering, so repeated provenance stays close enough for ordinary gzip to compress effectively. This changes ordering, not identities or relationships; consumers must use IDs, not array positions.

Captured describe JSON is supplied explicitly as a `CustomObject` source at `salesforce-api/sobjects/<Object>/describe.json`, with `source_kind="api"`. Only fields and relationships actually present in that response are declared. API response evidence is distinct from retrieved XML. The Tooling `(hidden)` sentinel produces a `source_hidden_by_salesforce` availability diagnostic, not a syntax error. Bundle coverage retains partial warnings while allowing a semantic JS adapter to supersede a structural CSS sidecar.

Constant-string analysis never executes Apex. Literal concatenation, local assignments and final string constants can resolve dynamic SOQL and `Type.forName`; branches, loops, unknown assignments and mutable class fields invalidate the constant. Derived query references retain the original Apex source hash and call-site line.

Trusted storage integrations can supply `Source.content_sha` to compute fingerprints without transferring unchanged bodies. They must supply matching previous facts for omitted bodies and verify the SHA when loading changed source; a hash is not a substitute for source on a cache miss.

The caller supplies the complete current source set. Files omitted from the next build are pruned, and unchanged caller files are rebound so deleted targets become unresolved. A source-cache fingerprint includes engine version; bump `ENGINE_VERSION` whenever extraction semantics change. This is not a Salesforce deletion-detection API.

## Maintenance and verification

```sh
python scripts/build_salesforce_registry.py /path/to/metadataRegistry.json 12.37.1
uv run --extra salesforce pytest tests/test_salesforce_graph.py tests/test_salesforce_permissions.py tests/test_languages.py
```

The registry records its source hash/version and Salesforce's Apache-2.0 attribution. Graphify's upstream Apache-2.0 license and NOTICE remain in force; the grammar-pack distribution retains its upstream grammar licenses. No Salesforce customer source is included in the fixtures.

The Salesforce and language suites currently pass 1,017 tests (20 optional-language skips), including a parametrized identity/coverage contract for every registered type. This is a coverage contract, not a promise of complete semantics for all 533 types. Cross-object relationship binding uses precomputed parent/child schema indexes rather than scanning every field per reference. Permission record line indexing is linear in source size rather than repeatedly rescanning large captures.
