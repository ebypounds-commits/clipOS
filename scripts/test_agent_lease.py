#!/usr/bin/env python3
"""Test suite for scripts/agent_lease.py (CLIP-P003 evidence).

Covers: fresh acquire, conflict refusal, idempotent owner re-acquire,
release, owner mismatch, expired-lease takeover policy (explicit only),
and lease file schema.
"""
import json
import os
import pathlib
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
LEASE = HERE / "agent_lease.py"
LEASE_DIR = HERE.parent / ".agent" / "leases"
TICKET = "CLIP-TEST-LEASE"

passed = failed = 0


def run(*args):
    return subprocess.run([sys.executable, str(LEASE), *args], capture_output=True, text=True)


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS: {name}")
    else:
        failed += 1
        print(f"FAIL: {name} {detail}")


def cleanup():
    p = LEASE_DIR / f"{TICKET}.lock"
    if p.exists():
        p.unlink()


# 0. clean slate
cleanup()

# 1. fresh acquire
r = run("acquire", TICKET, "--owner", "run-A", "--ttl", "900")
d = json.loads(r.stdout)
check("fresh acquire returns ok", d.get("ok") is True and r.returncode == 0)

# 2. lease file schema
lp = LEASE_DIR / f"{TICKET}.lock"
raw = json.loads(lp.read_text()) if lp.exists() else {}
need = {"ticket", "owner", "created_at", "expires_at", "owner_pid", "state"}
check("lease file has required fields", need.issubset(raw.keys()), f"missing={need - set(raw)}")

# 3. conflicting owner refused while lease live
r = run("acquire", TICKET, "--owner", "run-B")
d = json.loads(r.stdout)
check("conflicting acquire refused", r.returncode == 3 and d.get("error") == "lease_held", f"rc={r.returncode} d={d}")

# 4. same owner re-acquire = idempotent renew
r = run("acquire", TICKET, "--owner", "run-A")
d = json.loads(r.stdout)
check("owner re-acquire idempotent", d.get("ok") is True and r.returncode == 0)

# 5. release by owner
r = run("release", TICKET, "--owner", "run-A")
d = json.loads(r.stdout)
check("release by owner", d.get("ok") is True and json.loads(lp.read_text())["state"] == "released")

# 6. release by non-owner refused
r = run("release", TICKET, "--owner", "run-B")
check("release by non-owner refused", r.returncode == 7 and "owner_mismatch" in r.stdout)

# 7. expired lease: takeover WITHOUT reason refused (explicit decision required)
cleanup()
r = run("acquire", TICKET, "--owner", "run-A", "--ttl", "1")
time.sleep(1.2)  # let it expire
r = run("acquire", TICKET, "--owner", "run-B")
d = json.loads(r.stdout)
check("stale takeover refused without explicit reason",
      r.returncode == 4 and d.get("error") == "stale_lease_requires_explicit_takeover",
      f"rc={r.returncode} d={d}")

# 8. expired takeover WITH reason succeeds and is audited
r = run("acquire", TICKET, "--owner", "run-B", "--steal-reason", "lease-A expired after restart")
d = json.loads(r.stdout)
raw = json.loads(lp.read_text())
check("explicit takeover succeeds + audited",
      d.get("ok") is True and raw.get("takeover_reason") and raw.get("previous_owner") == "run-A",
      f"d={d} raw={raw}")

# 9. renew by wrong owner refused
r = run("renew", TICKET, "--owner", "run-A")
check("renew by wrong owner refused", r.returncode == 7)

# 10. status lists the lease
r = run("status")
d = json.loads(r.stdout)
check("status lists lease", any(x.get("ticket") == TICKET for x in d.get("leases", [])))

# 11. invalid ticket id rejected
r = run("acquire", "../evil", "--owner", "run-A")
check("path traversal ticket id rejected", r.returncode != 0)

cleanup()
print(f"\nRESULT: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
