#!/usr/bin/env python3
"""ClipOS ticket lease manager (M-1, CLIP-P003).

Single-active-agent control: one valid lease per ticket at a time.
All lease mutations are recorded (owner, timestamps, reason) so overlap
and restart behavior is auditable from repository state.

Usage:
  agent_lease.py acquire <ticket> --owner <orchestrator_run_id> [--ttl 900] [--steal-reason "..."]
  agent_lease.py renew   <ticket> --owner <orchestrator_run_id> [--ttl 900]
  agent_lease.py release <ticket> --owner <orchestrator_run_id>
  agent_lease.py status
"""
import argparse
import datetime
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LEASE_DIR = ROOT / ".agent" / "leases"
TTL_DEFAULT = 900  # lease_timeout_seconds from 15_HERMES_ORCHESTRATOR.md


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(t: datetime.datetime) -> str:
    return t.isoformat(timespec="seconds")


def lock_path(ticket: str) -> pathlib.Path:
    if not ticket or "/" in ticket or ticket.startswith("."):
        raise SystemExit(f"invalid ticket id: {ticket!r}")
    return LEASE_DIR / f"{ticket}.lock"


def read_lease(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError):
        return {"state": "corrupt", "raw_error": True}


def write_atomic(path: pathlib.Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def fail(code: int, payload: dict) -> int:
    payload["ok"] = False
    print(json.dumps(payload, indent=2))
    return code


def cmd_acquire(args) -> int:
    path = lock_path(args.ticket)
    LEASE_DIR.mkdir(parents=True, exist_ok=True)
    lease = {
        "ticket": args.ticket,
        "owner": args.owner,
        "created_at": iso(now()),
        "expires_at": iso(now() + datetime.timedelta(seconds=args.ttl)),
        "owner_pid": os.getpid(),
        "state": "active",
    }
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(lease, indent=2) + "\n")
    except FileExistsError:
        cur = read_lease(path)
        if cur is None:
            return fail(5, {"error": "unreadable_lease", "path": str(path)})
        if cur.get("raw_error"):
            return fail(5, {"error": "corrupt_lease_manual_intervention_required", "path": str(path)})
        # Idempotent re-acquire by same owner on a live lease = renew
        if cur.get("owner") == args.owner and cur.get("state") == "active":
            if now() < datetime.datetime.fromisoformat(cur["expires_at"]):
                cur["expires_at"] = iso(now() + datetime.timedelta(seconds=args.ttl))
                cur["renewed_at"] = iso(now())
                write_atomic(path, cur)
                print(json.dumps({"ok": True, "lease": cur, "note": "re-acquired by owner (renewed)"}, indent=2))
                return 0
        expired = now() >= datetime.datetime.fromisoformat(cur.get("expires_at", "1970-01-01T00:00:00+00:00"))
        if not args.steal_reason:
            # Stale takeover requires an explicit orchestrator decision
            return fail(3 if not expired else 4, {
                "error": "lease_held" if not expired else "stale_lease_requires_explicit_takeover",
                "current_lease": cur,
                "hint": "pass --steal-reason '<why>' for explicit takeover (recorded in lease file)",
            })
        # Explicit takeover (steal) — audited
        lease["previous_owner"] = cur.get("owner")
        lease["previous_state"] = cur.get("state")
        lease["takeover_reason"] = args.steal_reason
        lease["state"] = "active-takeover"
        write_atomic(path, lease)
        print(json.dumps({"ok": True, "lease": lease, "note": "explicit takeover recorded"}, indent=2))
        return 0
    print(json.dumps({"ok": True, "lease": lease}, indent=2))
    return 0


def cmd_renew(args) -> int:
    path = lock_path(args.ticket)
    cur = read_lease(path)
    if not cur or cur.get("raw_error"):
        return fail(6, {"error": "no_lease", "path": str(path)})
    if cur.get("owner") != args.owner:
        return fail(7, {"error": "owner_mismatch", "lease": cur})
    cur["expires_at"] = iso(now() + datetime.timedelta(seconds=args.ttl))
    cur["renewed_at"] = iso(now())
    write_atomic(path, cur)
    print(json.dumps({"ok": True, "lease": cur}, indent=2))
    return 0


def cmd_release(args) -> int:
    path = lock_path(args.ticket)
    cur = read_lease(path)
    if not cur or cur.get("raw_error"):
        return fail(6, {"error": "no_lease", "path": str(path)})
    if cur.get("owner") != args.owner:
        return fail(7, {"error": "owner_mismatch", "lease": cur})
    cur["state"] = "released"
    cur["released_at"] = iso(now())
    write_atomic(path, cur)
    print(json.dumps({"ok": True, "released": cur}, indent=2))
    return 0


def cmd_status(_args) -> int:
    if not LEASE_DIR.exists():
        print(json.dumps({"ok": True, "leases": []}, indent=2))
        return 0
    out = []
    for p in sorted(LEASE_DIR.glob("*.lock")):
        cur = read_lease(p)
        if cur and not cur.get("raw_error"):
            cur["expired"] = now() >= datetime.datetime.fromisoformat(cur["expires_at"])
            out.append(cur)
    print(json.dumps({"ok": True, "leases": out}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("acquire", "renew", "release"):
        sp = sub.add_parser(name)
        sp.add_argument("ticket")
        sp.add_argument("--owner", required=True)
        if name == "acquire":
            sp.add_argument("--ttl", type=int, default=TTL_DEFAULT)
            sp.add_argument("--steal-reason", default=None)
        elif name == "renew":
            sp.add_argument("--ttl", type=int, default=TTL_DEFAULT)
    sub.add_parser("status")
    args = ap.parse_args()
    return {"acquire": cmd_acquire, "renew": cmd_renew, "release": cmd_release, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
