# Security, Governance, Rights & Safety

## Security posture
Assume agents can make mistakes and external content can contain malicious instructions. Treat all retrieved/transcribed/web content as untrusted data, never system instructions.

## Mandatory controls
- RBAC plus capability checks at service boundaries.
- tenant scoping enforced server-side.
- secrets manager references only.
- secure cookies/session rotation/CSRF protections.
- SSRF defenses on URL ingestion.
- MIME sniffing and file-type validation.
- malware scanning where practical.
- media size/duration quotas.
- network egress allowlists for sensitive workers.
- signed object URLs.
- webhook signature validation.
- audit all privileged changes.
- no Docker socket exposed to app/agent containers.
- rootless/non-root containers where possible.
- dependency, secret, and container-image scanning in CI.

## Prompt injection defenses
- separate instructions from untrusted source text;
- never let source text grant tools/permissions;
- high-risk tools require deterministic policy check outside model;
- strip/label embedded instructions in retrieved text;
- tool calls validated against JSON schema and capability policy;
- use allowlists for destinations/operations;
- evaluator or deterministic guard on publication-critical decisions.

## Rights gate
A public publication requires:
- source provenance known;
- current rights record;
- allowed commercial use;
- allowed platform;
- allowed territory if restricted;
- attribution requirements satisfied;
- license not expired/revoked;
- transformation record where relevant;
- policy evaluator pass.

Unclear rights => default deny.

## Content provenance
Every render output stores a manifest of source assets and transformations. Hash/fingerprint originals and outputs. Maintain chain from source -> transcript/segment -> script -> render -> platform variant -> publication.

## Financial governance
Agents may:
- read budgets;
- estimate costs;
- request spend;
- automatically consume approved API/render budgets within ceilings.

Agents may not:
- open bank accounts;
- transfer arbitrary funds;
- change payout destinations;
- sign contracts;
- increase global monthly budget above owner threshold.

## Spend tiers
Configurable defaults:
- micro: automatic inside approved service budget;
- routine: automatic up to per-agent/per-brand ceiling;
- elevated: Governor deterministic policy + optional reviewer;
- material: human approval;
- legal/contractual: human approval always.

## Publishing governance
Global kill switch, organization kill switch, brand kill switch, platform-account kill switch, agent kill switch.

New platform account starts `PRIVATE_TEST`. Public mode requires explicit owner approval after successful private test.

## Destructive actions
Deletion of production records, mass content deletion, account disconnect, credential revocation, database migrations with destructive operations, and rollback past data boundaries require explicit policy and backup validation; material destructive actions require human approval.

## Legal note
ClipOS is infrastructure. It does not determine legal ownership merely by model inference. Rights status is based on documented basis/evidence and configured policy. Legal disputes are escalated, not autonomously adjudicated.
