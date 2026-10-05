#!/usr/bin/env python3
from pathlib import Path
import json
try:
    import yaml
except ImportError as e:
    raise SystemExit("PyYAML required for validation") from e
root=Path(__file__).resolve().parents[1]
required=["AGENTS.md","MASTER_CODEX_PROMPT.md","HERMES_START_PROMPT.md","BUILD_STATE.json","10_TICKETS.yaml","15_HERMES_ORCHESTRATOR.md","17_M_MINUS_1_PREFLIGHT.md"]
missing=[x for x in required if not (root/x).exists()]
if missing: raise SystemExit(f"missing required files: {missing}")
state=json.loads((root/"BUILD_STATE.json").read_text())
backlog=yaml.safe_load((root/"10_TICKETS.yaml").read_text())
ids=[t["id"] for t in backlog["tickets"]]
if len(ids)!=len(set(ids)): raise SystemExit("duplicate ticket ids")
by={t["id"]:t for t in backlog["tickets"]}
for t in backlog["tickets"]:
    for d in t.get("depends_on",[]):
        if d not in by: raise SystemExit(f"unknown dependency {d} in {t['id']}")
if state["current_ticket"] not in by: raise SystemExit("BUILD_STATE current_ticket missing from backlog")
if by["CLIP-001"].get("depends_on") != ["CLIP-P006"]: raise SystemExit("CLIP-001 must depend on M-1 completion")
print(f"OK: {len(ids)} tickets; current={state['current_ticket']}; spec={state['spec_version']}")
