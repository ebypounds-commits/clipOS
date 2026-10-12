"""Benchmark eval set runner for deterministic M10 helpers (CLIP-045).

Executes every frozen case in ``tests/fixtures/clip_benchmark.json`` against
the pure scoring, overlap, and fingerprint functions. The fixture's
expectations are hand-computed (totals) or canonical hashlib outputs
(fingerprints) — any mismatch is a behavior regression, not a fixture issue.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clipos_api.clip_dedup import (
    overlap_over_shorter,
    transcript_fingerprint,
    window_fingerprint,
)
from clipos_api.clip_scores import compute_components, score_clip

_FIXTURE = Path(__file__).parent / "fixtures" / "clip_benchmark.json"


@pytest.fixture(scope="module")
def benchmark() -> dict:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert data["version"] == 1, "benchmark fixture version must stay frozen"
    return data


def test_fixture_has_meaningful_coverage(benchmark: dict) -> None:
    # Guard against accidentally shipping an empty eval set.
    assert len(benchmark["score_cases"]) >= 8
    assert len(benchmark["overlap_cases"]) >= 5
    assert len(benchmark["fingerprint_cases"]) >= 5


def test_score_cases_match_frozen_expectations(benchmark: dict) -> None:
    failures: list[str] = []
    for case in benchmark["score_cases"]:
        payload = case["input"]
        components = compute_components(
            start_ms=payload["start_ms"],
            end_ms=payload["end_ms"],
            hook_spans=[tuple(span) for span in payload["hook_spans"]],
            claim_spans=[tuple(span) for span in payload["claim_spans"]],
            word_count=payload["word_count"],
            min_len_ms=payload["min_len_ms"],
            max_len_ms=payload["max_len_ms"],
            target_wps=payload["target_wps"],
        )
        expected_components = case["expect"]["components"]
        if components != expected_components:
            failures.append(f"{case['name']}: components {components} != {expected_components}")
            continue
        total = score_clip(components, payload["weights"])
        expected_total = case["expect"]["total"]
        if abs(total - expected_total) > 1e-4:
            failures.append(f"{case['name']}: total {total} != {expected_total}")
    assert not failures, "benchmark score regressions:\n" + "\n".join(failures)


def test_overlap_cases_match_frozen_expectations(benchmark: dict) -> None:
    failures: list[str] = []
    for case in benchmark["overlap_cases"]:
        a_start, a_end = case["a"]
        b_start, b_end = case["b"]
        actual = overlap_over_shorter(a_start, a_end, b_start, b_end)
        if actual != case["expect"]:
            failures.append(f"{case['name']}: {actual} != {case['expect']}")
    assert not failures, "benchmark overlap regressions:\n" + "\n".join(failures)


def test_fingerprint_cases_match_frozen_expectations(benchmark: dict) -> None:
    failures: list[str] = []
    for case in benchmark["fingerprint_cases"]:
        if case["kind"] == "window":
            actual: str | None = window_fingerprint(
                case["media_asset_id"], case["start_ms"], case["end_ms"]
            )
        else:
            actual = transcript_fingerprint(case["text"])
        if actual != case["expect"]:
            failures.append(
                f"{case.get('text') or case.get('media_asset_id')}: "
                f"{actual!r} != {case['expect']!r}"
            )
    assert not failures, "benchmark fingerprint regressions:\n" + "\n".join(failures)
