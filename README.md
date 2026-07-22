# migration-poc

An artifact-definition migration proof of concept for:

`Discover -> Assess -> Plan -> Approve -> Deploy -> Validate`

The tool migrates orchestration artifacts and their definitions—not customer
data. It discovers complete ADF definitions, generates deterministic Microsoft
Fabric artifact packages, deploys those definitions to an in-process mock, and
validates structural preservation. No real Azure or Fabric API calls are made.

Structural validation covers mappings, nested activities, transformations and
order, parameters, variables, expressions, dependencies, schedules,
connections, conversion reporting, schemas, manifests, and deployed digests.
Row counts, totals, and other business metrics are optional runtime checks and
never determine structural migration status.
