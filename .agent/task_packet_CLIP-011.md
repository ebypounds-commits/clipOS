# Codex Task Packet — CLIP-011

## Lease
- ticket_id: CLIP-011
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-011
- worktree: /srv/clipos/worktrees/CLIP-011
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement S3-compatible object storage and signed URLs (M2): an `ObjectStorage` Protocol with an `S3ObjectStorage` implementation (boto3 against any S3-compatible endpoint — MinIO locally per 02, R2/S3 in prod), env-driven configuration (credentials are REFERENCES via environment, never code — 07), presigned GET/PUT URLs with TTL (07 "signed object URLs"), idempotent put/get/delete, upload policy enforcement (content-type allowlist + max size, foundation-level), and a MinIO-backed integration test gated on `STORAGE_ENDPOINT` that the CI test job activates (MinIO already runs in the compose stack).

## Environment constraints
- Sandbox: NO network, NO docker. **venv EXISTS with boto3 pre-installed** — self-verify: `.venv/bin/pytest -q services/api`, `.venv/bin/ruff format services/api`, `.venv/bin/ruff check services/api`, `.venv/bin/mypy services/api/clipos_api`. STORAGE-gated tests SKIP in-sandbox (no MinIO) — correct and honest; orchestrator runs them against real MinIO.
- NO new deps beyond boto3 (already in venv — ADD it to pyproject dependencies).

## Required reading
02_SYSTEM_ARCHITECTURE.md (object storage: MinIO local, S3-compatible prod, signed URLs), 07_SECURITY_GOVERNANCE_RIGHTS.md (signed object URLs; secrets manager references only — creds via env; MIME/file-type validation), 01_PRODUCT_REQUIREMENTS.md (media persistence, resumable uploads note), docker-compose.dev.yml (minio service: endpoint port, root credentials, for CI env), services/api/clipos_api/{repository.py (settings/engine pattern), infrastructure.py (health)}, .agent/RUNNER_CONVENTIONS.md, docs/development.md (persistence section to extend).

## Scope boundaries
- No HTTP routes (uploads wire up in pipeline tickets); no Temporal/multipart resumable upload logic yet (document as future work); no CDN logic.
- Do NOT modify CLIP-006..010 modules (import/reuse patterns only).
- Never hardcode credentials/endpoints — env only (STORAGE_ENDPOINT, STORAGE_ACCESS_KEY, STORAGE_SECRET_KEY, STORAGE_BUCKET, optional STORAGE_REGION, STORAGE_PRESIGN_TTL_SECONDS default 3600).

## Deliverables (exact paths — orchestrator verifies)
1. `services/api/pyproject.toml` — add boto3>=1.34 (floor from installed version).
2. `services/api/clipos_api/storage.py` — stdlib + boto3 + pydantic:
   - `StorageConfig` (frozen, from_env() reading the STORAGE_* vars; endpoint/key/secret/bucket required, region/presign_ttl optional with defaults; secret never repr'd — `__repr__`/`__str__` redacts the secret key).
   - `PutPolicy` (allowed content types frozenset default e.g. image/png, image/jpeg, image/webp, video/mp4, video/webm, application/pdf; max_bytes default 100MB) with `validate(key, content_type, size)` raising ValueError on violation.
   - `ObjectStorage` Protocol: put, get, delete, exists, presign_get, presign_put, ensure_bucket.
   - `S3ObjectStorage(config, client=None)` — client injectable for tests (default boto3.client("s3", endpoint_url=..., aws_access_key_id=..., aws_secret_access_key=..., region_name=...)); `put(key, data|fileobj, content_type, policy=None)` validates policy then uploads (idempotent by key: second put overwrites, no error); `get(key) -> bytes` (KeyError-ish domain error on missing); `delete` idempotent (missing = no-op success); `presign_get(key, ttl=None)`/`presign_put(key, content_type=None, ttl=None)` return URL strings (client-generated presigned, method GET/PUT); `ensure_bucket()` idempotent (BucketAlreadyOwnedByYou tolerated).
   - Domain errors: `ObjectNotFound`, `PolicyViolation` (subclass ValueError) — no botobotocore exception leakage in the API surface (wrap: catch ClientError, map 404/NoSuchKey → ObjectNotFound; document mapping).
   - Audit: `storage.object_put`/`storage.object_deleted` events through clipos_api.audit (include key + size + content_type, NEVER data or credentials).
3. `services/api/tests/test_storage.py` — NO network tests using an injected FAKE S3 client (simple stub recording calls + emulating presign URLs as deterministic strings): config from_env + secret redaction in repr; policy accept/reject matrix (bad type, oversize); put validates then calls client with expected bucket/key/contenttype; second put = overwrite not error; get missing → ObjectNotFound; delete idempotent; presign_get/put produce URLs with expected params (X-Amz-Expires=ttl) via stub; ensure_bucket called once then tolerated (idempotent); audit events recorded for put/delete (via audit.read_events) with no secret present in any event.
4. `services/api/tests/test_storage_integration.py` — `@pytest.mark.skipif(not os.environ.get("STORAGE_ENDPOINT"))`: real roundtrip against MinIO: ensure_bucket, put bytes, exists, get roundtrip equal, presign_get URL obtainable, delete, exists False. Use distinct keys per run (uuid) for idempotency; tolerate re-runs.
5. CI `.github/workflows/baseline-ci.yml` test job: add STORAGE_* envs to the pytest step (extract endpoint/keys from docker-compose.dev.yml minio service — exact env names as compose defines; bucket e.g. clipos-test) and a tiny pre-step (bash -c) creating the bucket via the tests' ensure_bucket or `python -c` — prefer letting `ensure_bucket()` in the test handle it, so NO extra step; keep report needing all four; YAML valid.
6. `docs/development.md` — "Object storage" paragraph (env vars table, presign usage, MinIO local, future: multipart/resumable).
7. `.agent/dryrun/CLIP-011_completion_report.json` with REAL gate totals.

## Acceptance (orchestrator re-runs outside sandbox — incl. REAL MinIO)
- `.venv/bin/pytest -q services/api` green in sandbox (integration SKIPs honest); orchestrator adds STORAGE_* against compose MinIO: integration green, full suite green.
- ruff format --check + check + mypy clean; compileall green.
- validate_handoff, validate_state, state_audit verify, architecture_guard, validate_packet, secret_scan, workflow YAML — green; CI 4 jobs green after merge.
- No credential literals anywhere (grep your module for AKIA/asdf/secret values — only env reads); no boto3 ClientError escaping storage.py's API.
- Existing tests (137) untouched and passing.

## Allowed scope
paths: services/api/{pyproject.toml,clipos_api/storage.py,tests/test_storage.py,tests/test_storage_integration.py}, .github/workflows/baseline-ci.yml, docs/development.md, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; hardcoded credentials; routes; modifying CLIP-006..010 modules; fabricating results.

## Required gates (runnable here — RUN THEM)
- .venv/bin/pytest -q services/api (green; STORAGE tests SKIP)
- .venv/bin/ruff format services/api && .venv/bin/ruff check services/api && .venv/bin/ruff format --check services/api
- .venv/bin/mypy services/api/clipos_api
- python3 -m compileall services/api
- python3 scripts/validate_handoff.py; python3 scripts/validate_state.py; python3 scripts/state_audit.py verify; python3 scripts/architecture_guard.py; python3 scripts/validate_packet.py .agent/task_packet_CLIP-011.md
- bash scripts/secret_scan.sh; yaml parse of baseline-ci.yml

## Completion output
Completion report + concise summary; no fabrication.
