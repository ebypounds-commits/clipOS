# CLIP-099 — Owner-gated public publishing (M24) — HUMAN GATE

## Gate status

`human_gate: true` — "Owner approval required before first public posting."
Per the operator's direction this ticket was built and tested **against the
Mock adapter only**: the guard machinery is implemented and qualified, and no
public post was made to any real platform account. Real posting remains the
owner's act.

## Goal

`07_SECURITY_GOVERNANCE_RIGHTS.md`: "New platform account starts
`PRIVATE_TEST`. Public mode requires explicit owner approval after
successful private test." `00_README_FIRST.md`: "owner gate before first
public posting per real platform account." The gate did not exist:
`GUARDED_PUBLIC` and `AUTONOMOUS_PUBLIC` were policy modes with no approval
requirement at all. This ticket adds it.

## Deliverables

| Path | Purpose |
| --- | --- |
| `services/api/clipos_api/public_publishing_gate.py` | Approval/private-test records, pure gate, routes |
| `services/api/migrations/versions/0072_public_publishing_gate.py` | `owner_approvals`, `private_tests` |
| `services/api/tests/test_public_publishing_gate.py` | 34 tests |
| `services/api/clipos_api/publishing.py` (modified) | Evaluator + route consult the gate |
| `services/api/tests/test_publishing.py` (modified) | Existing PUBLIC tests updated for the tightened contract |

## The gate

An owner approval is a **record**, not a config flag. Public publishing
requires **all** of:

1. a granted owner approval for that exact `(org, platform, account_ref)`;
2. that approval not revoked and not expired;
3. a recorded successful private test for that same account.

Every missing precondition has its own reason code — `no_owner_approval`,
`owner_approval_revoked`, `owner_approval_expired`, `no_successful_private_test`
— so an operator sees precisely what to do next rather than a bare denial.
The default is closed: no approval means DENIED, and a private/unlisted
visibility never consults the gate.

The gate is **per real platform account**, so approving one channel cannot
unlock another. A re-granted approval supersedes an earlier revoked one.

## Where it is enforced

`evaluate_publish_policy` gained `account_approved: bool = False`, so a
caller that does not consult the gate **cannot publish publicly by omission**
— the fail-closed default is denial. The route resolves the decision from
the gate store for PUBLIC visibility and passes it in, which keeps the
policy function pure and free of persistence.

## Blast radius on existing tests

Four pre-existing tests published PUBLIC with `public_enabled=True` and no
approval; they now correctly receive DENIED. Each was updated to say what it
actually tests — the pure ones pass `account_approved=True` to isolate the
`public_enabled` rule, and the route roundtrip now grants an approval. Two
new tests pin the tightened contract (`test_public_denied_without_owner_approval`,
`test_private_visibility_ignores_the_owner_gate`).

The CLIP-098 harness patches `evaluate_publish_policy`, so its three
deliberate-regression patches were updated for the new parameter; the
CLIP-093 security qualification still passes unchanged.

## Verification

- 34 new tests; 1765 passed CI-env (94.01% cov), 1 skipped.
- ruff format + check clean; mypy clean (110 files).
- Migration 0072 applied on Postgres (0071 -> 0072).
- secret_scan clean; architecture_guard 6/6; deploy_topology_check PASS;
  state_audit 104/104.
- CLIP-093 and CLIP-098 qualification harnesses both still pass.

## What is left for the owner

Nothing in code. To enable real public posting the owner must, per real
account: run a private test, record it via `POST /publishing/private-tests`,
then grant approval via `POST /publishing/owner-approvals` — and separately
flip the deployment's `public_publishing` flag and the policy's
`public_enabled`. Until then `first_public_post` stays false.
