# CLIP-098 — End-to-end qualification in private mode (M24 opener)

## Goal

M24 is *Autonomous Operations Qualification*: end-to-end production
qualification, guarded public mode, SLOs, operator runbook, autonomy
evidence. This ticket opens the milestone with the end-to-end qualification
itself — proving the whole pipeline runs **and** that running it cannot
produce a public post.

## Deliverables

| Path | Purpose |
| --- | --- |
| `scripts/private_mode_qualification.py` | Live end-to-end harness + report |
| `services/api/tests/test_private_mode_qualification.py` | 11 tests |

No migration and no new tables: this is a qualification harness over the
existing surface, in the shape CLIP-093 established for the security
controls.

## Two layers

**1. Pipeline walk.** 28 steps across 8 stages — research → planning →
produce → render → publish → analytics → revenue → experiments. A required
step failing fails the qualification; the report records every step's
observed HTTP status so an operator can see how far the walk got.

**2. Private-mode invariants** (checked independently of the walk):

| ID | Invariant |
| --- | --- |
| P1 | OBSERVE mode denies all publishing |
| P2 | PRIVATE_TEST cannot publish PUBLIC |
| P3 | PUBLIC refused while `public_enabled` is false |
| P4 | Rights gate refuses uncleared content |
| P5 | Kill switch active denies a normally-allowed private post |
| P6 | No secret-shaped material in any observed response |

## What the live run found — and what it did not

The first live run reported three HIGH findings. **All three were harness
defects, not product defects**, and chasing that distinction was the
substance of this ticket:

1. **P3 "PUBLIC succeeded with public_enabled=False"** — looked like a
   real policy bypass. The cause was harness state bleed: a publishing
   policy is unique per `(org, platform)`, and P2 had already registered a
   `public_enabled=True` policy for the same platform, so the earlier
   policy decided the later probe. Each invariant now uses its **own
   platform**. Reading the evaluator on current main confirmed it does
   enforce `public_enabled` → `public_disabled_by_default`.
2. **P1 "-> 404"** — the probe hit a server started days earlier from an
   older build. Restarting from current main gave `DENIED
   (mode_OBSERVE_denies_publishing)`.
3. **Render spec 422** — genuinely a harness bug: `inputs.candidate_id` is
   required and 1..128 chars, so the walk now extracts clip candidates
   first and references a real candidate id.

Also fixed: the per-stage step counts rendered as zero because the
aggregation keyed on `status.lower()` (`"pass"`) while the buckets were
initialised as `passed`/`failed`/`skipped`; and P5 originally probed a
nonexistent `/kill-switches` endpoint, so it now exercises the real
enforcement path (a policy with `kill_switch: True`).

Two scp transfers silently failed during this work, serving stale content
and producing misleading runs. Verified on the server after every transfer
(`grep` for a marker) — the standing discipline in this repo.

## Final live result

```
research 4 | planning 5 | produce 2 | render 2 | publish 3
analytics 3 | revenue 4 | experiments 5
P1..P6 PASS        VERDICT: PASS (28 steps, 0 failed; 6 invariants, 0 failed)
```

## Verification

- 11 tests, including three deliberate-regression tests that patch the
  policy evaluator and prove P1/P3/P5 flip the verdict to FAIL rather than
  silently passing.
- Live run against a freshly started API from current main: VERDICT PASS,
  38 responses scanned with no secret-shaped material.
- 1737 passed CI-env (94.00% cov), 1 skipped.
- ruff format + check clean (API and script); mypy clean (109 files).
- secret_scan clean; architecture_guard 6/6.
