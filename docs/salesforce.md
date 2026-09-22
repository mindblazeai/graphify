# Salesforce dependency graph (LivingContext fork)

This fork adds `graphify.salesforce`, a deterministic, model-free graph engine. It does not contact Salesforce or upload source to Graphify. Install the optional Salesforce grammar pack:

```sh
uv sync --inexact --extra salesforce
uv run graphify-salesforce /path/to/salesforce-project --output graph.json
# Equivalent: python -m graphify.salesforce /path/to/salesforce-project
```

The `[salesforce]` and `[all]` extras include the pinned Salesforce grammar pack
and timezone definitions used to validate Business Hours settings independently
of the host operating system, plus the pinned Pillow raster validator. On an
existing development environment, `--inexact` preserves other installed extras.

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
| Typed declarative metadata | Paths/animations, role hierarchy and access configuration, queue targets/membership/routing, service-channel fields, profile policies, topics, lead-conversion field pairs and moderation criteria |
| Experience Cloud and presence configuration | Network/site/template/profile links, typed navigation containers and list-view IDs, Visualforce/static site assets, moderation/keyword/user-criteria site ownership, presence profiles and statuses |
| Translations and branding | Exact object/member/value-set translation targets; theme → branding → declared asset links; documented local file-asset routes |
| Asset envelopes and notification actions | Explicit asset-origin network and document-folder links; notification API actions → Apex; payload analysis limits remain partial |
| CSV static resources | Exact adjacent descriptor/body pairing for `text/csv` and `application/csv`; bounded literal tables, both source hashes retained; cell values are not metadata references |
| Document and StaticResource images | Bounded PNG/JPEG validation from original bytes; exact descriptor pairing, both source hashes, independently declared document folders; pixels and image metadata are not code |
| Document folders | Required labels and typed access settings; independently declared group/role share targets; User/manager and unverified legacy share identities remain partial |
| Restriction, prompt and notification policies | Bounded field-restriction expressions/field sets; prompt images and documented visibility filters; isolated notification delivery settings |
| Data-cleaning mappings | Context-verified input reads/output writes; virtual data-service objects remain distinct from Salesforce objects |
| External client apps and menus | Explicit settings-to-app, Apex handler, custom OAuth scope, ID-bound permission/certificate and qualified attribute-field links; typed app-menu entries remain partial |
| Setup and selected Settings roots | Home-page widgets; forecast models/configuration; legacy community references; site-scoped topic hierarchy; CMS field definitions; active theme, identity certificate, search objects and territory Apex handlers |
| Experience audiences | Container-scoped audiences, profile/field/custom-permission criteria, record-type criteria and typed report/dashboard/navigation targets; criteria text remains literal |
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

Engine `salesforce-14` accepts verified `FieldDefinition.DurableId` values for custom fields: `Object.00N…` or `01I….00N…`. The object name or custom-object ID must match an independent parent declaration. A catalog ID is retained when richer parent XML owns the actual field declaration. Conflicting component IDs never bind either candidate; duplicate IDs across components remain ambiguous. The current source inventory, not an unresolved reference, supplies identity, and rebinding does not write normalized IDs into cached syntax facts.

Integrations can pass `node_filter(node) -> bool` to `build_graph` to scope declarations embedded in parent files before cross-file binding. Filter top-level `Source` inputs as well. References from excluded declarations are dropped; incoming references from included components remain unresolved without exposing the excluded target's source. The callback runs on every build, including cached-fact reuse, so a later scope change cannot reuse stale bindings. This is a caller-supplied scope policy, not authentication.

The Audience adapter follows the Metadata API v68 criterion-type contracts. Profile, object/field, custom-permission, nested-audience and RecordTypeId criteria use explicit identities; names of standard permissions, location/domain values and arbitrary field values are literals. Numbered AND/OR/NOT filter logic is bounded and must reference declared criteria. Nested audiences require matching independently indexed containers. Report, Dashboard and NavigationLinkSet targets bind their exact kind/name or catalog ID. ExperienceVariation targets require bundle analysis and remain partial. Org-only containers, unknown properties, malformed/duplicate criteria and missing target identities also remain explicit gaps; support for the current default audiences is not a claim to complete runtime personalization analysis.

Graph schema version 1 has `nodes`, `edges`, `coverage`, `diagnostics`, `stats`, and optional `facts`. IDs use case-insensitive Salesforce identities, not checkout paths. Nodes include ownership and file/line evidence; edges include relationship, `resolved`/`unresolved`/`ambiguous` binding, and confidence. Lookup traversals retain both the intermediate lookup-field dependency and the final field. Function overloads have signature-specific IDs. Graphs are per org; the caller must enforce tenant/connection boundaries.

Engine `salesforce-8` includes source SHA-256 on nodes and references for hash-verified evidence previews, per-node coverage, and `is_test` on Apex test declarations/methods. Test-class calls and field access remain ordinary static evidence, not proof that tests ran or covered a line. Flow assignments distinguish writes from reads; merge expressions inside declarative text, FlexiPage record context, ReportType table/relationship scopes, report dollar-delimited field paths, CustomMetadata value fields and global-value-set references are extracted explicitly. An identity-only component can have proven incoming usages even when it has no standalone source file.

Type-scoped XML adapters follow Salesforce's [Metadata API Developer Guide](https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/api_meta.pdf) (v68, September 18, 2026). `PathAssistant` uses its explicit entity, picklist, step fields and record type; `AnimationRule` respects All/Master/Custom scope. `__MASTER__` is scope, not an invented record-type declaration. Labels, guidance, picklist values, usernames in unrelated properties and arbitrary reference-shaped XML are not dependencies. Unexpected properties, missing/duplicate reference contexts and mismatched roots remain partial with source-line diagnostics.

Role access settings are configuration, not effective grants. Queue membership preserves direct, internal-subordinate and all-subordinate selectors without expanding users or inheritance. Explicit usernames are unresolved `SalesforceUser` references and keep the queue partial; no User records are fetched. Service-channel priority fields resolve against both real field names and exact catalog IDs, with object/type/case checks and ambiguous collisions retained. Unclassified console components remain unresolved instead of being guessed to be Apex or Lightning.

Lead conversions use one owned `LeadConversionMapping` member per declared field pair, with reads and writes kept separate. Consumers should roll these members up to their owning settings component for impact counts but must not traverse through unrelated sibling pairs. Moderation retains explicit object/field/keyword/user-criteria links. Engine 16 models the documented Metadata API-only `RawBody`/`RawCommentBody` selectors separately from normal API fields (see below). Disabled topic settings still reference their explicit object, and profile policies bind actual profile names without display-label guesses. These adapters add static semantics, not complete runtime behavior or effective access evaluation.

Engine `salesforce-9` adds Experience Cloud and presence adapters. `Network` is the site's named identity; explicit `site` and `picassoSite` properties link its separate `CustomSite` and `SiteDotCom` components. Do not replace spaces with underscores or append suffixes to guess these identities. Moderation rules, keyword lists and user criteria use the documented site prefix of their full names. Networks link documented email-template slots, profiles, permission sets, custom tabs and footer documents. Role labels, page-override enums, standard tabs, keywords, user-type selectors and literal prose are not references. Device-activation templates use exact catalog IDs, not a display-name fallback.

Navigation menus bind their explicitly typed `Network` or `CommunityTemplateDefinition` container, Salesforce object targets, independently declared list views with matching object/ID/type/case, and tile ContentAssets. Valid submenu children retain these links; unsupported shapes and invalid nested menu labels stay partial. External URLs do not become metadata; internal routes and runtime navigational topics remain explicit gaps. Custom sites link Visualforce pages, static resources, guest profiles and certificates. Usernames, legacy portal identity and feed records are not fetched or guessed. Presence configuration links profiles and presence statuses, but does not infer effective user access or split undocumented decline-reason lists. Unknown properties—including undocumented newer template slots—remain partial rather than being silently accepted.

Engine `salesforce-10` adds typed object/field/global-value-set/standard-value-set translations. Locale-qualified filenames establish the parent; translated labels, help, picklist text and section titles are literal data. Field sets, quick actions, record types, sharing reasons, validation rules, web links and workflow tasks use exact object-scoped names; layouts use their object-hyphen identity. Embedded field translations retain their own IDs and containment. Opaque standard-field translation keys are not converted into plausible API names, and legacy packaged-name spellings are not reordered. Missing or ambiguous identity bindings mark affected coverage partial on each build without poisoning cached syntax facts; adding a verified declaration can close the gap on a later rebind.

Lightning themes bind their default BrandingSet by documented name or independently cataloged ID, preserving ambiguous collisions. Known image properties bind exact ContentAsset names or the documented local `/file-asset/<API name>` route with an optional numeric version. The original route/version remains evidence, not proof that that payload version was analyzed. Absolute URLs, org overrides, arbitrary basenames and encoded paths are not normalized into local metadata. See Salesforce's [asset URL contract](https://help.salesforce.com/s/articleView?id=004652690&language=en_US&type=1). Unknown branding properties and definition identities stay partial.

ContentAsset, Document and StaticResource XML envelopes report `asset_payload_not_analyzed` unless a supported original payload is independently validated and paired (CSV or engine-22 images). Asset `originNetwork` and exact document-folder references are supported, but client filenames, zip entries and the provider-reserved asset-link `name` are not guessed as metadata identities. GlobalValueSet values and RemoteSiteSetting URLs are data/configuration, not invented dependencies; a fully understood component can legitimately have no outgoing links. Custom notification `NotificationApiAction` targets link Apex classes; client-side `Share` actions do not. These adapters do not evaluate effective sharing, download binary payloads or execute notification actions.

Engine `salesforce-11` adds seven policy adapters. FieldRestrictionRule uses its documented User/Employee target and FieldSet/ComplianceCategory discriminator. A bounded expression parser extracts record-field and `$User` reads, never evaluates formulas, and rejects malformed or excessive syntax. Known-function arities are checked; unfamiliar calls/globals stay partial. Strings, function names and compliance-category values are not fields or field sets. Missing independently declared targets keep coverage partial until a later rebind supplies them.

NotificationTypeConfig owns a `NotificationDeliverySetting` for each distinct notification name. Each member references its notification and configured application without crossing sibling settings. Actual custom notifications bind only to independent CustomNotificationType declarations; provider-standard names remain unresolved NotificationType identities, not invented custom components. Application API names prefer an exact ExternalClientApplication over ConnectedApp, following the guide's ECA precedence rule. Salesforce also identifies ECA as [external client apps](https://developer.salesforce.com/docs/platform/accsdk/guide/acc-sdk-setup-auth-external.html); package prefixes are never stripped. Disabled settings still describe configuration, not effective delivery or access.

Prompt images bind declared ContentAssets; conflicting image/video/link media stays partial. Deprecated explicit app-name/namespace fields have a defined identity contract. Documented custom-permission and encoded profile filters link their declarations; standard-permission filters remain literal configuration. Engine 17 also binds the verified modern application slot (see below). User/page keys, unimplemented experience contexts, unknown criteria and new properties stay partial rather than becoming guessed metadata. Body text and external URLs are not parsed as merge templates.

CleanDataService owns separate CleanDataRule and CleanDataMapping members. The v68 guide's input/output example (printed pages 614–615) places Salesforce fields and virtual data-service fields on opposite sides of each mapping. The adapter derives direction from the rule, mapping, row and pair object contexts; only exact independently declared Salesforce API names resolve. Virtual names, field labels and internal match-engine/rule keys never become guessed Salesforce fields or MatchingRules. Missing/ambiguous context stays partial. Virtual service schema is not indexed, so these sources retain that explicit gap even when all their Salesforce field links resolve. Impact counts may roll members up to the service; dependency traversal must not jump across unrelated mappings or rules.

Group definitions, CampaignInfluenceModel options and CspTrustedSite URLs/policy switches have validated literal contracts and can legitimately have no outgoing metadata references. Unknown properties/enum values and missing required identity contexts remain partial. Group's `membership_analysis: not_in_metadata` and diagnostic explicitly state that Metadata API group definitions do not supply actual group members; semantic definition coverage is not membership or effective-access analysis.

Engine `salesforce-12` adds ExternalClientApplication and its four OAuth/configurable-settings types. Each settings file links its explicitly named app, even when disabled. Documented Apex handlers and custom OAuth scope names bind only their exact kinds. Preauthorization PermissionSet selectors and asset-token signing Certificate selectors use independently cataloged, case-sensitive Salesforce IDs, never a name fallback. The guide's PermissionSet example contradicts its ID field definition; Salesforce's [first-party-app configuration instructions](https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_eca_auth_challenge.htm&language=en_US&type=5) confirm the ID contract. Profile selector encoding, execution users, opaque assertion certificates and internal/reserved fields stay explicit gaps.

Custom OAuth attributes accept only the documented qualified field selector (including relationship traversal verified by the scoped schema). Arbitrary formula syntax, duplicate attribute keys, unsupported scopes/settings and malformed or excessive lists remain partial. Standard OAuth scopes such as `Profile`, `Email` and `CustomPermissions` are literal capabilities, not metadata references or effective grants. Consumer keys/secrets, PEM certificate contents, callback URLs, OAuth consumer links and audiences are never copied into graph nodes, edges or cached syntax facts. A source-preview integration must separately redact sensitive source values: this parser's evidence hashes and line numbers still describe the original input.

AppMenu extracts explicit type/name pairs for CustomApplication, ConnectedApp, ExternalClientApplication, Network and CustomTab. StandardAppMenuItem names are built-in navigation entries, not invented custom tabs. Unknown kinds, excluded packages and absent declarations are not guessed from similar names. The v68 guide reserves AppMenu's contract, so these links are explicitly inferred from typed XML slots and the component remains partial even when all supplied names bind.

Engine `salesforce-13` adds typed setup definitions and six explicitly routed Settings roots. Lightning settings link the named active custom theme; identity-provider settings link their certificate; search settings link exact objects; territory settings link their Apex assignment handler and documented supported objects. Forecast settings link independently indexed ForecastingType declarations. Qualified column selectors bind verbatim against the schema and `00N` selectors use case-sensitive catalog IDs: no underscore removal, label matching, guessed base object or unrelated report-type mapping. Unqualified columns, unsupported forecast configuration and unbound aliases remain partial. Other Settings roots retain structural coverage; a boolean-looking file is not automatically semantic.

HomePageLayout links custom HomePageComponent names and recognizes a bounded set of platform widgets. Unknown standard-widget tokens remain partial. ForecastingType validates modes/options and explicit model/group/split references; numeric date modes never manufacture date-field dependencies. Legacy Community means a zone, not an Experience Cloud Network; documented inactive Chatter Answers properties are ignored. Iframe allowlist URLs are literals, not Visualforce components. Empty, fully supported definitions may legitimately have zero dependencies.

ManagedTopics uses the file's exact site identity and site-scoped member/parent declarations, with duplicate, cycle, dotted-scope and unknown-enum guards. ManagedContentType declares its named CMS fields (including the provider-returned built-in MEDIA definition kind), not Salesforce fields or CMS content records. Its name-field, field-count and localization rules are checked. This is definition coverage, not embedded CMS content analysis. NetworkBranding links its explicit Network and Document slots; dynamic asset URLs remain partial. SiteDotCom archives, undocumented CallCenter adapter settings and notification User records are not silently treated as analyzed. Email recipients and literal adapter/configuration values are not copied into graph facts.

Engine 23 adds bounded Open CTI CallCenter settings, based on Salesforce's
[required](https://developer.salesforce.com/docs/service/api-cti/guide/sforce-api-cti-call-def-file-required.html),
[optional](https://developer.salesforce.com/docs/service/api-cti/guide/sforce-api-cti-call-def-file-optional.html)
and [sample](https://developer.salesforce.com/docs/service/api-cti/guide/sforce-api-cti-call-def-file-sample.html)
contracts. Exact local `/apex/PageName` primary and standby URLs reference
independently declared ApexPage identities, preserving namespaces. Mirrored
root, section and flat-JSON settings must agree; each original XML line/hash
remains evidence, without duplicate primary edges. Primary and standby keep
separate configuration roles, even when they name the same page. These are
configured references, not proof of runtime execution or which adapter wins.

Only documented general/dialing settings are interpreted. The parser bounds
JSON to 64 KiB, sections/items/JSON keys to 128 each and values to 4,096
characters. Duplicate keys, nested JSON, unknown sections/options, invalid
dimensions/modes, incomplete standby/timeout pairs, Canvas overrides and legacy
adapters retain explicit gaps. Voice-only channel definitions do not acquire
Open CTI requirements. Absolute HTTP(S) locations are not guessed local pages;
dynamic, credential-bearing or unverified relative URLs remain partial. Adapter
URLs and arbitrary setting values are not serialized into facts or fetched.
Missing/excluded page targets remain unresolved and rebind when the scoped
catalog changes. No User or business records are queried.

Profile-backed permission captures use `PermissionSet` API sources at `salesforce-api/permissions/<catalog Salesforce ID>/{PermissionSet,FieldPermissions,ObjectPermissions,SetupEntityAccess,status}.json`. API version `v63.0` includes `PermissionsViewAllFields`; this broad object permission does not invent an individual grant for every field. Owner identity, `IsOwnedByProfile`, ProfileId, record IDs, ParentId and boolean permission flags are validated. Object/field grants require independently declared targets. Setup access resolves only against exact IDs supplied by the scoped catalog; missing IDs stay unresolved and collisions ambiguous. A verified profile owner adds a structural Profile → backing PermissionSet member link, allowing profile exploration to reach its grants. Explicitly disabled permissions in REST and XML use `configures_access`, not `grants_access`.

Permission edges retain both the capture hash/line and the latest per-section status hash/line. A failed refresh preserves old evidence but marks affected links with its capture status; consumers must not show it as current. A complete API capture means only that all requested direct permission records were returned. It is not a full Profile retrieve or effective-user-access calculation: assignments, groups, muting, license constraints, implicit access and runtime context are not inferred. Profile-backed permission components remain partial.

Salesforce sometimes returns a bare `Field` API name in a FieldPermissions row. Its explicit `SobjectType` supplies context; this is not a guessed object name. Both qualified and bare spellings still require an independent field declaration before a link is resolved.

Report-type Analytics responses use `ReportType` sources at `salesforce-api/reportTypes/<URL-encoded API type>/describe.json`. The requested identity must match the response and the catalog identity (custom report types may omit the API-only `__c` suffix). Columns are scoped to that report type; exact aliases win over dollar-delimiter normalization, and conflicting mappings remain ambiguous. Binding tries verified relationship paths and actual declared sObject fields, never display labels. Edges retain both report XML provenance and `binding_evidence` for the captured mapping. Computed aliases are not invented fields. A sibling `status.json` marks failed/inaccessible checks partial while preserving prior evidence, including its status on affected bindings. Storage adapters should omit column dictionaries from interactive node payloads.

Foldered metadata retains its catalog identity when a verified source path contains a fuller folder hierarchy. The exact path-derived name is indexed as an alias for cross-file references. Basenames are never guessed; colliding aliases remain ambiguous. Storage integrations must verify returned file ownership before constructing a `Source`.

Apex collection receivers retain their complete generic type. Platform list/map/set calls do not become fabricated methods on the element's object. Indexed elements, typed `get()` results, map `values()`, nested collections and collection DML retain their actual field/object dependencies. Cross-file return receivers use the bounded engine-19 contract below; unproven receivers still produce `apex_receiver_type_unresolved` and partial coverage rather than invented field paths. Collection overloads remain distinct from element overloads.

Nodes and edges have deterministic source-local ordering, so repeated provenance stays close enough for ordinary gzip to compress effectively. This changes ordering, not identities or relationships; consumers must use IDs, not array positions.

Captured describe JSON is supplied explicitly as a `CustomObject` source at `salesforce-api/sobjects/<Object>/describe.json`, with `source_kind="api"`. Only fields and relationships actually present in that response are declared. API response evidence is distinct from retrieved XML. The Tooling `(hidden)` sentinel produces a `source_hidden_by_salesforce` availability diagnostic, not a syntax error. Bundle coverage retains partial warnings while allowing a semantic JS adapter to supersede a structural CSS sidecar.

Constant-string analysis never executes Apex. Literal concatenation, local assignments and final string constants can resolve dynamic SOQL and `Type.forName`; branches, loops, unknown assignments and mutable class fields invalidate the constant. Derived query references retain the original Apex source hash and call-site line.

Trusted storage integrations can supply `Source.content_sha` to compute fingerprints without transferring unchanged bodies. They must supply matching previous facts for omitted bodies and verify the SHA when loading changed source; a hash is not a substitute for source on a cache miss.

The caller supplies the complete current source set. Files omitted from the next build are pruned, and unchanged caller files are rebound so deleted targets become unresolved. A source-cache fingerprint includes engine version; bump `ENGINE_VERSION` whenever extraction semantics change. This is not a Salesforce deletion-detection API.

## Additional typed Settings and Network contracts (engine 15)

The Settings scalar registry covers 1,473 explicitly typed slots across 106
roots, including five reviewed enum slots. These are provider WSDL contracts,
not a heuristic that promotes boolean-looking XML. Each root/property is exact
and case-sensitive; required values, scalar cardinality, bounded numbers,
closed enums and unknown properties are checked. A root can become semantic
only when every supplied property is understood. Unknown roots stay structural;
unreviewed string/complex properties stay partial. No runtime network lookup is
needed. The registry pins Salesforce's
[structured metadata documentation](https://github.com/forcedotcom/sf-skills/tree/c217b703b3e5a3c279f1a510d8703161b14bd0a5/skills/platform-metadata-api-context-get/assets/metadata_api)
by revision and input SHA-256. WSDL spelling takes precedence over prose-table
typos, such as `defaultQueueableDelay`, `fileType` and `enableNewToReadTriggers`.

Nested Company fiscal-year, My Domain URL, Employee User, file-download policy,
real-time event and Case settings have separately reviewed contracts. Employee
profiles and permission sets, event entities, Case email templates and Apex
handlers, routing flows, fallback queues and Case record types link independent
typed declarations. Picklist defaults reference their fixed Case/Task field,
never an API name guessed from the picklist's text. Disabled configuration still
retains its references. User records, missing/ambiguous identities, unsupported
values and future properties remain gaps. Addresses, domain suffixes and other
literal configuration do not produce arbitrary metadata edges.

Network now recognizes the WSDL's optional `enableExpFriendlyUrlsAsDefault` and
`enableLWRExperienceConnectedApp` booleans. Its headless registration and password
reset template fields bind exact EmailTemplate identities. Salesforce documents
these [headless-flow template settings](https://help.salesforce.com/s/articleView?id=sf.headless_identity_experience_settings_parent.htm&language=en_US&type=5).
This does not claim full semantics for undocumented Network fields.

## Moderation content selectors (engine 16)

The pinned Salesforce [ModeratedEntityField contract](https://github.com/forcedotcom/sf-skills/blob/c217b703b3e5a3c279f1a510d8703161b14bd0a5/skills/platform-metadata-api-context-get/assets/metadata_api/ModerationRule.json)
defines `FeedItem.RawBody` and `FeedComment.RawCommentBody` exclusively for the
Metadata API. They are content selectors, not aliases for REST `Body` or
`CommentBody`. `moderates_content` edges retain the exact selector and its XML
line/hash while binding the independently declared entity. No synthetic field
declaration is added; field Where Used views do not claim a false alias.

Ordinary moderation fields still require real field declarations. Site, entity,
keyword list and repeatable user-criteria references have independent identity
checks. Supported action/boolean/integer/enum literals and cardinality are
validated; unknown properties, wrong selector contexts, missing identities and
bounded-input limits keep coverage partial. Disabled rules remain configuration,
not evidence of actual moderation, membership, delivery or user access.

## Prompt application identity and values (engine 17)

A read-only Metadata API 62.0 `describeValueType(PromptVersion)` check verified
`customApplication` as a `CustomApplication` foreign key and `experience` as
the `Lightning` / `Site` enum. The reviewed adapter records the provider-schema
SHA-256 in `graphify/salesforce/prompts.py`; it does not ship customer metadata,
query records or turn every non-foreign-key string into a proven literal.

Application names bind independently indexed, type-scoped API identities.
IDs bind only matching case-sensitive catalog IDs, not prefixes or labels.
Missing, excluded or ambiguous targets keep coverage partial. Conflicting
modern and deprecated application slots do not choose an arbitrary winner.
References carry their XML line, source hash and schema-contract name.

Required version fields, integer/boolean/date lexical forms and documented
enum values are validated. Duplicate, nested, empty typed, unknown and oversized
version collections remain explicit gaps. `experienceContext`, user identities,
page keys and record-type context are still unsupported; recognizing a field
in the provider schema alone does not implement its semantics. Coverage is
per supplied component, not a promise of every possible Prompt configuration.

## Nested Address, Business Hours and Security settings (engine 18)

These three roots have reviewed, type-scoped adapters backed by the pinned
Salesforce metadata documentation. Address country/state codes, labels and
integration values are literal configuration, not invented object or field
dependencies. Required values, duplicate codes and default-country cardinality
are checked.

Business Hours definitions become owned `BusinessHoursEntry` members. Holidays
are separate `BusinessHoursHoliday` members, including when their labels are
identical. Explicit holiday-to-hours names bind independent declarations and
retain source-line/hash evidence. Impact counts can roll members up to Settings;
dependency paths must not cross unrelated sibling holidays. Weekday times,
holiday recurrence values and dates are validated; timezone IDs use the pinned
`tzdata==2026.2` package. This is static configuration, not a scheduling engine.

Security settings validate documented network ranges, password policies,
session settings and single-sign-on literals. `welcomeEmailTemplateId` binds
only an independently cataloged, case-sensitive EmailTemplate ID. Names and
wrong-type IDs are not fallbacks. Reserved `lockerTrustedResources`, unknown
properties, malformed values and absent or ambiguous targets remain partial.
This parser does not change security settings, read User records or evaluate
effective access.

The nested-settings suite adds 165 synthetic cases covering documented values,
literal rejection, malformed and oversized collections, duplicate labels,
identity rebinding, timezone portability and source provenance. Service tests
also check holiday sibling isolation and Security → EmailTemplate → field
dependency paths. These are supported static contracts, not complete semantics
for every Settings root or a claim that every org has those references.

## Original image bytes and DocumentFolder metadata (engine 22)

`Source(..., source_kind="binary", binary_content=bytes)` keeps original bytes
separate from text. `scan_project` preserves these bytes; Document identities
retain their folder and original extension. Binary hashes participate in source
fingerprints, but raw bytes never appear in graph/facts JSON.

PNG and JPEG validation is limited to 2 MiB compressed input, four million
pixels, 8,192 pixels per dimension and a single frame. PNG preflight checks chunk
bounds/CRCs, terminal IEND, unknown critical chunks and bounded compressed
ancillary metadata (1 MiB total); animated PNG is unsupported. JPEG must have
its expected signature and terminal EOI without appended data. The pinned
[Pillow validator](https://pillow.readthedocs.io/en/stable/reference/Image.html)
verifies the container, then reopens and actually decodes the pixels. Missing
dependencies, permissive truncated-image settings, oversized images, malformed
data and unsupported formats remain partial. This is bounded format validation,
not an image-content classifier or a claim that every possible image subtype is
supported. Pixels, comments, EXIF and embedded strings are not metadata references.

Payload success alone cannot close a component gap. The exact adjacent
descriptor must match its kind, component, namespace and path. Document's
required `internalUseOnly`/`public` booleans are validated; the original extension
must agree with the decoded PNG/JPEG format. StaticResource requires its actual
image MIME type and valid cache-control setting. Missing/duplicate descriptors,
conflicting inline bodies, unsupported properties and other envelope diagnostics
remain partial. Both evidence hashes are retained and paired on every build,
including cached-fact reuse. A Document's folder must independently exist in the
current scoped declarations; it is never synthesized from the filename.

`DocumentFolder` handles the Metadata API's extensionless MDAPI folder XML and
DX `.documentFolder-meta.xml` source form. Required display name, access type,
public-folder access and exact explicit identity are checked. Labels remain
literal; group/role share recipients require independent metadata declarations.
User/manager recipients emit `metadata_only_identity_boundary`; legacy sharing
selectors and other unverified recipient kinds remain partial. Folder definition
coverage does not evaluate effective access or expand membership.

Storage integrations can pass `load_source(source) -> Source` to `build_graph`.
It is called only for cache misses, must preserve the inventoried fingerprint,
and lets the caller load bounded batches without retaining all binary bodies.
The integration is responsible for authorization, size limits and verifying the
supplied bytes against its stored hash. Cache-hit parse/reuse counters remain
accurate. The engine makes no database or Salesforce calls itself.

## Literal CSV resource payloads (engine 21)

A StaticResource body is assessed as literal CSV only when its current adjacent
`.resource-meta.xml` descriptor declares `text/csv` or `application/csv`. The
component identity, namespace and full source path must match. Coverage is
also gated by valid descriptor semantics: Metadata API v67's captured
`describeValueType(StaticResource)` requires `contentType` and a `Private` or
`Public` cache setting. Conflicting inline content and mismatched explicit names
remain partial. CSV parsing is
bounded to 2 MiB, 100,000 rows total, 256 columns and 16,384
characters per cell. UTF-8 BOMs, quoted commas/newlines and doubled quotes are
supported; malformed, ragged, oversized and unsupported tables remain partial.
This follows the [CSV format](https://www.rfc-editor.org/rfc/rfc4180) rather than
interpreting data as a Salesforce formula or component-name manifest.

Coverage retains both descriptor and payload hashes. Public graph facts contain
format/dimensions and provenance, not table cells. Source previews may show the
authorized original file. Removing/changing either source invalidates the
cross-file proof even when syntax facts are reused. Other descriptor warnings
still prevent semantic component coverage. A valid literal dataset can have no
outgoing metadata dependencies; actual callers remain incoming usages.

Images use the separate engine-22 contract above. Binary archives, JavaScript
resources and SiteDotCom payloads are not covered by either payload contract;
their gaps remain visible.

## Static schema reflection (engine 20)

Verified `Schema.SObjectType.Account`, `Account.SObjectType.getDescribe()` and
literal `Schema.getGlobalDescribe().get('Account')` chains link the independently
indexed object. Developer-name selectors from
`getRecordTypeInfosByDeveloperName()` link the actual object-scoped RecordType;
`getRecordTypeInfosById()` requires an independently supplied, case-sensitive
metadata ID. Both retain the caller's source hash/line and available declaration
proofs. Catalog-only targets can have incoming references, but are never presented
as captured source excerpts.

`apex_schema.json` pins 35 reviewed signatures from the same metadata-only
Tooling API v67 capture as the platform catalog. Return signatures alone do not
prove a metadata identity. The [Apex reference](https://resources.docs.salesforce.com/latest/latest/en-us/sfdc/pdf/salesforce_apex_reference_guide.pdf)
distinguishes developer names from localized record-type labels. ByName selectors,
runtime keys/IDs, unreviewed field-map reflection and unknown declarations remain
partial. Nothing executes Apex or queries User/RecordType rows.

Only bounded selector constants are considered, and reusable syntax facts retain
their SHA-256 digests rather than literal values. Binding matches independent
in-scope declarations; removing a declaration or excluding its package invalidates
the link even when the caller's syntax facts are cached. Customer classes and
variables named `Schema` cannot inherit platform shortcuts. The 45 reflection
regressions cover these boundaries, direct-token usages, arity, source provenance,
wrong-case keys, ID/object mismatches and incremental rebinding.

## Bounded Apex receiver typing (engine 19)

Deferred receiver expressions are rebound against the **current scoped** class,
method and schema declarations. Supported forms include method return chains,
overload selection, inherited/nested class members, typed properties, indexed
collections, and generic List/Map results. A returned `Account` can establish a
real `Account.Name` field usage. A DTO property instead references the actual
owning Apex class with `apex_member`; it never becomes a fabricated CustomField.
Same-component property access does not add self-usage links.

The packaged `apex_platform.json` pins 332 reviewed System data-return signatures
from Salesforce Tooling API v67.0 `completions?type=apex`. The raw provider response
SHA-256 is `3a8c609cccb49b2383c13a6157bdc8b802a981ff4473cfc7d95c7b5fef6a9cda`.
The generator copies only selected method names, static/instance flags, argument
types and return types—not customer source, method bodies or documentation.
This allows supported Date, Datetime, PageReference and other data-return chains
to be understood without inventing platform metadata nodes. Unqualified names
first consider scoped customer declarations; explicit System types and platform
literal/return types cannot silently become similarly named custom classes.

Call sites retain their own source hash/line plus secondary declaration evidence
used for return/property binding. Reused syntax facts contain the unbound
expression, never a cached final target. Removing or changing a callee, or
excluding its package, re-evaluates the caller and restores its gap when needed.
Storage/read APIs should omit `apex_fields` and `apex_signature_verified` from
interactive node payloads while retaining them in the reusable fact cache.

Expression traversal is bounded by depth, node and argument limits. Each nested
call has its own byte-span identity; resolving an inner call cannot clear an
unresolved outer call. Unknown types, unsupported platform APIs, ambiguous
overloads and other syntax/retrieval diagnostics are not promoted to complete.
This is not a complete Apex compiler, runtime evaluation, complete reflection support or
proof of effective visibility/access. No User or business-record lookup occurs.

## Maintenance and verification

```sh
python scripts/build_salesforce_registry.py /path/to/metadataRegistry.json 12.37.1
python scripts/build_salesforce_settings_literals.py /path/to/pinned-metadata-docs
python scripts/build_salesforce_apex_platform.py /path/to/captured-v67-system-symbols.json
python scripts/build_salesforce_apex_schema.py /path/to/captured-v67-system-symbols.json
uv run --extra salesforce pytest tests/test_salesforce_graph.py tests/test_salesforce_permissions.py tests/test_salesforce_declarative.py tests/test_salesforce_experience.py tests/test_salesforce_translations_assets.py tests/test_salesforce_policies.py tests/test_salesforce_external_clients.py tests/test_salesforce_setup.py tests/test_salesforce_settings.py tests/test_languages.py
```

The registry records its source hash/version and Salesforce's Apache-2.0 attribution. Graphify's upstream Apache-2.0 license and NOTICE remain in force; the grammar-pack distribution retains its upstream grammar licenses. No Salesforce customer source is included in the fixtures.

The engine-20 full fork suite passes 13,486 tests (97 optional skips). This includes a parametrized identity/coverage contract for every registered type, 47 external-client/menu regressions, 71 initial setup/settings regressions, 65 Audience/field-identity/scope regressions, 6,186 additional Settings/Network cases, 165 nested-settings cases, 46 bounded Apex receiver cases and 45 static reflection cases. Every generated scalar slot has positive, wrong-type, empty and nested-value tests. Fixtures also cover typed reference bindings, secret/literal rejection, malformed/bounded input, exact IDs, incremental rebinding, virtual-schema isolation and ambiguous contexts. This is a coverage contract, not a promise of complete semantics for all 533 types. Cross-object relationship binding uses precomputed parent/child schema indexes rather than scanning every field per reference. Permission record line indexing is linear in source size rather than repeatedly rescanning large captures.
