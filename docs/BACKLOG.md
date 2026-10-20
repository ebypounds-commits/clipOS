# ClipOS post-launch backlog (v1.0.0 freeze)

What v1 deliberately does **not** include, and what comes next. The freeze
manifest (`release/v1.manifest.json`) lists the human gates deferred to
post-launch; this backlog turns the rest of the known work into ordered,
ownable items. Items are grouped by area; priority is P0 (before any
production use) → P1 (first production month) → P2 (hardening).

## P0 — before any production use

### Real platform accounts (human gates)
- Execute `production_secrets`: import production secrets from the server
  secret manager into `deploy/.env.production`; complete the TLS issuance
  and Caddy bring-up (`docs/PRODUCTION_BOOTSTRAP.md` §6).
- Connect the first real YouTube/TikTok/Meta account per
  `13_HUMAN_GATES_AND_SECRETS.md`; record each private test via
  `POST /publishing/private-tests`, then grant the owner approval via
  `POST /publishing/owner-approvals`.
- Execute `first_public_post` per account — owner act only; the CLIP-099
  gate refuses public publishing until both the private test and the
  approval exist.

### Provider credentials (execution, not code)
- Export provider API keys on the deployment host
  (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
  `DEEPSEEK_API_KEY`, `DEEPINFRA_API_KEY`) and restart the API; the model
  registry routes are already seeded and report `degraded` until keys exist.
- Re-run the dispatch probe (`scripts/test_llm_dispatch.py`) and flip route
  health to `ok`.

### CI/ops wiring (small, high value)
- Wire `scripts/schema_drift.py` and `scripts/autonomy_evidence.py` into the
  CI `test`/`validate` jobs (both are proven; neither is enforced yet).
- Add a scheduled job for backup freshness + restore-test cadence so
  `GET /backups/status` is driven by Temporal rather than by hand.

## P1 — first production month

### Deployment
- Image build + registry publish pipeline (`ghcr.io` digests) so
  `CLIPOS_API_IMAGE` / `CLIPOS_WEB_IMAGE` pin real digests instead of
  placeholders; exercise canary → promote → rollback against a real
  registry once.
- Production bootstrap end-to-end run on the production host with real
  secrets, then `scripts/security_qualification.py` as the go-live gate.
- Stand up the `edge` TLS path on the production host and retire any
  direct-port access.

### Operations
- First documented disaster-recovery drill (runbook §Backup policy: monthly
  initially) recorded via `POST /slo/drills` with evidence refs.
- Real SLO measurements feeding `POST /slo/objectives/{id}/evaluate` (v1
  declares objectives and the evaluator; production traffic supplies the
  observations).
- Alert routing: connect the runbook's alert conditions to the operator's
  notification destination (SMTP or equivalent) — currently documented but
  unwired.

### Platform integrations
- Real adapter certification per platform (upload, status, takedown) beyond
  the Mock adapter, with each platform's quota/retry behaviour encoded in
  its adapter policy.
- OAuth refresh flows exercised against real accounts; expiry monitoring
  already exists (`POST /monitors/check` credential expiry) — feed it real
  credentials.

## P2 — hardening

- Multi-org isolation review: every table is org-scoped; add a dedicated
  cross-tenant access test suite.
- Replace the stdlib backup envelope (`sha256-ctr + HMAC-SHA256`) with a
  vetted AEAD (AES-GCM/ChaCha20-Poly1305) once a crypto dependency is
  acceptable; version-1 envelopes remain readable by design.
- Performance passes on the render path (ffmpeg dominates; see the E2E
  timing evidence) and on the LLM gateway under concurrent load.
- Expand the review UI review-board flows (bulk decisions, keyboard-only
  operation) once real candidates flow through.
- Dependency audit cadence: `pip-audit`/SBOM lanes are defined; schedule
  them and act on findings.

## Done in v1 (for contrast)

Autonomous build M-1..M24: identity/RBAC, agent registry and lifecycle,
research→produce→render pipeline, publishing governance with the owner
gate, finance and budgets, evaluations, incidents/remediation/monitors,
threat model and injection guard, security qualification, hardened
staging/production topologies, canary/rollback, encrypted backups with
restore verification, bootstrap/secrets/TLS declarations, private-mode E2E
qualification, SLO objectives, autonomy evidence, and this backlog.
