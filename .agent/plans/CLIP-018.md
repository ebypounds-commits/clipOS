# ExecPlan — CLIP-018

## Ticket and objective
Implement the Agent Registry schema and API: immutable agent versions, lifecycle activation, high-risk human gate, model-alias routing data, and operator-visible audit events.

## Current repository facts
- Branch/worktree lease: `agent/CLIP-018` at `/srv/clipos/worktrees/CLIP-018`.
- `BUILD_STATE.json` points to CLIP-018; CLIP-017 is complete.
- The API uses SQLAlchemy 2 declarative models, Alembic create-only migrations, FastAPI dependency wiring, and additive audit logging.

## Scope / non-scope
- In scope: `services/api/clipos_api/agent_registry.py`, migration 0003, tests, task packet, and this plan.
- Out of scope: actual model-provider calls, evaluator execution, RBAC integration beyond the explicit high-risk approval flag, and global build-state advancement.

## Dependencies and assumptions
- CLIP-017 is complete.
- Agent definitions store logical model aliases only.
- Model alias targets are registry data; provider identifiers never enter `AgentVersion`.
- Activation requires explicit approval when the version risk is HIGH.

## Files/domains expected to change
- `services/api/clipos_api/agent_registry.py`
- `services/api/clipos_api/main.py`
- `services/api/migrations/versions/0003_agent_registry.py`
- `services/api/tests/test_agent_registry.py`
- `services/api/tests/test_migrations.py`
- `.agent/plans/CLIP-018.md`, `.agent/task_packet_CLIP-018.md`

## Security, rights, budget and external-side-effect impact
- No external side effects.
- Persistence is idempotent by `(agent_id, version)`; conflicting immutable definitions are rejected.
- Lifecycle transitions are constrained, HIGH-risk ACTIVE activation is denied without an approval flag, and every successful material state change is audited.
- Alias resolution returns database-configured ordered targets and contains no provider identity defaults.

## Implementation steps
1. Add typed agent contracts, lifecycle transition map, SQL records, and `AgentRegistryStore`.
2. Add idempotent registration, legal transitions, high-risk gate, active-version resolution, canary percentage support, and ordered alias resolution.
3. Add FastAPI routes and wire the router in `main.py`.
4. Add migration tables for agent records/versions and model alias targets.
5. Add lifecycle, idempotency, governance, alias-ordering, audit, migration, and route-identity tests.
6. Run all required gates in order and repair failures.

## Test/gate plan
- `ruff format services/api`
- `ruff check services/api`
- `mypy services/api/clipos_api`
- `pytest -q services/api`

## Rollback / recovery plan
The migration is create-only and adds no destructive operation. The module and router are isolated and can be removed by reverting the ticket commit; no data is modified by existing tables.

## Completion evidence
Local gate output, updated task packet checkboxes, and the CLIP-018 completion report for Hermes review.

## Remaining risks / follow-ups
Runtime authorization, evaluator orchestration, actual routing health, and BUILD_STATE advancement remain later orchestration concerns.
