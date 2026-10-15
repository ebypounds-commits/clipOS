# ExecPlan — CLIP-091 Harden prompt injection / tool authorization

## Ticket and objective
CLIP-091 (M22): make the `07_SECURITY_GOVERNANCE_RIGHTS.md`
prompt-injection defenses executable — detect instruction-shaped spans
in untrusted content, neutralize or quarantine them, ensure untrusted
content can never grant a tool/capability, and audit every decision
without storing the hostile text.

## Current repository facts
- Head at plan time: `cb16c97` (CLIP-090 state advance, 96/102). CI green.
- Worktree `/srv/clipos/worktrees/CLIP-091`, branch `agent/CLIP-091`,
  venv ready, lease active. Next free migration: **0068** (0067 -> 0068).
- Existing coverage: `tool_policy.py` (capability/sandbox/rate gate) and
  `mcp_boundary.py` (plugin boundary). Neither inspects untrusted TEXT,
  which is this ticket's gap.

## Scope / non-scope
Scope: `clipos_api/injection_guard.py`, migration 0068, tests, main.py
wiring, test_migrations, docs/development.md.
Non-scope: LLM-side prompt templating changes, security test suite
(CLIP-092), qualification run (CLIP-093).

## Implementation
1. Pure scanner: six pattern classes (instruction override, role
   assumption, tool-grant attempt, exfiltration request,
   delimiter escape, hidden characters) with per-finding severity,
   counts, and truncation at 20k chars.
2. Pure neutralizer: label matched spans right-to-left, strip
   zero-width/bidi characters, count per kind.
3. Boundary: fail-closed wrapper (scanner error -> SCANNER_ERROR,
   never raises), quarantine at/above a tunable severity threshold,
   else NEUTRALIZED/ALLOWED with safe text.
4. `authorize_tool_from_content`: untrusted sources deny with
   `content_cannot_grant_tools`; trusted set OPERATOR/POLICY/SYSTEM.
5. injection_events (append-only, content-free: counts + decision +
   reason + severity + char count), idempotent with 409 conflict.
6. Routes: POST /security/injection/scan, POST /security/injection/
   enforce, GET /security/injection/events, POST
   /security/tool-authorization.

## Test/gate plan
~22 tests incl. benign false-positive check, all six detector classes,
neutralization round-trip, threshold tuning, scanner-failure
fail-closed, tool-authorization matrix, and an audit assertion that the
hostile text never reaches the log. Gates: ruff/mypy, pytest SQLite +
CI-env, PG 0067 -> 0068, OpenAPI/secret_scan/arch_guard/state_audit.

## Landing
Commit -> merge --no-ff -> state advance (96->97, current -> CLIP-092)
-> push -> CI green -> release lease, remove worktree, local pull.