# Codex Task Packet — CLIP-012

## Lease
- ticket_id: CLIP-012
- orchestrator_run_id: hermes-clipos-m1-001
- branch: agent/CLIP-012
- worktree: /srv/clipos/worktrees/CLIP-012
- lease_expires_at: +60 minutes from dispatch

## Objective
Implement Redis caching and rate-limit primitives (M2): a `KeyValueCache` Protocol with `RedisCache` (env-driven URL, injectable client) and an in-memory fallback (injected clock, no sleeping), plus a `RateLimiter` Protocol with a fixed-window `RedisRateLimiter` (INCR+EXPIRE, atomic-ish semantics documented) and an in-memory twin, returning structured results (allowed, remaining, retry_after) per 02 ("Cache/rate-limits: Redis"). Include a `CACHE_REDIS_URL`-gated integration test wired into CI (compose redis already runs in the test job).

## Environment constraints
- Sandbox: NO network, NO docker. **venv EXISTS with redis pre-installed** — self-verify: `.venv/bin/pytest -q services/api`, `.venv/bin/ruff format services/api`, `.venv/bin/ruff check services/api`, `.venv/bin/mypy services/api/clipos_api`. CACHE-gated tests SKIP in-sandbox — honest; orchestrator runs them against real Redis.
- NO new deps beyond `redis` (already in venv — ADD it to pyproject dependencies).

## Required reading
02_SYSTEM_ARCHITECTURE.md (Redis for cache/rate-limits), 07_SECURITY_GOVERNANCE_RIGHTS.md (rate limits/least privilege context), 01_PRODUCT_REQUIREMENTS.md (security/maintainability notes), docker-compose.dev.yml (redis service: port mapping, password if any — exact env names for CI), services/api/clipos_api/{storage.py (config-from-env + injectable-client pattern to mirror), infrastructure.py (health)}, .agent/RUNNER_CONVENTIONS.md, docs/development.md.

## Scope boundaries
- No HTTP middleware yet (rate-limit MIDDLEWARE wiring happens in API tickets) — primitives only; document how a route will call `allow()`.
- No distributed Lua token bucket yet (fixed window foundation; sliding window = future work, documented).
- Do NOT modify CLIP-006..011 modules; never hardcode redis URLs/passwords (env only: CACHE_REDIS_URL, optional CACHE_KEY_PREFIX default "clipos:", CACHE_DEFAULT_TTL_SECONDS default 300, RATE_LIMIT_* defaults documented).

## Deliverables (exact paths — orchestrator verifies)
1. `services/api/pyproject.toml` — add `redis>=5.0` (floor from installed).
2. `services/api/clipos_api/cache.py` — `CacheConfig` frozen from_env() (url, prefix, default ttl; password embedded in URL never repr'd — redact), `KeyValueCache` Protocol (get/set/delete/exists/incr, all namespaced by prefix), `RedisCache(config, client=None)` (client injectable; default redis.Redis.from_url; domain errors wrap redis exceptions — `CacheUnavailable`), `InMemoryCache(clock=None)` same Protocol (dict + expiry via injected clock).
3. `services/api/clipos_api/rate_limit.py` — `RateLimitPolicy(limit: int, window_seconds: int)` frozen with validation, `RateLimitResult` frozen (allowed, remaining, retry_after_seconds, reset_at), `RateLimiter` Protocol `allow(key) -> RateLimitResult`, `RedisRateLimiter(client, policy, clock=None)` using INCR on `{prefix}rl:{key}:{window_index}` + EXPIRE set only when INCR returns 1 (document the crash-window edge), window_index = int(now // window) from injected clock (redis EXPIRE uses window+1 slack), `InMemoryRateLimiter(policy, clock)` twin. Both consistent in behavior — SAME key/policy sequence yields same allow/deny pattern (contract test).
4. `services/api/tests/test_cache_rate.py` — NO network: fake redis client stub (records incr/expire/get/set calls, returns scripted values) + InMemory versions: config redaction in repr; prefix namespacing on every op; set/get roundtrip + ttl expiry via fake clock (in-memory) and EXPIRE-called-with-correct-seconds (redis stub); CacheUnavailable wraps stub-raised redis error; rate limiter allows exactly `limit` calls per window then denies with retry_after>0; window rollover resets (advance fake clock); contract: parametrize allow/deny sequence equality between InMemory and Redis(stub) for identical policies; validation errors (limit<1, window<1).
5. `services/api/tests/test_cache_integration.py` — `@pytest.mark.skipif(not os.environ.get("CACHE_REDIS_URL"))`: real redis roundtrip: set/get with ttl (assert TTL>0 via client ttl), delete, incr; rate limiter: allow limit times, deny next, advance real time? NO sleeping — use window_seconds=1 and time.sleep(1.1) ONCE max in integration (acceptable in gated test) or set window tiny and only assert deny within window (skip reset to avoid sleep — prefer: assert allow×limit then deny, no reset wait); distinct keys per run (uuid).
6. CI `.github/workflows/baseline-ci.yml` test job: add `CACHE_REDIS_URL` env to pytest step (extract redis URL shape from compose — port mapping host port and password if present; e.g. redis://localhost:6379/0 — READ compose for truth); keep report needing all four; YAML valid.
7. `docs/development.md` — "Cache & rate limiting" paragraph (env vars, primitives vs future middleware, fixed-window caveat).
8. `.agent/dryrun/CLIP-012_completion_report.json` with REAL gate totals.

## Acceptance (orchestrator re-runs outside sandbox — incl. REAL Redis)
- `.venv/bin/pytest -q services/api` green in sandbox (CACHE tests SKIP honest); orchestrator with CACHE_REDIS_URL against compose redis: integration green, full suite green.
- ruff format --check + check + mypy clean; compileall green.
- validate_handoff, validate_state, state_audit verify, architecture_guard, validate_packet, secret_scan, workflow YAML — green; CI 4 jobs green after merge.
- No credential literals (env reads only); existing tests untouched and passing.

## Allowed scope
paths: services/api/{pyproject.toml,clipos_api/cache.py,clipos_api/rate_limit.py,tests/test_cache_rate.py,tests/test_cache_integration.py}, .github/workflows/baseline-ci.yml, docs/development.md, .agent/**
network: none in-session
## Forbidden actions
push/merge; touching main/10_TICKETS.yaml/BUILD_STATE.json/docs/reports/*; hardcoded redis URLs/passwords; routes/middleware; modifying CLIP-006..011 modules; fabricating results.

## Required gates (runnable here — RUN THEM)
- .venv/bin/pytest -q services/api (green; CACHE tests SKIP)
- .venv/bin/ruff format services/api && .venv/bin/ruff check --fix services/api && .venv/bin/ruff check services/api && .venv/bin/ruff format --check services/api
- .venv/bin/mypy services/api/clipos_api
- python3 -m compileall services/api
- python3 scripts/validate_handoff.py; python3 scripts/validate_state.py; python3 scripts/state_audit.py verify; python3 scripts/architecture_guard.py; python3 scripts/validate_packet.py .agent/task_packet_CLIP-012.md
- bash scripts/secret_scan.sh; yaml parse of baseline-ci.yml

## Completion output
Completion report + concise summary; no fabrication.
