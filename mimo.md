# ClipOS — Handoff Note (`mimo.md`)

**Repo:** https://github.com/ebypounds-commits/clipOS.git (private; no `gh` CLI/token on box — access via VPS git only)
**VPS:** `ssh vps` → root@173.212.230.3 · canonical repo `/srv/clipos/clipOS` · worktrees `/srv/clipos/worktrees/CLIP-0NN`
**Canonical runtime env (per worktree):** `services/api` package · venv `<worktree>/.venv` (Python 3.14; CI runs 3.12 — mypy verified clean under `--python-version 3.12`) · PG dev DB `localhost:5433` (`clipos_dev`), Redis `6380`, MinIO `9002`.
**Head at handoff:** main = `2a5d78a` · CI **GREEN** (run 38180545396: validate/test/quality/build all success) · suite **567 passed + 11 skipped** · mypy clean · ruff clean.

---

## 1. Where the build stands

- **46 of 102 tickets DONE** (P001–P006 prep + CLIP-001…038 + CLIP-040). `10_TICKETS.yaml` says 46 DONE / 56 TODO; BUILD_STATE completed list trails by design (CLIP-033+ land via merge commits, state flips in the `complete — state transition` commit). Trust `10_TICKETS.yaml` + git log for truth.
- **Milestones M0–M8 fully DONE. M9: 3/4 done (only CLIP-041 landed; CLIP-041 in 10_TICKETS still marked TODO — stale, see §5).**
- **Landed since last full audit (chronological):**
  - `ba29e7b`/`46879b1` — CLIP-038 CI fixes (module-level skip needs `allow_module_level=True`; ruff format drift in tests).
  - CLIP-039 asset-validation — `f4a517f` (worktree) → merged; FFprobeValidator via real `/usr/bin/ffprobe` subprocess (30s timeout, JSON parse), corrupt/missing → QUARANTINED, `asset_validations` append-only + `quarantine_records` idempotent, migration `0019`. 12 tests.
  - **CLIP-040 transcription** — `8ffdaf0` merged `3a46fa6`; `transcripts` idempotent per (org, media_asset, language), `transcript_segments` append-only with monotonic `seq`, `TranscriptionAdapter` Protocol + deterministic Mock adapter, 4 routes (Depends providers BEFORE decorators), migration `0020`, 17 tests. State → 46/102 (`c02d6eb`).
  - **CLIP-041 semantic segmentation/embeddings** — `230d0df`; `semantic_segments` (org-scoped, TOPIC/SCENE/CLAIM/HOOK, source TRANSCRIPT/PROBE/MANUAL), `segment_embeddings` (registry `model_alias`, `vector_ref` refs-not-blobs, (segment, model_alias) unique), pure `chunk_transcript` + deterministic `fake_embed`, `SemanticPipeline`, migration `0021`. 26 tests.
  - **CLIP-042 clip candidate extraction** — `6b59b72` merged `5f400b5`; `clip_candidates` (status NEW→SCORED→APPROVED/REJECTED), deterministic heuristic `extract_candidates` (length window, overlap merge, gap), `ClipCandidateStore`, flow `extract_and_register`, migration **renumbered 0022→`0023`** to linearize the chain. 23 tests.
  - `ee90061` state transition → 48/102 nominal; `a963596`/`bd60d48`/`2a5d78a` CI fixes. PG migrated to 0023.
- Migration chain (linear): 0015 topics → 0016 opportunities → 0017 watchlists → 0018 media_ingestion → 0019 asset_validation → 0020 transcription → 0021 semantic → 0023 clip_candidates.
- Parallel dispatch **validated**: CLIP-041+042 ran simultaneously on disjoint modules/migrations/tests, then serialized at merge.

## 2. Standing workflow (reproduce exactly)

1. **PATH GUARD first (mandatory):** `bash /root/dispatch_path_guard.sh <ticket> <worktree>` — validates worktree toplevel, temp root `/tmp/clipos/<id>`, zero Windows drive-letter paths in packet/scripts (exit 64–66). This is the CLIP-039 path-safety policy, permanently enforced; review stored at `docs/reports/CLIP039_PATH_SAFETY_REVIEW.md`.
2. **Worktree + venv:** `git worktree add -b agent/<T> <wt> origin/main`; chown clipos:clipos; `python3.14 -m venv .venv && .venv/bin/pip install -qe 'services/api[dev]'`.
3. **Lease:** `python3 scripts/agent_lease.py acquire <ticket> --owner hermes-clipos-m1-001 --ttl 14400` (must be `ok: true`).
4. **Packet:** write task packet locally (Windows scratch staging), `scp` to `/root/clipNNN_prompt.md`, then `/home/clipos/clipNNN_prompt.md`. Packet MUST carry `host_context`, `path_policy`, `paths` (repo/worktree/temp roots), `runtime.python` — the PATH AUTHORITY RULE. Packet specifies the single module, migration id, test file, and marker string.
5. **Dispatch:** `codex exec --profile glm --sandbox workspace-write - < packet`, `timeout 1500`, 1 attempt, disjoint modules per parallel agent. GLM free model; OpenRouter key loaded from `/home/clipos/.codex/openrouter_key` (never in argv).
6. **Verify independently — NEVER trust worker markers.** Two false GREENs so far (CLIP-033 echo marker; CLIP-040 claimed lint-clean but wasn't). Re-run: `ruff format --check`, `ruff check`, `mypy`, full `pytest` yourself.
7. **Orchestrator-finish** anything partial (workers routinely under-deliver: missing routers, missing tests, missing tool_policy import).
8. **Land:** commit in worktree → merge to main (custom merge, union test_migrations table list) → PG alembic upgrade → state-transition commit updating BUILD_STATE + audit.md → push → poll CI via `git fetch origin ci-results` + `last_run.txt` (no GitHub token; private API = 404).
9. **Model-health cron (live):** `*/5 * * * * bash /root/model_health2.sh` → `/root/model_health.log`; green line = `status=ok openrouter=ok remaining=2.72 ollama=ok codex=ok`. (bf_selfheal cron also present — pre-existing, not ours.)

## 3. Issues & bugs (all resolved, but they will recur)

1. **PEP 563 + FastAPI triple landmine (hit 3×: CLIP-033, 039, 042).** `Annotated[str, Query(...)]` nested under `from __future__ import annotations` stringifies at REQUEST time → PydanticUserError; `TYPE_CHECKING`-gated real classes in route annotations break OpenAPI build; Depends providers defined AFTER decorators degrade to query params. Rule: real runtime imports in route modules, providers before decorators. Probe pattern: TestClient request, not just import.
2. **Module-level `pytest.skip`** needs `allow_module_level=True` or CI lane-5 collection errors (CLIP-038).
3. **ruff format drift** — workers don't run `ruff format`; every land needs a format pass (CLIP-038, 040).
4. **Migration numbering races under parallel dispatch** — two workers picked 0021/0022 while another 0022 existed; fixed by renumber to 0023 + linearize. Rule: assign migration ids in the packet, never let workers choose.
5. **Root-owned git/objects + `.mypy_cache`/`.ruff_cache`** in canonical repo break worktree commits — `chown -R clipos:clipos` after root-side git ops.
6. **Hermes context-compression truncating tool-call writes** (host-side, not repo) — countermeasure: full literal strings, re-verify written files from tool result length before scp/exec.
7. **ruflo unusable as worker** (routes everything through `claude -p`, no Anthropic key) — parallelism = parallel disjoint `codex exec` runs instead.
8. **Local ollama qwen2.5-coder:7b** works (`codex -p ollama-local`, profile `/home/clipos/.codex/ollama-local.config.toml`) but is too small for full packets — bootstrap/ping only; orchestrator-finish fallback.
9. **`assert '' is None`-class bugs, scalar-row ORM misuse, missing tests/routers** in worker output — expect orchestrator finishing on most tickets.
10. **OpenRouter key remaining 2.72** of ~5.00 — free-model profiles only; monitor via cron.
11. **CLIP-041/042 forward-ref OpenAPI failures** — fixed by replacing `TYPE_CHECKING` aliases with real imports in routers (`a963596`, `87db815`).

## 4. Environment invariants (do not break)

- Ports pre-existing (DO NOT TOUCH): 3000, 80, 443, 3080, 18080, 8090, PG 5432, Tailscale. clipOS stack: 5433/6380/9002/9003/9005, Temporal 7233/8233/9233.
- Workers must NOT edit `BUILD_STATE.json` or mermaid blocks.
- No provider/model identifiers hard-coded where a registry alias applies (standing acceptance criterion).
- Canonical checkout `/srv/clipos/clipOS` is for integration/merges/state only — never implementation.
- Secrets only via file (600) or stdin; never argv/logs/chat.

## 5. Remaining scope (56 TODO tickets)

| Milestone | Tickets | Content |
|---|---|---|
| **M9** (1 left) | CLIP-041 → **stale TODO in 10_TICKETS.yaml — actually landed**; flip to DONE in next state commit. If a 4th M9 ticket appears in the file, it's the real remainder. | ingestion/transcription/semantic wrap-up |
| **M10** | CLIP-043 **← NEXT**, 044, 045 | multi-factor clip scoring (next migration 0024); duplicate/content fingerprint checks; review UI + benchmark eval set |
| **M11** | 046–049 | research packet/claim-source model; brief/content-plan engine; hooks/scripts/versioning; factuality/transformation evals |
| **M12–M23** | 4 tickets each | per `10_TICKETS.yaml` (titles in file) |
| **M24** | 5 tickets | qualification & release; **CLIP-099 = guarded public publishing with owner gate — the ONLY human-gated ticket** |
- TODO count check: 4×(M10…M23) + 5×M24 = 56 ✓ (matches grep).
- Remaining migrations: next free id is **0024**.
- Unblocked-now candidates: CLIP-043 (deps: CLIP-042 ✓) and any independent M11+ ticket — parallel dispatch pattern is proven; keep disjoint modules + pre-assigned migration ids.

## 6. Immediate next actions

1. Flip CLIP-041 to DONE in `10_TICKETS.yaml` (stale) in the next state-transition commit.
2. Dispatch CLIP-043 (multi-factor clip scoring): scores table (next migration **0024**), `ClipScoringService` consuming CLIP-042 candidates, deterministic pure scorers + registry-alias model refs, routes with Depends-before-decorator, tests, PG upgrade to 0024, merge+push+CI poll.
3. Optionally parallel-dispatch CLIP-044 or CLIP-046 alongside 043 (disjoint files, pre-assigned migration ids 0025+ if needed).
4. Keep the PATH GUARD + lease + verify-independently cadence; expect orchestrator-finish.
5. ChatGPT/Codex OAuth (host side, separate thread): browser auth completed 3× but credential not persisting in profile pool (`hermes auth status openai-codex` → logged out). Unresolved; not a repo blocker.

## 7. Key artifacts index

- `BUILD_STATE.json`, `10_TICKETS.yaml`, `docs/reports/audit.md`, `docs/reports/STATE_AUDIT.jsonl`, `docs/reports/CLIP039_PATH_SAFETY_REVIEW.md`, `docs/reports/M-1_COMPLETION.md`
- VPS: `/root/dispatch_path_guard.sh`, `/root/clipNNN_prompt.md` packets, `/root/ci_poll_*.sh`, `/root/model_health2.sh` + `or_parse.py`, `/home/clipos/.codex/{glm,ollama-local,mimo,deepseek}.config.toml`
- Local scratch (staging before scp): `C:\Users\Gozmar Dynamics\AppData\Local\hermes\profiles\youtube-video-content-creator\cache\scratch\`
