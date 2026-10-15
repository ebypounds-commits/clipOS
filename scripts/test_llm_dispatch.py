#!/usr/bin/env python3
"""Test model dispatch through POST /models/{alias}/dispatch.

Runs one completion per provider alias (chat-claude, chat-openai, ...)
plus the shared `chat` fallback alias. Works with or without API keys
in the server environment: missing keys surface as 502 with a clear
"set <ENV>" message and are reported as KEY-MISSING (not a crash).

  BASE_URL  API base (default http://127.0.0.1:8111)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8111").rstrip("/")
PROVIDERS = ["claude", "openai", "gemini", "deepseek", "deepinfra", "mimo"]


def dispatch(alias: str, execution_id: str) -> tuple[int, dict]:
    payload = {
        "execution_id": execution_id,
        "messages": [
            {"role": "user", "content": "Reply with exactly the word: pong"},
        ],
        "params": {"temperature": 0.0, "max_tokens": 16},
    }
    req = urllib.request.Request(
        f"{BASE}/models/{alias}/dispatch",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            status, body = resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            body = json.loads(exc.read().decode())
        except json.JSONDecodeError:
            body = {}
    except Exception as exc:  # noqa: BLE001 - transport failure is a test result
        return 599, {"detail": str(exc)[:200]}
    body = dict(body)
    body["_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return status, body


def main() -> int:
    print(f"dispatching against {BASE}\n")
    results: list[tuple[str, str]] = []
    for index, provider in enumerate(PROVIDERS):
        alias = f"chat-{provider}"
        status, body = dispatch(alias, f"probe-{provider}-{os.getpid()}-{index}")
        if status == 200:
            content = str(body.get("content", ""))[:60].replace("\n", " ")
            mark = "PASS"
            detail = (
                f"provider={body.get('provider')} model={body.get('model_ref')} "
                f"tokens={body.get('usage', {}).get('input_tokens')}/"
                f"{body.get('usage', {}).get('output_tokens')} "
                f"latency={body.get('latency_ms')}ms http={body.get('_ms')}ms "
                f"content={content!r}"
            )
        else:
            detail_text = str(body.get("detail", body))[:200]
            if "missing api key env" in detail_text or "api key missing" in detail_text or "all routes failed" in detail_text:
                mark = "KEY-MISSING"
            else:
                mark = "FAIL"
            detail = detail_text
        results.append((mark, f"{alias}: {detail}"))
        print(f"[{mark:11}] {alias}: {detail}")

    status, body = dispatch("chat", f"probe-chat-fallback-{os.getpid()}")
    if status == 200:
        detail = (
            f"fallback served by provider={body.get('provider')} "
            f"model={body.get('model_ref')} content="
            f"{str(body.get('content', ''))[:60]!r}"
        )
        results.append(("PASS", f"chat (fallback): {detail}"))
        print(f"[{'PASS':11}] chat (fallback): {detail}")
    else:
        detail = str(body.get("detail", body))[:200]
        if (
            "missing api key env" in detail
            or "api key missing" in detail
            or "all routes failed" in detail
        ):
            mark = "KEY-MISSING"
        else:
            mark = "FAIL"
        results.append((mark, f"chat (fallback): {detail}"))
        print(f"[{mark:11}] chat (fallback): {detail}")

    passed = sum(1 for mark, _ in results if mark == "PASS")
    missing = sum(1 for mark, _ in results if mark == "KEY-MISSING")
    failed = sum(1 for mark, _ in results if mark == "FAIL")
    print(
        f"\nSUMMARY: {passed} passed, {missing} key-missing, {failed} failed "
        f"of {len(results)} dispatches"
    )
    if failed:
        return 1
    if passed == 0:
        print(
            "No live providers yet: export provider API keys in the server "
            "environment and restart, then re-run."
        )
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
