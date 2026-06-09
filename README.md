# Files

---

name: data-lineage-investigator
description: Use this skill to trace the data lineage of a target artifact or operation. The target may be an outbound payload, REST request, Kafka/JMS message, generated file, email, webhook, DB query, DB insert/update/delete, repository save, or any other sink where data is sent, written, queried, or transformed. The skill identifies required data points, where they come from, and how they participate in direct mapping, derivation, lookup, business logic, suppression/no-op logic, routing, metadata, config, constants, or local state.

Data Lineage Investigator

Purpose

Trace the lineage of a target artifact or operation and produce a concise report showing:

- what fields, parameters, headers, columns, or query predicates are produced or used
- what data points are required
- where those data points come from
- how each data point participates in mapping or business logic
- which values are direct mappings, derived values, lookup keys, rule inputs, metadata, routing inputs, suppression inputs, config values, constants, generated values, or local state
- which parts are unknown or low confidence

The report should be practical and straightforward. Do not produce a giant architecture document.

---

Valid targets

A lineage target can be any sink or formed artifact, including:

- REST request body
- REST headers
- portal/vendor response
- Kafka message
- JMS/MQ message
- generated file
- email
- webhook payload
- SOAP/XML request
- DB query
- DB insert
- DB update
- DB delete
- repository save
- Mongo query/write
- S3/object write
- any custom client/gateway operation

---

Core principle

Do not only list fields that directly appear in the target.

Also capture data points used for:

- direct mapping
- derived mapping
- lookup keys
- business rules
- send/write/query/no-op decisions
- conditional field inclusion
- suppression/no-send/no-write rules
- routing/destination selection
- validation
- defaulting/fallback logic
- idempotency/staleness checks
- metadata/header/audit fields
- config, constants, secrets, generated values, and local state

A data point can be required even if it never appears directly in the target.

---

Tool usage guidance

Use normal IDE navigation and workspace search first.

Use ast-grep when available to accelerate structural searches. Treat ast-grep as a search helper, not as the analysis itself.

Use ast-grep to find candidate code shapes such as:

- sink calls
- sender/client calls
- repository save/query/update calls
- builder chains
- setters
- constructors
- "Map.put(...)"
- JSON/XML object construction
- header mutations
- query parameter binding
- SQL execution
- entity field assignment
- conditional logic
- switch statements
- validation branches
- suppression/no-op rules

Useful ast-grep search goals:

Find calls that send or publish a payload.
Find repository save/update/query calls.
Find builder methods that populate the target DTO.
Find setters on the target object.
Find Map.put calls used to build a dynamic payload.
Find headers.add / headers.set / putHeader calls.
Find if/switch/filter logic around the target mapping or sink.
Find status/type/enum mapping logic.
Find validation or no-op branches before the sink.
Find query parameter binding or SQL update parameter binding.

Do not rely on ast-grep results alone. After finding candidates, read the source files and verify the flow.

If an existing ast-grep skill is available, use it for search mechanics. This skill is responsible for the lineage investigation and report format.

Graphify or repo-map tools may be used for broad orientation only. Do not require them.

---

Investigation workflow

1. Identify the target

Start from the user’s target, such as:

- payload class
- request/response DTO
- mapper method
- client method
- endpoint
- topic/queue
- DB repository method
- SQL query
- insert/update/save operation
- file/email/webhook sender

If the target is broad, narrow it to a specific artifact or operation before producing the report.

---

2. Find the sink point

Find where the target is actually sent, written, queried, or executed.

Examples:

- "RestClient.post().body(...)"
- "WebClient.post().bodyValue(...)"
- "RestTemplate.exchange(...)"
- "KafkaTemplate.send(...)"
- "JmsTemplate.convertAndSend(...)"
- "repository.save(...)"
- "entityManager.persist(...)"
- "jdbcTemplate.update(...)"
- "jdbcTemplate.query(...)"
- "mongoTemplate.save(...)"
- custom methods like "send", "submit", "publish", "dispatch", "write", "save", "insert", "update", "query", "execute"

---

3. Build the execution path

Trace the visible path from entrypoint to sink.

Example:

Controller / Consumer / Scheduler
→ Service / Orchestrator
→ Context Builder / Mapper
→ Client / Repository / Writer
→ Target sink operation

Capture the main files/classes/methods involved.

---

4. Identify the target surface

List the fields, headers, parameters, columns, or predicates that are directly part of the target.

For payloads, use JSONPath-style paths:

$.shipment.id
$.recipient.email
$.delivery.estimatedDate
headers.X-Correlation-Id

For DB operations, use operation paths:

insert.shipment_notification.order_id
insert.shipment_notification.email_address
update.order_status.status
where.order_id
query.limit

---

5. Build the required data point list

Create a numbered data point list: "D1", "D2", "D3", etc.

Include every data point required to form, route, execute, suppress, or decide anything about the target.

Each data point should have:

- ID
- data point name
- source
- required status
- participation role(s)
- where it is used
- confidence

---

6. Assign participation roles

Use this role vocabulary:

Role| Meaning
"direct_mapping"| copied directly into payload/header/query/DB field
"derived_mapping"| used to calculate a target field
"lookup_key"| used to retrieve other required data
"business_rule"| affects mapping or behavior
"suppression_rule"| decides whether not to send/write/query
"routing"| chooses destination/topic/portal/table/tenant
"validation"| checked before forming/executing target
"metadata"| header, audit, correlation, source info
"defaulting"| fallback/default behavior
"idempotency_or_staleness"| duplicate/stale behavior
"config_secret_constant"| config, token, static value, enum map
"generated"| generated ID, timestamp, sequence, runtime value
"local_state"| retry state, previous send/write state, local persistence state

A data point can have multiple roles.

---

7. Produce key lineage chains

Use arrow chains for important or non-obvious lineage.

Format:

[target field / operation / decision]
← [mapping expression or decision condition]
← [intermediate object/value]
← [lookup/repository/client/config]
← [earliest visible source]

Only include chains for fields or decisions that are:

- derived
- lookup-based
- conditional
- business-rule-related
- suppression/no-op-related
- routing-related
- low confidence
- important enough that a table row is not sufficient

Do not create long chains for obvious direct fields unless useful.

---

Required Report Format

Data Lineage Report: [Target]

1. Target Summary

Item| Value
Target| 
Target type| payload / message / DB query / DB write / file / email / webhook / other
Sink point| 
Main mapper/builder/repository/client| 
Entrypoint| 
Repos/modules inspected| 
Overall confidence| High / Medium / Low
Main unknowns| 

2. Execution Path

[Entrypoint]
→ [Service/orchestrator]
→ [Mapper/context builder]
→ [Client/repository/writer]
→ [Sink operation]

3. Target Surface Map

For payload/message/file/email/webhook targets:

Target path| Mapping/value expression| Data points used| Notes
"$.shipment.id"| "shipment.getId()"| "D1"| direct
"$.recipient.email"| "customer.getEmailAddress()"| "D2", "D3"| "D2" used to lookup customer; "D3" mapped
"$.delivery.estimatedDate"| "deliveryDateCalculator.estimate(order, warehouse)"| "D4", "D5", "D6"| derived
"headers.X-Correlation-Id"| "requestContext.getCorrelationId()"| "D7"| metadata

For DB query/write targets:

Target path| Mapping/value expression| Data points used| Notes
"insert.shipment_notification.order_id"| "order.getId()"| "D1"| insert column
"insert.shipment_notification.email_address"| "customer.getEmailAddress()"| "D2", "D3"| lookup result
"insert.shipment_notification.created_at"| "clock.instant()"| "D8"| generated timestamp
"where.order_id"| "order.getId()"| "D1"| query/update predicate

4. Required Data Points

ID| Data point| Source| Required?| Participates as| Used by
"D1"| "orderId"| inbound shipment event| yes| direct_mapping, lookup_key| "$.shipment.id", order lookup, DB "where.order_id"
"D2"| "customerId"| order record| yes| lookup_key| customer lookup
"D3"| "customer.emailAddress"| customer profile lookup| yes| direct_mapping, validation| "$.recipient.email"
"D4"| "order.shippingSpeed"| order record| yes| derived_mapping, business_rule| delivery date calculation
"D5"| "warehouse.timeZone"| warehouse lookup| conditional| derived_mapping| delivery date calculation
"D6"| "carrier.transitDays"| carrier service lookup| conditional| derived_mapping| delivery date calculation
"D7"| "correlationId"| request context / inbound event metadata| yes| metadata| "headers.X-Correlation-Id"
"D8"| "createdAt"| generated by "clock.instant()"| yes| generated, metadata| DB "created_at"
"D9"| "notificationPreferences.emailOptIn"| customer preference lookup| yes| business_rule, suppression_rule| send/no-send decision
"D10"| "shipmentNotificationEnabled"| application config / feature flag| yes| config_secret_constant, suppression_rule| send/no-send decision

5. Participation Map

Data point| Direct mapping| Derived mapping| Lookup key| Business rule| Suppression/no-op| Routing| Metadata| Config/constant/state
"D1 orderId"| yes| no| yes| no| no| no| no| no
"D2 customerId"| no| no| yes| no| no| no| no| no
"D3 customer.emailAddress"| yes| no| no| no| no| no| no| no
"D4 order.shippingSpeed"| no| yes| no| yes| no| no| no| no
"D5 warehouse.timeZone"| no| yes| no| no| no| no| no| no
"D6 carrier.transitDays"| no| yes| no| no| no| no| no| no
"D7 correlationId"| no| no| no| no| no| no| yes| no
"D8 createdAt"| no| no| no| no| no| no| yes| yes
"D9 notificationPreferences.emailOptIn"| no| no| no| yes| yes| no| no| no
"D10 shipmentNotificationEnabled"| no| no| no| yes| yes| no| no| yes

6. Key Lineage Chains

Only include important or non-obvious chains.

$.recipient.email
← customer.emailAddress
← CustomerClient.getCustomer(order.customerId)
← D2 customerId from order record
← OrderRepository.findById(D1 orderId)
← D1 orderId from inbound shipment event

$.delivery.estimatedDate
← deliveryDateCalculator.estimate(order.shippingSpeed, warehouse.timeZone, carrier.transitDays)
← D4 order.shippingSpeed from order record
← D5 warehouse.timeZone from warehouse lookup
← D6 carrier.transitDays from carrier service lookup

send/no-send decision
← shipmentNotificationEnabled == true && customer.emailOptIn == true
← D10 shipmentNotificationEnabled from application config / feature flag
← D9 notificationPreferences.emailOptIn from customer preference lookup

insert.shipment_notification.created_at
← clock.instant()
← D8 generated runtime timestamp

7. Unknowns / Low Confidence

Area| Why uncertain| Suggested follow-up
| | 

8. Notes / Observations

Include concise notes about:

- dynamic maps
- generated code
- framework magic
- cross-repo dependencies
- external systems not visible
- default behavior not proven
- tests or fixtures that should be checked

---

Evidence and confidence rules

Every major claim should be grounded in source evidence.

Use confidence levels:

- "High" — direct assignment, getter, setter, builder, constructor, or clearly traced method parameter
- "Medium" — traced through helper/mapper/service with some inference
- "Low" — dynamic maps, generated code, reflection, cross-repo gap, external dependency, framework wiring
- "Unknown" — source cannot be determined from visible code

Do not claim full lineage if evidence is incomplete.

---

What not to do

- Do not produce a giant architecture report.
- Do not design a new event unless explicitly asked.
- Do not ignore data points that only affect decisions.
- Do not ignore no-send/no-op logic.
- Do not ignore lookup keys.
- Do not ignore headers/metadata.
- Do not ignore DB query predicates or DB write columns.
- Do not assume output field names match source field names.
- Do not hide uncertainty.
- Do not over-explain obvious direct mappings.



---
lineage.prompt.md

Use the Data Lineage Investigator skill.

Target:

"[describe the target payload, message, DB query/write, sender method, repository method, endpoint, topic, DTO, or sink operation]"

Relevant repos/modules currently open:

- "[repo/module 1]"
- "[repo/module 2]"

Produce a concise lineage report.

Focus on:

- target summary
- execution path
- target surface map
- required data points
- participation map
- key arrow lineage chains
- unknowns and low-confidence areas

Capture data points used for:

- direct mapping
- derived mapping
- lookup keys
- business rules
- suppression/no-op decisions
- routing
- validation
- metadata/headers/audit fields
- DB query predicates
- DB write columns
- config/constants/secrets/local state

Use workspace search and symbol navigation first.

Use ast-grep when helpful to find structural patterns such as:

- sink calls
- builder methods
- setters
- constructors
- "Map.put(...)"
- header mutations
- repository calls
- SQL execution
- query parameter binding
- entity field assignment
- "if" / "switch" / "filter" decision logic
- validation branches
- suppression/no-op logic

Treat ast-grep results as candidates. Verify by reading the source.

Do not design a new event or input contract unless explicitly asked.
