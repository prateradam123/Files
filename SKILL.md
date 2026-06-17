# outbound-payload-lineage

Use this skill when the user asks to trace where every data point in an outbound object, request, message, event, payload, response, vendor DTO, Kafka record, MQ message, API body, or persisted output object came from.

The primary output is a deterministic field-level lineage JSON report. The secondary outputs are a human-readable Markdown report and a self-contained HTML viewer packaged from the reusable React Flow viewer template.

## Core promise

For the requested outbound object, identify every field on the object, including nested object and list item fields, then trace each field value backward as far as reachable.

Do not stop at setters, builders, constructors, mapper classes, or helper method names. A setter/builder/mapper is only the first assignment point. Keep tracing the value source backward through local variables, method parameters, caller arguments, method returns, service calls, repository/entity values, input events, external service responses, config, constants, generated runtime values, or unknown/unavailable boundaries.

Computed and derived fields are not terminal. Treat them as transform nodes and recursively trace every input that contributes to the computed value.

## Expected repo setup

This skill folder contains:

```text
.agent-skills/outbound-payload-lineage/
  SKILL.md
  schemas/lineage-report.schema.json
  examples/lineage-report.example.json
  scripts/package_viewer.py
  scripts/validate_lineage_report.py
  viewer-template/prebuilt-viewer-template.html
  viewer-template/reactflow-lineage-viewer-vite-source.zip
```

The viewer source is included for maintenance, but normal skill runs should not rebuild or redesign the UI. Normal runs only generate JSON/Markdown and package the JSON into the prebuilt viewer shell.

## Bootstrap rule

If the repository does not already contain the skill, copy this folder into:

```text
.agent-skills/outbound-payload-lineage/
```

If the repository does not already contain a working viewer source and the user wants one, copy `viewer-template/reactflow-lineage-viewer-vite-source.zip` into:

```text
tools/lineage-viewer/
```

Do not overwrite an existing viewer source unless the user explicitly asks to reset or upgrade it. Treat existing viewer source as user-owned code.

## Normal output contract

For each tracing run, create a new folder:

```text
lineage-runs/<safe-run-name>/
  lineage-report.json
  lineage-report.md
  lineage-viewer.html
```

`lineage-report.json` is the source of truth.

`lineage-report.md` is a readable summary.

`lineage-viewer.html` is a self-contained report: compiled React Flow UI + CSS + that run's JSON embedded in the HTML. It should be openable directly in a browser without npm, a dev server, internet, or a separate JSON fetch.

Package the HTML using:

```bash
python .agent-skills/outbound-payload-lineage/scripts/package_viewer.py \
  --json lineage-runs/<safe-run-name>/lineage-report.json \
  --template .agent-skills/outbound-payload-lineage/viewer-template/prebuilt-viewer-template.html \
  --out lineage-runs/<safe-run-name>/lineage-viewer.html
```

Validate the JSON using:

```bash
python .agent-skills/outbound-payload-lineage/scripts/validate_lineage_report.py \
  lineage-runs/<safe-run-name>/lineage-report.json
```

## Inputs to accept

The user may provide any of:

- Output object class name
- Endpoint/controller response type
- Vendor request class
- Kafka/MQ/event payload class
- Method where object is sent/published/returned
- Client method that sends the object
- Mapper/builder class
- Example field name
- Repository/service package hints

If the user gives only an object name, search usages to find likely outbound surfaces. If multiple outbound surfaces exist, proceed with the strongest match and list alternatives in the Markdown report.

## Step-by-step tracing process

### 1. Identify the outbound surface

Find where the object leaves the system.

Examples:

```text
kafkaTemplate.send(...)
producer.publish(...)
restTemplate.postForEntity(...)
webClient.post().bodyValue(...)
mqClient.send(...)
return ResponseEntity.ok(payload)
return DTO from controller
repository.save(outputObject)
externalVendorClient.submit(request)
```

Record the outbound surface in `object.outboundSurface`.

### 2. Discover the object hierarchy

Build `objectShape` as a nested tree. Include:

- Root object
- Nested DTOs
- Lists/arrays with `[]` paths
- Leaf fields
- Superclass fields
- Record components
- Lombok builder fields
- Jackson `@JsonProperty` names where relevant
- MapStruct target names where relevant

Use paths like:

```text
applicationId
applicant.fullName
financialTerms.paymentToIncomeRatio
vehicles[].vin
decision.reasons[].text
metadata.sourceSystem
```

Every leaf field in `objectShape` must have a matching entry in `fields`.

### 3. Find assignment points

For each leaf field, find all ways it can be populated:

- Setter calls: `setField(...)`
- Builder calls: `.field(...)`
- Constructor args
- Record constructors
- Direct field assignment
- Manual mapper methods
- MapStruct annotations and generated implementations if available
- Object copy utilities such as `BeanUtils.copyProperties`
- JSON/object mapper conversions
- Stream/list/loop item mapping
- Helper methods that build nested objects
- Default/fallback assignments

Do not stop here. Assignment is only the first node in the lineage trace.

### 4. Trace value sources backward

For every assigned value, recursively trace the source:

- Local variables
- Method parameters
- Caller arguments
- Method returns
- Helper methods
- Mapper methods
- Service methods
- Entity fields
- Repository calls
- DAO/SQL/Mongo lookups
- Inbound Kafka/MQ/API/request payloads
- External service responses
- Config/properties/env/feature flags
- Constants/enums/defaults
- Runtime generated values such as `Instant.now()` or UUIDs

When a method parameter is used, find callers and trace the passed argument backward.

When a method return is used, inspect the callee and trace what the method returns.

When a variable is assigned from another variable or expression, trace that expression and all inputs.

### 5. Expand derived/computed fields

A derived field is never complete until every contributing input has been traced as far back as possible.

Bad:

```text
fullName <- NameFormatter.buildFullName(applicant)
```

Good:

```text
fullName
<- setFullName(buildFullName(applicant))
<- NameFormatter.buildFullName(applicant)
   ├─ applicant.firstName
   │  <- ApplicantRepository.findByApplicationId(...)
   │  <- Database boundary: applicant.first_name
   ├─ applicant.middleInitial
   │  <- ApplicantRepository.findByApplicationId(...)
   │  <- Database boundary: applicant.middle_initial
   ├─ applicant.lastName
   │  <- ApplicantRepository.findByApplicationId(...)
   │  <- Database boundary: applicant.last_name
   └─ delimiter " "
      <- hardcoded constant
```

For formulas, conditionals, formatting, translations, template rendering, decision rules, list transformations, and enrichment logic, show every input branch.

### 6. Handle branches

If a field can come from multiple branches, show all branches.

Examples:

```text
status
├─ approved branch: DecisionEngineResult.approvalStatus
├─ declined branch: DecisionEngineResult.declineStatus
└─ fallback: PENDING_REVIEW constant
```

For each branch, trace branch inputs as far backward as possible.

### 7. Handle lists and nested objects

For list fields, trace both the collection source and each item field.

Example:

```text
decision.reasons[]
<- decisionResult.getReasons()
<- DecisionEngineResult boundary

decision.reasons[].text
<- reasonMessageBuilder.build(reason.code, locale)
   ├─ reason.code <- DecisionEngineResult.reasons[].code
   ├─ locale <- application.yml
   └─ template <- MessageTemplateRepository
```

### 8. Stop only at clear boundaries

Allowed stop points:

- Input boundary: Kafka/MQ/API/controller request/batch file/scheduler payload
- Persistence boundary: Repository/DAO/query/entity loaded from DB/Mongo/stored proc
- External service boundary: REST/SOAP/SDK/vendor/client response
- Config boundary: `@Value`, properties, env var, config server, feature flag
- Static boundary: constant, enum, hardcoded default, template
- Runtime boundary: timestamp, UUID, sequence, clock, system principal
- Unavailable boundary: generated code unavailable, reflection, binary dependency, third-party internals, missing source

Every trace must end at a clear boundary or unknown gap.

## JSON report structure

The JSON must follow `schemas/lineage-report.schema.json`.

Top-level shape:

```json
{
  "schemaVersion": "1.0.0",
  "object": {},
  "summary": {},
  "objectShape": {},
  "fields": [],
  "boundaries": [],
  "gaps": []
}
```

Important conventions:

- `objectShape` is the nested DTO/object tree.
- `fields[]` has one entry for every leaf field.
- Each field has `path`, `confidence`, `mappingType`, `description`, and `lineage`.
- Field lineage nodes use relative node IDs like `assign`, `transform`, `repo`.
- The special edge source `FIELD` means the selected output field node.
- Node `kind` values should be from the standard vocabulary.

Standard node kinds:

```text
object, group, array, field, assignment, transform, source, method,
repository, event, external, config, constant, runtime, unknown
```

Standard confidence values:

```text
high, medium, low
```

Recommended mapping types:

```text
direct, derived, conditional, lookup, enriched, config, runtime,
constant, defaulted, direct-list, derived-list, dynamic, unknown
```

Recommended edge labels:

```text
contains, assigned by, value from, read from, computed by, input,
primary input, fallback, selected branch, loaded by, returned by,
called with, uses, produced by, resolved from, unresolved
```

## Markdown report structure

Create `lineage-report.md` with:

1. Object traced
2. Outbound surface
3. Field coverage summary
4. Primary origins
5. Field lineage table
6. Important derived fields and input branches
7. Boundaries reached
8. Gaps/risks
9. Confidence notes
10. How to open `lineage-viewer.html`

## Viewer behavior

The packaged HTML viewer should support:

- Left object hierarchy tree
- Field trace mode for a selected leaf field
- Selected object lineage mode for all descendant fields under an object/list
- Shape map mode for the nested object structure
- Legend drawer explaining node/edge types
- Inspector panel for selected node/field details
- Gaps filter
- Fit graph action
- Minimap and graph controls
- Clean toolbar with compact mode switcher

The UI should be driven entirely by JSON. Do not hardcode object-specific UI.

## Overlap/readability rule

When rendering multiple descendant fields under a selected object, each descendant leaf field should be placed into its own vertical lane. Avoid showing the full root lineage graph by default when the object has many fields. Default to focused field trace or selected object trace.

## Evidence requirements

For each field, include as much code evidence as available in nodes:

- File path
- Class/method
- Line number if known
- Code snippet or assignment expression
- Boundary type
- Reason tracing stopped

If evidence is inferred rather than directly found, mark confidence as medium or low and describe the gap.

## Final self-check before completing a run

Before returning, verify:

- Every leaf field in `objectShape` has a matching `fields[]` entry.
- Every `fields[].path` exists as a leaf in `objectShape`.
- Every field has a lineage graph, even if unresolved.
- Every derived field expands into all known input branches.
- Every branch/fallback is represented.
- Every trace ends at a source boundary or a clear unknown/unavailable gap.
- Dynamic copy/reflection/generation is called out explicitly.
- The JSON validates against the schema.
- `lineage-viewer.html` was packaged from the prebuilt template.
- The final response links or points to the three artifacts.
