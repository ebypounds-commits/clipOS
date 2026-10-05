# ClipOS ExecPlan Contract

Use an ExecPlan for every non-trivial ticket, migration, cross-domain change, infrastructure change, security-sensitive change, or task expected to require more than one implementation/test cycle.

## Required sections
- Ticket and objective
- Current repository facts
- Scope / non-scope
- Dependencies and assumptions
- Files/domains expected to change
- Security, rights, budget and external-side-effect impact
- Implementation steps
- Test/gate plan
- Rollback / recovery plan when applicable
- Completion evidence
- Remaining risks / follow-ups

Store active plans in `.agent/plans/<ticket>.md`. Keep plans current as facts change. The plan is not a substitute for `BUILD_STATE.json` or the ticket state.
