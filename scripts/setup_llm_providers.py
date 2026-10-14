#!/usr/bin/env python3
"""Register the six provider routes through the Model Registry HTTP API.

Creates a per-provider alias (chat-claude, chat-openai, ...) plus a
fallback alias ``chat`` with all six routes ordered by priority.
Endpoints/models are environment-overridable configuration — nothing
is hard-coded as an identifier in source:

  BASE_URL            API base for this script (default 127.0.0.1:8111)
  ENDPOINT_OPENAI / ENDPOINT_DEEPSEEK / ENDPOINT_DEEPINFRA /
  ENDPOINT_MIMO / ENDPOINT_CLAUDE / ENDPOINT_GEMINI
                      e.g. https://api.openai.com/v1  (MIMO_BASE_URL
                      required for mimo; others have defaults)
  MODEL_OPENAI (gpt-4o-mini), MODEL_DEEPSEEK (deepseek-chat),
  MODEL_DEEPINFRA (deepseek-ai/DeepSeek-V3), MODEL_MIMO (required),
  MODEL_CLAUDE (claude-sonnet-4-5), MODEL_GEMINI (gemini-2.0-flash)
                      model ids stored as route model_ref (db data)

Re-running is idempotent (200 replay or 409-tolerated).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8111").rstrip("/")

DEFAULT_ENDPOINTS = {
    "openai": os.environ.get("ENDPOINT_OPENAI", "https://api.openai.com/v1"),
    "deepseek": os.environ.get("ENDPOINT_DEEPSEEK", "https://api.deepseek.com/v1"),
    "deepinfra": os.environ.get(
        "ENDPOINT_DEEPINFRA", "https://api.deepinfra.com/v1/openai"
    ),
    "mimo": os.environ.get("ENDPOINT_MIMO", os.environ.get("MIMO_BASE_URL", "")),
    "claude": os.environ.get("ENDPOINT_CLAUDE", "https://api.anthropic.com/v1"),
    "gemini": os.environ.get(
        "ENDPOINT_GEMINI", "https://generativelanguage.googleapis.com/v1beta"
    ),
}

DEFAULT_MODELS = {
    "openai": os.environ.get("MODEL_OPENAI", "gpt-4o-mini"),
    "deepseek": os.environ.get("MODEL_DEEPSEEK", "deepseek-chat"),
    "deepinfra": os.environ.get("MODEL_DEEPINFRA", "deepseek-ai/DeepSeek-V3"),
    "mimo": os.environ.get("MODEL_MIMO", ""),
    "claude": os.environ.get("MODEL_CLAUDE", "claude-sonnet-4-5"),
    "gemini": os.environ.get("MODEL_GEMINI", "gemini-2.0-flash"),
}

# Fallback priority for the shared `chat` alias: strongest-first.
CHAT_CHAIN = ["claude", "openai", "gemini", "deepseek", "deepinfra", "mimo"]

# Route metadata contract understood by clipos_api.llm_providers:
# protocol token + credential env name (scripts may hold vendor literals;
# application source never does).
PROTOCOLS = {
    "openai": "compat",
    "deepseek": "compat",
    "deepinfra": "compat",
    "mimo": "compat",
    "claude": "messages",
    "gemini": "gen-content",
}
KEY_ENVS = {
    "openai": os.environ.get("KEY_OPENAI", "OPENAI_API_KEY"),
    "deepseek": os.environ.get("KEY_DEEPSEEK", "DEEPSEEK_API_KEY"),
    "deepinfra": os.environ.get("KEY_DEEPINFRA", "DEEPINFRA_API_KEY"),
    "mimo": os.environ.get("KEY_MIMO", "MIMO_API_KEY"),
    "claude": os.environ.get("KEY_CLAUDE", "ANTHROPIC_API_KEY"),
    "gemini": os.environ.get("KEY_GEMINI", "GEMINI_API_KEY"),
}


def post_route(payload: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{BASE}/models",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()[:200]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


def main() -> int:
    failures: list[str] = []
    for provider in (
        "claude",
        "openai",
        "gemini",
        "deepseek",
        "deepinfra",
        "mimo",
    ):
        endpoint = DEFAULT_ENDPOINTS[provider]
        model = DEFAULT_MODELS[provider]
        if not endpoint:
            hint = (
                "MIMO_BASE_URL (or ENDPOINT_MIMO)"
                if provider == "mimo"
                else f"ENDPOINT_{provider.upper()}"
            )
            failures.append(f"{provider}: no endpoint (set {hint})")
            continue
        if not model:
            failures.append(f"{provider}: no model (set MODEL_{provider.upper()})")
            continue
        for alias, priority, chain in (
            (f"chat-{provider}", 0, [provider]),
            ("chat", CHAT_CHAIN.index(provider), CHAT_CHAIN),
        ):
            if provider not in chain:
                continue
            payload = {
                "alias": alias,
                "provider": provider,
                "endpoint_ref": endpoint,
                "model_ref": model,
                "priority": priority,
                "enabled": True,
                "metadata": {
                    "protocol": PROTOCOLS[provider],
                    "api_key_env": KEY_ENVS[provider],
                },
            }
            status, body = post_route(payload)
            mark = (
                "OK"
                if status in (200, 201)
                else ("REPLAY/CONFLICT" if status == 409 else "FAIL")
            )
            if mark == "FAIL":
                failures.append(f"{alias}/{provider}: HTTP {status} {body}")
            print(
                f"[{mark:15}] {alias:<18} {provider:<10} p{priority} {endpoint}  model={model}"
            )
    if failures:
        print("\nFAILED:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("\nAll provider routes registered. Next: scripts/test_llm_dispatch.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
