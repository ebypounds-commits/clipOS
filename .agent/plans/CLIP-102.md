# CLIP-102 — Freeze v1 release and post-launch backlog (M24 closer, FINAL)

## Goal

Freeze v1.0.0 as a validated artefact and convert the deferred work into an
ordered backlog. The freeze commits the terminal build state: 108/108
tickets, every milestone M0..M24 complete, `status=M24_COMPLETE`.

## Deliverables

| Path | Purpose |
| --- | --- |
| `release/v1.manifest.json` | The frozen release manifest |
| `scripts/release_freeze.py` | Stdlib-only freeze validator (F1..F7) |
| `services/api/tests/test_release_freeze.py` | 12 tests incl. red-team mutations |
| `docs/BACKLOG.md` | Post-launch backlog (P0/P1/P2) |
| `services/api/pyproject.toml` | 0.1.0 → **1.0.0** (surfaced by `GET /health`) |
| `scripts/validate_state.py`, `scripts/validate_handoff.py` | Terminal-state support |

## The manifest's honesty rule

`deferred_to_post_launch.human_gates` must equal
`requires.human_gates_documented` — check F6 fails otherwise. v1 explicitly
does **not** include: production secrets, real YouTube/TikTok/Meta accounts,
or the first public post. All five gates are owner acts; the system operates
in DRAFT with public publishing disabled.

## Terminal build state

Completing 108/108 required coordinated validator support (both validators
previously assumed an in-progress build):

- `current_ticket` / `current_milestone` become the sentinel **`NONE`**;
- `validate_state.py` exempts `NONE` from the must-exist-in-YAML check;
- `validate_handoff.py` accepts `NONE` only while zero backlog tickets
  remain outside `completed_tickets` (guarded against premature use);
- `status` becomes `M24_COMPLETE` (matches the existing status pattern —
  no schema change needed).

The autonomy evidence harness agrees with the terminal state (its E4
"current milestone not already completed" check reads the sentinel as
coherent), and `release_freeze` F3/F4 pin the ledger and milestones.

## Bugs caught while building it

- **Validator read the wrong manifest**: `validate()` used a module-level
  constant path, so `--repo` mutations were invisible — caught by the
  mutation test deleting a manifest key and seeing no failure. Fixed: the
  manifest is read from the repo under validation.
- **BOM in pyproject.toml**: a PowerShell write added a UTF-8 BOM; pytest's
  TOML parser refused the file. Stripped; a recurring footgun worth naming.

## Verification

- 12 new tests; 1837 passed CI-env (94.05% cov), 1 skipped.
- `release_freeze` → VERDICT PASS on the real repository (F1..F7 all INFO).
- `autonomy_evidence` → PASS on the frozen record; `state_audit` → 108/108
  covered (CLIP-102 pinned to evidence ref `v1.0.0` — the release itself,
  since the merge SHA cannot exist before the merge).
- ruff clean; mypy clean (112 files); secret_scan clean; architecture_guard
  6/6; deploy_topology_check PASS.
