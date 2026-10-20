
## 2026-10-11 â€” CLIP-041 + CLIP-042 (parallel dispatch)
- First successful parallel codex run: CLIP-041 (semantic.py 602L, 26 tests) + CLIP-042 (clip_candidates 320L + router 198L, 23 tests) on disjoint modules/migrations.
- Orchestrator fixes: frozen-pydantic created_at, missing router routes (041), missing tool_policy import in main.py (042), B008 Annotated conversion both, migration renumber 0022->0023 + linearize 0021->0023, union test_migrations table list.
- Gates: main 567 passed + 11 skipped; mypy clean 49/50 files; ruff clean; PG upgraded to 0023.
- Migration chain now linear: 0019 -> 0020 -> 0021 -> 0023.

## 2026-10-12 â€” Phase-0 housekeeping (MiMo orchestrator takeover)
- Role transfer: MiMo Desktop is now the build orchestrator (operator decision: direct implementation over VPS SSH, no Codex worker dispatch; OpenRouter top-up deferred for future parallel use).
- Flipped stale CLIP-041/042 TODO -> DONE in 10_TICKETS.yaml (both landed 2026-10-11: 230d0df, 5f400b5).
- BUILD_STATE reconcile: completed_tickets backfilled CLIP-033..042; tickets_completed 46 -> 48; removed stray tokens_completed typo field; current_milestone/milestone/status -> M10; completed_milestones -> M0..M9; last_evidence -> CLIP-042 with verifiable facts; orchestrator/coding_engine fields updated.
- STATE_AUDIT.jsonl: appended CLIP-041/042 entries (state_audit.py append).
- Hygiene: untrack .alembic-dev.db (accidentally committed in ee90061), add to .gitignore; prune 42 stale merged ticket worktrees under /srv/clipos/worktrees; stale lease lock files remain as historical records (leases expired; acquire on live tickets works).
- Local validators: validate_state.py green pre-push; full handoff/state_audit/architecture_guard/secret_scan gates re-run on VPS canonical before push; CI polled to green.

## 2026-10-12 - CLIP-043 multi-factor clip scoring (single-orchestrator direct implementation)
- Module clipos_api/clip_scores.py: pure scorers length_fit / span_coverage (merged union, clips to window) / density_score / score_clip (weighted 0..100, CLIP-036 contract); compute_components derives length_fit + hook_coverage + claim_coverage + transcript_density from candidate window, semantic_segments (HOOK/CLAIM), transcript_segments (word count) - all org-scoped.
- ClipScoreStore.record_score idempotent per (org_id, candidate_id): identical re-score no-op, different fields conflict (409); NEW->SCORED transition audited (clip_scores.recorded + clip_candidate.scored); APPROVED/REJECTED candidates frozen; scorer_alias nullable registry ref (vendor scan clean).
- Migration 0024 clip_scores (revises 0023; unique score_id + unique (org_id, candidate_id) + org index); PG dev upgrade 0023->0024 idempotent (alembic current=0024 head); offline SQL render asserts CREATE TABLE clip_scores.
- Routes Depends-before-decorator + real runtime imports: POST /clip-candidates/{id}/scores (404 missing / 409 decided-or-conflict / 422 bad weights incl. unknown factor), GET .../scores (null when unscored), GET /clip-scores?org_id=; main.py wiring; OpenAPI probe 81 paths green.
- Circular-import fix: transcription_store <-> transcription cycle entered unsafely from my import -> deferred import inside load_scoring_context (repo-established pattern).
- Gates (observed): ruff format --check clean; ruff check clean; mypy clean 52 files (CI invocation + --python-version 3.12); pytest SQLite 602+11 (89.04% cov); pytest CI-env full services 613 passed (89.51% cov); secret_scan clean; architecture_guard 6/6; state_audit verify 48/48; 35 new tests in test_clip_scores.py.
- Land: worktree commit c200d92, merge 82dc8bc into main; state advance 49/102, current -> CLIP-044.

## 2026-10-12 - CLIP-044 duplicate/content fingerprint checks (single-orchestrator direct implementation)
- Module clipos_api/clip_dedup.py: pure normalize_text / overlap_over_shorter (overlap over shorter window, degenerate-safe) / window_fingerprint (window:asset:start-end sha256) / transcript_fingerprint (normalized text sha256, None on empty - absent content never collides).
- candidate_fingerprints (migration 0025, 0024->0025 PG idempotent): org-scoped, unique (org_id, candidate_id, kind), lookup index (org_id, kind, value); ClipDedupStore.persist_fingerprint idempotent (identical no-op, different value -> 409 conflict).
- Flow check_candidate_duplicates: persists WINDOW always + TRANSCRIPT when primary-transcript (earliest created_at, seq-ordered segments) window text exists; window dups = same asset overlap_over_shorter >= caller threshold (default 0.5); content dups = on-the-fly transcript hash of every other org candidate (corpus need not be pre-fingerprinted - design corrected during test cycle); audited candidate_fingerprints.persisted + clip_candidates.duplicate_check.
- Routes: POST /clip-candidates/{id}/duplicate-check (404 missing / 409 conflict / 422 bad threshold), GET /clip-candidates/{id}/fingerprints (404 unknown - try/except added after TestClient probe caught uncaught ValueError); main.py wiring; OpenAPI probe 83 paths.
- Fixes during gates: removed persisted-only content lookup (found nothing when other candidate unchecked), UP012 .encode(), mypy Literal cast for algorithm.
- Gates (observed): ruff format --check clean; ruff check clean; mypy clean 53 files (CI invocation + --python-version 3.12); pytest SQLite 626+11 (89.22% cov); pytest CI-env full services 637 passed (89.68% cov); secret_scan clean; architecture_guard 6/6; state_audit verify 49/49; 24 new tests in test_clip_dedup.py.
- Land: worktree commit 1fbf1bb, merge 49084c0 into main; state advance 50/102, current -> CLIP-045 (M10 next: review UI + benchmark eval set, last M10 ticket).

## 2026-10-12 - CLIP-045 review UI + benchmark eval set; M10 COMPLETE (single-orchestrator direct implementation)
- clip_review.py: candidate_decisions (migration 0026, 0025->0026 PG idempotent, append-only, unique (candidate_id, decision)); ClipReviewStore.record_decision idempotent per (kind, actor) â€” identical no-op, different actor -> 409; APPROVE/REJECT sets candidate status (reversal allowed per CLIP-036 structure); audited clip_candidates.decided; routes POST/GET /clip-candidates/{id}/decisions (404/409).
- ClipCandidateView response model (+candidate_id, +status) across create/get/list/extract â€” the review surface now has everything it needs; request body unchanged (ClipCandidateCreate).
- Web /reviews: server page (await searchParams; org from ?org= or NEXT_PUBLIC_ORG_ID; loads candidates+scores) + ReviewBoard client (equal-weight Score button, Check duplicates with threshold field showing finding counts/ids, Approve/Reject with actor field, error surface, router.refresh); home nav += Clip Review link.
- Benchmark eval set: tests/fixtures/clip_benchmark.json v1 â€” 9 score cases (hand-computed components/totals), 6 overlap cases, 5 fingerprint cases (canonical hashlib sha256); test_clip_benchmark.py runs every case with minimum-count floor guard (set cannot silently shrink).
- LATENT BUG FOUND+FIXED by TestClient probes: clip_candidates_router + semantic used Depends(get_engine) â€” get_engine(url) made FastAPI require ?url= query on every route (422; CLIP-042 never HTTP-probed its routes). Switched both to the CLIP-033-proven _database_engine late-import pattern. This unblocked the review UI.
- Gates (observed): ruff format/check clean; mypy clean 54 files (CI + --python-version 3.12); pytest SQLite 644+11 (89.44% cov); pytest CI-env full services 655 passed (89.90% cov); PG upgrade 0025->0026 idempotent (current=0026 head); tsc --noEmit clean; next build green (/, /agents, /models, /reviews); npm install + npm audit 0 vulns; OpenAPI probe 84 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 50/50; 18 new tests (test_clip_review 14 + test_clip_benchmark 4; suite delta 637->655).
- Land: worktree commit 5a5ac3d, merge e655077; state advance 51/102, M10 COMPLETE (CLIP-042 extraction, 043 scoring, 044 dedup, 045 review), current -> CLIP-046 (M11 research/claims engine begins).

## 2026-10-12 - CLIP-046 research packet and claim/source model (M11 opener, single-orchestrator direct implementation)
- research.py: ResearchPacket / Claim / ClaimSource contracts + ClaimVeracity (UNVERIFIED/VERIFIED/DISPUTED) + SourceSupport (SUPPORTS/CONTRADICTS) enums; three SQL tables (migration 0027, 0026->0027 PG idempotent): research_packets uq (org, packet), claims uq (org, claim) + ix (org, packet), claim_sources uq (claim, source_ref) + ix (org).
- Idempotency (CLIP-016 contract): packet register per (org, research_packet_id) identical-noop / different-fields 409; claim register per (org, claim_id) requires existing packet (404); source link per (claim_id, source_ref) identical-noop / different support-or-quote 409.
- Veracity lifecycle: claims created UNVERIFIED always (verification only via audited route); set_veracity any transition incl. reopen, same-value no-op, audited claims.veracity_changed {from, to, actor_ref, reason_ref}.
- Routes (8, CLIP-033 pattern): POST/GET /research-packets, POST/GET /research-packets/{id}/claims (path/body id mismatch -> 422), POST/GET /claims/{id}/sources, POST /claims/{id}/veracity; _map_error 404 (not found) / 409 (already...) / 422.
- Audits: research_packets.registered, claims.registered, claims.veracity_changed, claim_sources.linked (source_ref recorded as 64-char prefix — refs, not content).
- Gates (observed): ruff format/check clean (I001 in main.py auto-fixed); mypy clean 55 files (CI + --python-version 3.12); pytest SQLite 675+11 (89.68% cov); pytest CI-env full services 686 passed (90.12% cov); PG upgrade 0026->0027 idempotent (current=0027 head); OpenAPI probe 89 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 51/51; 31 new tests in test_research.py (suite delta 655->686 CI-env).
- Land: worktree commit cb2694c, merge 1e90691 into main; state advance 52/102, current -> CLIP-047 (brief/content-plan engine; next free migration 0028).

## 2026-10-12 - CLIP-047 brief/content-plan engine (single-orchestrator direct implementation)
- content_planning.py: ContentBrief (client natural id, idempotent per (org, brief), requires existing research packet -> 404, platforms validator 1..64 chars x <=16), ContentPlan (versioned payload), PlanRequest; pure derive_plan_payload (sorted claim_ids byte-stable, claim_count, echoed objective/platforms/refs — refs only).
- Plan engine: generate_plan fail-closed factuality gate — brief 404 -> packet 404 -> zero VERIFIED claims = 422 (UNVERIFIED/DISPUTED never planned); identical re-derivation returns latest row untouched; changed claim state appends version N+1 + audits content_plans.generated {version, claim_count}; get_latest_plan returns null when none.
- content_plans append-versioned: migration 0028 (0027->0028 PG idempotent, unique (org_id, brief_id, version) + ix (org, brief)); content_briefs uq (org, brief); audited content_briefs.registered.
- Routes (5, CLIP-033 pattern, _map_error 404/409/422): POST/GET /content-briefs, POST /content-briefs/{id}/plan, GET /content-briefs/{id}/plan (ContentPlan | null).
- Gates (observed): ruff format/check clean (2 I001 auto-fixes); mypy clean 56 files (CI + --python-version 3.12); pytest SQLite 696+11 (89.86% cov); pytest CI-env full services 707 passed (90.29% cov); PG upgrade 0027->0028 idempotent (current=0028 head); OpenAPI probe 92 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 52/52; 21 new tests in test_content_planning.py (suite delta 686->707 CI-env).
- Land: worktree commit d00c010, merge 29cfa73 into main; state advance 53/102, current -> CLIP-048 (hooks/scripts/versioning; next free migration 0029).

## 2026-10-12 - CLIP-048 hooks/scripts/versioning (single-orchestrator direct implementation)
- script_hooks.py: HookCreate/Hook (semantic idempotent key (org_id, brief_id, text) unique index, server uuid hook_id, brief existence gate), Script (client script_id, idempotent per (org, script_id), conflict on different brief/title, current_version starts 0), ScriptVersionRequest/ScriptVersion (append-only bodies <=8192).
- Versioning semantics: save_version — latest body == request body => return latest untouched (no-op regardless of author); changed body => mint version N+1, bump scripts.current_version, audit scripts.version_appended {version, author_ref}; list_versions ordered asc, 404 unknown script.
- Migration 0029 (0028->0029 PG idempotent): hooks uq (org, brief, text) + ix (org, brief); scripts uq (org, script_id) + ix (org, brief); script_versions uq (org, script_id, version) + ix (org, script_id).
- Routes (7, CLIP-033 pattern, _map_error 404/409/422): POST /hooks, GET /hooks (404 unknown brief), POST/GET /scripts, GET /scripts/{id}, POST /scripts/{id}/versions, GET /scripts/{id}/versions.
- Audits: hooks.registered (text as 64-char prefix), scripts.registered, scripts.version_appended.
- Gates (observed): ruff format/check clean (2 I001 auto-fixes); mypy clean 57 files (CI + --python-version 3.12); pytest SQLite 718+11 (90.09% cov); pytest CI-env full services 729 passed (90.51% cov); PG upgrade 0028->0029 idempotent (current=0029 head); OpenAPI probe 96 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 53/53; 22 new tests in test_script_hooks.py (suite delta 707->729 CI-env).
- Land: worktree commit b52f670, merge 8f792ad into main; state advance 54/102, current -> CLIP-049 (factuality/transformation evals — FINAL M11 ticket; next free migration 0030).

## 2026-10-12 - CLIP-049 factuality/transformation evaluations; M11 COMPLETE (single-orchestrator direct implementation)
- content_evals.py: FactualityEvaluation (client eval_id, idempotent per (org, eval_id), claim existence gate 404, conflict 409) + EvalResult (PASS/FAIL/INCONCLUSIVE) + VERACITY_FOR_RESULT module mapping (PASS->VERIFIED, FAIL->DISPUTED, INCONCLUSIVE->None).
- Engine behavior: record_evaluation applies the mapping ATOMICALLY with the eval insert in one transaction; claims.veracity_changed audited with actor_ref=eval.actor_ref and reason_ref=eval_id (every veracity move cites its evidence); already-matching target records eval without re-transition; identical re-record no-op.
- TransformationRecord rights-chain (07_SECURITY): from_ref->to_ref->method per brief, brief existence gate, idempotent per (org, transformation_id), refs <=255 (PG btree entry limit on uq index), description <=2048; audited transformation_records.registered.
- Migration 0030 (0029->0030 PG idempotent): factuality_evaluations uq (org, eval_id) + ix (org, claim); transformation_records uq (org, transformation_id) + ix (org, brief).
- Routes (4, CLIP-033 pattern): POST/GET /factuality-evaluations (GET claim-scoped 404), POST/GET /transformation-records (GET brief-scoped 404).
- Test fix during gates: test_list_transformations_org_scoped expected [] for a foreign org, but brief-gated list correctly 404s (brief is org-scoped) — test aligned to the documented behavior.
- Gates (observed): ruff format/check clean; mypy clean 58 files (CI + --python-version 3.12); pytest SQLite 739+11 (90.29% cov); pytest CI-env full services 750 passed (90.70% cov); PG upgrade 0029->0030 idempotent (current=0030 head); OpenAPI probe 98 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 54/54; 21 new tests in test_content_evals.py (suite delta 729->750 CI-env).
- Land: worktree commit 1d6805b, merge 5ae2cda into main; state advance 55/102, M11 COMPLETE (CLIP-046 research, 047 plans, 048 hooks/scripts, 049 evals), current -> CLIP-050 (M12 automated media studio begins; next free migration 0031).

## 2026-10-12 - CLIP-050 render project DSL (M12 opener, single-orchestrator direct implementation)
- render_project.py: validate_render_spec pure validator — version==1, inputs.candidate_id 1..128, aspect in {1:1, 16:9, 9:16}, duration_ms int 1000..600000, captions {enabled bool, style_ref null|1..128}, audio_normalization {enabled bool, target_lufs -24..-5 required when enabled}, outputs 1..8 unique platform slugs (1..64), unknown top-level keys rejected (typos fail loudly), nested unknown keys tolerated (forward compatible).
- render_projects (migration 0031, 0030->0031 PG idempotent): uq (org_id, render_project_id) + ix (org_id, brief_id); register validates spec BEFORE persistence (422 carrying first 5 validator errors, nothing written), brief gate 404, idempotent identical re-register / changed brief+name+spec conflict 409, audited render_projects.registered {spec_version, output_count}.
- Dry-run route POST /render-specs/validate: pure {valid, errors} — zero database dependency (works without DATABASE_URL), the tooling pre-check for spec documents.
- Routes (4, CLIP-033 pattern): POST /render-specs/validate (pure), POST/GET /render-projects, GET /render-projects/{id} (404).
- Design note: spec JSON is the source document CLIP-051's FFmpeg pipeline consumes; captions/audio/aspect exercised here as data, executed by the pipeline ticket.
- Gates (observed): ruff format/check clean (1 I001 auto-fix); mypy clean 59 files (CI + --python-version 3.12); pytest SQLite 766+11 (90.35% cov); pytest CI-env full services 777 passed (90.76% cov); PG upgrade 0030->0031 idempotent (current=0031 head); OpenAPI probe 101 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 55/55; 27 new tests in test_render_project.py (suite delta 750->777 CI-env).
- Land: worktree commit 7136d51, merge 03daf19 into main; state advance 56/102, current -> CLIP-051 (FFmpeg worker/render pipeline — heaviest M12 ticket; next free migration 0032).

## 2026-10-12 - CLIP-051 FFmpeg worker/render pipeline (single-orchestrator direct implementation)
- render_pipeline.py: spec_fingerprint (sha256 over canonical sorted-key compact JSON — key order irrelevant), build_render_argv pure (trim -t seconds, ASPECT_FILTERS scale+pad chains 9:16->1080x1920 / 16:9->1920x1080 / 1:1->1080x1080 black bars, optional loudnorm=I=<lufs> when audio normalization enabled, -movflags +faststart, shell=False argv — no injection surface, FFMPEG_BIN env default ffmpeg).
- RenderRunner protocol: SubprocessRenderRunner (capture_output, timeout RENDER_FFMPEG_TIMEOUT_SECONDS default 120 -> exit 124 failed job, stderr tail <=512, OSError -> 127); MockRunner test double records argv.
- render_jobs (migration 0032, 0031->0032 PG idempotent): uq (org, job_id) + ix (org, render_project); create gate 404 (project must exist) captures spec_fingerprint, idempotent same-project re-create / cross-project conflict 409, audited render_jobs.created.
- execute_job: workdir confinement FIRST (RENDER_WORKDIR default /tmp/clipos-render, CLIP-039 path policy: escape -> 422, input must exist, output must be .mp4); status gates PENDING|FAILED executable, SUCCEEDED|RUNNING -> 409 (retry-friendly, no duplicate renders); RUNNING stamped before run; success -> status SUCCEEDED + deterministic manifest {fingerprint, argv, paths, duration_ms, aspect, captions_requested, audio_normalized, returncode, completed_at} + audited render_jobs.succeeded; failure -> FAILED + error_ref stderr tail + audited render_jobs.failed.
- Routes (4, CLIP-033 pattern): POST /render-projects/{id}/jobs, POST /render-jobs/{id}/execute (real SubprocessRenderRunner), GET /render-jobs/{id} (404), GET /render-jobs?render_project_id= (project gate 404).
- Fix during gates: test_execute_missing_job used relative output path -> confinement fired before job lookup (422 not 404); test corrected to workdir-absolute output. Draft defects caught pre-transfer: leftover NotImplementedError stub + enum import hack removed.
- Gates (observed): ruff format/check clean (I001 auto-fixes); mypy clean 60 files (CI + --python-version 3.12); pytest SQLite 792+11 (90.43% cov); pytest CI-env full services 803 passed (90.82% cov) — real ffmpeg testsrc integration ran (store + route, skipif absent); PG upgrade 0031->0032 idempotent (current=0032 head); OpenAPI probe 105 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 56/56; 31 new tests in test_render_pipeline.py (suite delta 777->803 CI-env).
- Land: worktree commit a27ddd3, merge 108397e into main; state advance 57/102, current -> CLIP-052 (captions/audio normalization rendering; next free migration 0033).

## 2026-10-12 - CLIP-052 captions/audio normalization/branding (single-orchestrator direct implementation)
- caption_tracks.py (migration 0033, 0032->0033 PG idempotent): CaptionSegment (frozen, ordered-window model validator end>start), validate_caption_segments pure vs render duration (non-empty, <=200 cues, 1..200 char text, in-window, sorted non-overlap), segments_to_srt deterministic SubRip HH:MM:SS,mmm.
- register_track: render project gate 404, duration from project spec, validation errors -> 422 (first 5), idempotent (org, track_id) / changed segments+style_ref+language conflict 409, audited caption_tracks.registered {segment_count, language}; routes: POST/GET /render-projects/{id}/caption-tracks (path/body id match 422), GET /caption-tracks/{id} (404).
- render_pipeline.py extension: escape_filter_path (\\ : quote), build_render_argv gained srt_path/logo_path kwargs — SRT merged into -vf chain (aspect,subtitles=filename='...'), logo switches to -filter_complex ([0:v]chain,format=yuv420p[base];[base][1:v]overlay=W-w-16:16[v]) + -map [v] + -map 0:a?; existing no-kwarg argv byte-identical (CLIP-051 tests unchanged).
- execute_job prep (pre-RUNNING -> failures stay retryable 422): captions enabled -> latest track (created_at,id desc) written workdir/<job_id>.srt and burned (no track -> clean render captions_burned=false); inputs.logo_ref workdir-relative confined, must exist, escape -> 422 job stays PENDING (verified by test); manifest += captions_burned, logo_overlaid; loudnorm continues (covered jointly in tests).
- Gates (observed): ruff format/check clean (TRY004 isinstance-condition rewrite, F821 CaptionSegment import fix, F401, I001); mypy clean 61 files (CI + --python-version 3.12); pytest SQLite 819+11 (90.51% cov); pytest CI-env full services 830 passed (90.89% cov) incl real-ffmpeg caption burn-in; PG upgrade 0032->0033 idempotent (current=0033 head); OpenAPI probe 107 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 57/57; 27 new tests (test_caption_tracks 15 + pipeline additions 12; suite delta 803->830 CI-env).
- Land: worktree commit c5154b0, merge 8d35894 into main; state advance 58/102, current -> CLIP-053 (platform aspect variants/QC/render manifests — FINAL M12 ticket; next free migration 0034).

## 2026-10-12 - CLIP-053 platform aspect variants/QC/render manifests; M12 COMPLETE (single-orchestrator direct implementation)
- render_variants.py: execute_variants fanout over spec outputs[] — per-output aspect override (default composition aspect) validated against {1:1,16:9,9:16} BEFORE RUNNING stamp (422, job stays PENDING), platform slug regex [A-Za-z0-9._-]+ (filename injection defense), output dir workdir/<job_id>/ confine-checked + mkdir; gates identical to execute_job (confined input, 404, 409 SUCCEEDED/RUNNING, prep 422); re-run deletes job's prior render_outputs + qc_reports rows first (clean retries).
- QC: probe_output (FFMPEG_BIN sibling FFMPEG... FFPROBE_BIN default ffprobe, 30s timeout, JSON parse) -> pure evaluate_qc (size>0, duration +/-15% of spec window, exact width/height vs EXPECTED_RESOLUTIONS) -> PASSED/FAILED persisted qc_reports uq (org, output) with checks JSON; probe errors become FAILED reports (fail-closed, no crash); any ffmpeg/QC failure -> job FAILED + error_ref, completed rows kept for diagnosis.
- Manifests: per-output deterministic manifest on render_outputs uq (org, job, platform) {fingerprint, argv, paths, captions_burned, logo_overlaid, returncode, completed_at}; job manifest aggregates {spec_fingerprint, variant_count, outputs summaries, completed_at}. Migration 0034 (0033->0034 PG idempotent). Audits: render_outputs.rendered, qc_reports.recorded + jobs succeeded/failed.
- Pipeline hardening (same change): prep_job_assets extraction shared by both execute paths; SRT write now confine-checked (fixed latent traversal via job ids); RenderJobRequest.job_id slug pattern ^[A-Za-z0-9._-]+$; build_render_argv aspect_override kwarg (backward compatible).
- Routes (3, CLIP-033 pattern): POST /render-jobs/{id}/variants/execute (query org_id+input_path), GET /render-jobs/{id}/outputs (404), GET /render-outputs/{id}/qc-report (null absent).
- Gates (observed): ruff format/check clean (TRY004 type() rewrites, I001); mypy clean 62 files (CI + --python-version 3.12); pytest SQLite 841+11 (90.48% cov); pytest CI-env full services 852 passed (90.86% cov) incl real-ffmpeg two-platform fanout (9:16 + 1:1) with real ffprobe QC; PG upgrade 0033->0034 idempotent (current=0034 head); OpenAPI probe 110 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 58/58; 22 new tests in test_render_variants.py (suite delta 830->852 CI-env).
- Test fix during gates: route 404 test used a nonexistent confined input (422 before job lookup — correct precedence); test now seeds an existing workdir input.
- Land: worktree commit 0cf78d0, merge d08ca8c into main; state advance 59/102, M12 COMPLETE (CLIP-050 DSL, 051 pipeline, 052 captions/branding, 053 variants/QC/manifests), current -> CLIP-054 (M13 voice/generated media begins; next free migration 0035).

## 2026-10-12 - CLIP-054 TTS provider adapter (M13 opener, single-orchestrator direct implementation)
- voice_assets.py: TTSAdapter Protocol + MockTTSAdapter (stdlib wave, 8kHz mono 16-bit silence, synthesis_duration_ms text/15cps clamped 500..15000, byte-stable per text, voice_ref/model_alias accepted for Protocol parity, no dispatch/network/vendor SDK).
- voice_assets table (migration 0035, 0034->0035 PG idempotent): uq (org, voice_asset_id); register idempotent identical / changed label+voice_ref+model_alias conflict 409; model_alias REQUIRED (pydantic min_length 1, rejected empty, never defaulted — registry alias seam for future provider dispatch); audited voice_assets.registered.
- synthesize_speech(adapter, store, voice_asset_id, request): asset 404, confine_path(RENDER_WORKDIR) escape 422, write OSError 422, audits voice_synthesis.completed {model_alias, bytes_written, duration_ms, output_path}; returns VoiceSynthesisResult.
- Routes (4, CLIP-033 pattern): POST/GET /voice-assets, GET /voice-assets/{id} (404), POST /voice-assets/{id}/synthesize (MockTTSAdapter injected at route — documented stand-in until real adapters register behind the Protocol).
- Gates (observed): ruff format/check clean (I001/F401 auto-fixes incl. drafting-artifact import removal); mypy clean 63 files (CI + --python-version 3.12); pytest SQLite 858+11 (90.61% cov); pytest CI-env full services 869 passed (90.98% cov); PG upgrade 0034->0035 idempotent (current=0035 head); OpenAPI probe 113 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 59/59; 17 new tests in test_voice_assets.py (suite delta 852->869 CI-env). One SSH disconnect mid-CI-env-run — recovered, gates re-observed green.
- Land: worktree commit fa648df, merge 21c2a8e into main; state advance 60/102 (HALFWAY), current -> CLIP-055 (image/video generation adapters; next free migration 0036).

## 2026-10-12 - CLIP-055 image/video generation adapters (single-orchestrator direct implementation)
- generation_adapters.py: ImageGenAdapter/VideoGenAdapter Protocols; MockImageGenAdapter (stdlib struct+zlib solid PNG, RGB=sha256(prompt)[:3], byte-identical per prompt); MockVideoGenAdapter (ffmpeg lavfi color 1..30s, argv-only, 60s timeout, failure -> ValueError -> 422); model_alias provenance-only, no dispatch/vendor identifiers.
- image_assets (migration 0036, 0035->0036 PG idempotent): uq (org, image_asset_id) + ix (org, brief); generate_image: confine first, idempotent identical return w/o regenerate / changed brief+prompt+alias+dims+relative-path conflict 409, brief gate 404, writes PNG, stores workdir-relative output_path, audited image_assets.generated.
- generate_video: confine first, existing READY VIDEO asset + v1 version -> conflict on changed title/storage_ref else return stored; else brief gate, mock mp4, register MediaAsset (VIDEO/READY) + AssetVersion v1 (storage_ref relative, bytes, sha256) through CLIP-038 store (monotonic max+1, audited by store), audited video_generations.generated.
- Routes (5, CLIP-033 pattern): POST/GET /image-generations (list brief-gated 404, get 404), POST /video-generations, GET /video-generations/{media_asset_id} (404 without asset or version).
- Fix during gates: list_versions takes media_asset_id only (CLIP-038 signature) — call-arg mypy errors corrected in flow+route+tests.
- Gates (observed): ruff format/check clean (I001/F401 auto-fixes incl. drafting-artifact imports removed); mypy clean 64 files (CI + --python-version 3.12); pytest SQLite 878+11 (90.69% cov); pytest CI-env full services 889 passed (91.05% cov); PG upgrade 0035->0036 idempotent (current=0036 head); OpenAPI probe 117 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 60/60; 20 new tests in test_generation_adapters.py (suite delta 869->889 CI-env).
- Land: worktree commit c9f99c9, merge 59be560 into main; state advance 61/102, current -> CLIP-056 (licensed stock/b-roll adapter; next free migration 0037).

## 2026-10-12 - CLIP-056 licensed stock/b-roll adapter interface (single-orchestrator direct implementation)
- Design correction during implementation: first draft defined its own source_items table + migration 0037 — collection failed with sqlalchemy InvalidRequestError (table already defined: M7 rights_entities owns content_sources + source_items since migration 0011). Redesigned onto the existing rights-entity chain; migration dropped (next free stays 0037); test_migrations source_items assertion retained (valid — table exists in chain).
- stock_adapter.py: StockClipAdapter Protocol + MockStockClipAdapter (sha1(provider_alias:normalized-query), exactly 3 CC0-1.0 1920x1080 items, limit 1..10 clamp, whitespace-normalized query, alias-separation); provider addressed ONLY via required provider_alias (registry alias, never defaulted, no vendor identifier).
- search_stock flow: deterministic org-embedded ids (stock_creator_id {org}:stock-provider:{alias}[:128], stock_source_id {org}:stock-feed:{alias}[:128] — creators/sources key on bare ids so org-embedding prevents cross-tenant collisions); ensures Creator (contact_ref vault://stock/{alias} matching secrets-ref regex) + PARTNER_FEED ContentSource (both idempotent via rights store); registers items via RightsEntitiesStore.register_item (idempotent (source_id, external_ref), changed title/license/dims/content_ref -> 409, content_ref stock://{alias}/{ref} <=512, license evidence in item metadata); audited stock_searches.completed {org, alias, source_id, query prefix, count} + rights store creator/source/item audits.
- Routes (2, CLIP-033 pattern): POST /stock-searches (returns persisted item views), GET /stock-items?org_id=&provider_alias= (source-derived 404 when never searched).
- Gates (observed): ruff format/check clean (I001/C408 auto-fixes); mypy clean 65 files (CI + --python-version 3.12); pytest SQLite 895+11 (90.73% cov); pytest CI-env full services 906 passed (91.09% cov); NO alembic change (zero migrations by design); OpenAPI probe 119 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 61/61; 14 new tests in test_stock_adapter.py (suite delta 889->906 CI-env). Test fix during gates: conflict test assumed 1 row but default limit persists 3 -> limit=1 both runs.
- Land: worktree commit 384d911, merge c847692 into main; state advance 62/102, current -> CLIP-057 (generated-media provenance/AI-label metadata — FINAL M13 ticket).

## 2026-10-12 - CLIP-057 generated-media provenance/AI-label metadata; M13 COMPLETE (single-orchestrator direct implementation)
- ai_labels.py (migration 0037, 0036->0037 PG idempotent): uq (org, label_id) + ix (org, asset_kind); MediaAssetKind AUDIO/IMAGE/VIDEO reused; LabelValue AI_GENERATED/AI_ASSISTED; required generator_alias registry ref (never defaulted, no vendor ids); deterministic_label_id {org}:ai-label:{sha1(kind:asset:alias)[:16]} — idempotent same-id / changed asset+generator+label+detail conflict 409; audited ai_labels.recorded.
- record_generated_media: brief pre-validated BEFORE any persist (fixed partial-state flaw found in review — label no longer persists when brief missing); registers label; when brief present appends CLIP-049 TransformationRecord (deterministic {org}:gen:{sha1[:16]}, from gen-prompt:{sha1(prompt)[:16]} or label fallback, to asset ref, method image-generation/video-generation, description prompt[:2048]).
- Auto-labeling (route-level — flows of CLIP-054/055 untouched): voice synthesize -> AUDIO label (relative_ref conversion); image generation -> IMAGE label + transformation (brief/prompt from stored row); video generation -> VIDEO label + transformation (brief/prompt from request). Explicit POST /generated-media-metadata for workflow-side registration (with optional brief).
- Routes (3, CLIP-033 pattern): POST /generated-media-metadata, GET /ai-labels (optional asset_kind filter via Annotated Query), GET /ai-labels/{label_id} 404.
- Fixes during gates: duplicate from __future__ import + unused uuid4 (ruff); B008 Query() default -> Annotated; conflict test corrected (deterministic id includes alias — same-id conflict needs label change not alias change); missing workdir fixture added; vacuous auto-label test replaced with real workdir roundtrip asserting label + transformation rows.
- Gates (observed): ruff format/check clean; mypy clean 66 files (CI + --python-version 3.12); pytest SQLite 910+11 (90.77% cov); pytest CI-env full services 921 passed (91.12% cov); PG upgrade 0036->0037 idempotent (current=0037 head); OpenAPI probe 122 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 62/62; 15 new tests in test_ai_labels.py (suite delta 906->921 CI-env).
- Land: worktree commit cc9ed61, merge 8a1f2c2 into main; state advance 63/102, M13 COMPLETE (CLIP-054 TTS, 055 image/video, 056 stock, 057 labels), current -> CLIP-058 (M14 publishing connectors begin; next free migration 0038).

## 2026-10-12 - CLIP-058 publishing adapter contract/private-test policy (M14 opener, CRITICAL priority)
- publishing.py: PublishingAdapter Protocol (health_check: AdapterHealth ok+capabilities+detail; publish: PublishRequest -> PublishResult external_id+status; get_status: PublishStatus). MockPublishingAdapter (sha1(idempotency_key)[:12] deterministic, ok health ['upload','status'], no network/vendor ids). Visibility enum PRIVATE/UNLISTED/PUBLIC. OperatingMode PRD set.
- evaluate_publish_policy pure fail-closed (no external calls, no DB): kill_switch denies EVERYTHING; OBSERVE/DRAFT mode -> deny all publishing; visibility outside allowed_visibilities -> deny; PRIVATE_TEST && PUBLIC -> deny (redundant with allowed_visibilities but defensive); PUBLIC requires BOTH in allowed set AND public_enabled=True.
- publish_policies (migration 0038, 0037->0038 PG idempotent): unique (org, policy_id), unique (org, platform) — one policy per platform; allowed_visibilities JSON default [PRIVATE, UNLISTED]; public_enabled default FALSE (PRD: disabled by default); kill_switch default FALSE; audited publish_policies.registered.
- publication_jobs: unique (org, idempotency_key) — row-insert time arbitration (repeat -> stored without adapter call, different fields -> 409 conflict). Adapter called OUTSIDE DB transaction (SQLite nested-lock safety). DENIED submissions: audited DENIED row, adapter never called (zero external side effect). FAILED adapter failures: error_ref stored, audited via same submitted action. operating_mode request-carried (M23 ops-config seam); public/kill enforcement is claim-independent.
- Routes (5, CLIP-033 pattern): POST/GET /publish-policies (get 404, list by org), POST /publications (DENIED->200 with status=DENIED job, SUCCEEDED->200, missing policy->404, idempotent key->return stored), GET /publications/{id} 404.
- Test bug fixed during gates: policy's default allowed_visibilities=[PRIVATE,UNLISTED] excludes PUBLIC — tests assuming public_enabled=True allows PUBLIC had to also set allowed_visibilities to include PUBLIC. Two-layer gate: (1) visibility in allowed set AND (2) PUBLIC+public_enabled — both required, tests corrected.
- submit_publication restructured: adapter call moved OUTSIDE with store._sessions.begin() (SQLite write-lock issue); read-only phase (idempotency replay + policy load), evaluate, adapter call (or DENIED), short write transaction with idempotency re-check at insert.
- Gates (observed): ruff format/check clean (I001 auto-fixes); mypy clean 67 files (CI + --python-version 3.12); pytest SQLite 934+11 (90.89% cov); pytest CI-env full services 945 passed (91.24% cov); PG upgrade 0037->0038 idempotent (current=0038 head); OpenAPI probe 126 paths; secret_scan clean; architecture_guard 6/6; state_audit verify 63/63; 25 new tests (suite delta 921->945 CI-env).
- Land: worktree commit a1f7733, merge 062854f into main; state advance 64/102, current -> CLIP-059 (YouTube publishing/status adapter — M14 real adapter tickets begin).

## 2026-10-12 - CLIP-059 YouTube publishing/status adapter (single-orchestrator direct implementation)
- YouTubePublishingAdapter: deterministic mock (health ok [upload,metadata,status,resumable], publish yt-mock-{sha1(key)[:10]} SUCCEEDED, get_status SUCCEEDED, no network/vendor ids) registered at import via publishing.register_adapter.
- publishing.py: ADAPTERS registry dict + register_adapter + get_adapter (Mock fallback); submit_publication route now selects adapter per request.platform.
- platform_accounts (migration 0039, 0038->0039 PG idempotent): uq (org, platform, account_ref) + ix (org, platform); scopes JSON + channel_ref + account_metadata; vault refs only (no secrets); audited platform_accounts.registered; 3 routes register/get/list (platform filter).
- Gates: ruff format/check clean; mypy clean 69 files; pytest 959 CI-env (91.29% cov); PG 0039 head; OpenAPI 128 paths; secret/arch_guard/state_audit 64/64; 25 new tests (suite delta 945->959); publish tests unbroken (YouTubeMock also SUCCEEDED with 'mock' in external_id).
- Land: worktree 0e3f83b, merge 11b3013; state advance 65/102, current -> CLIP-060 (TikTok adapter; M14 3/4).

## 2026-10-12 - CLIP-060 TikTok publishing/status adapter

**Ticket**: CLIP-060 (M14)
**Merge SHA**: 8c32b97
**Status**: COMPLETE (66/102)

**What was built**:
- TikTokPublishingAdapter: deterministic mock implementing PublishingAdapter Protocol
  - health_check: ok with capabilities [upload, privacy, status, creator_info]
  - publish: external_id = "tt-mock-{sha1(key)[:10]}", SUCCEEDED status
  - get_status: always SUCCEEDED
  - No network, no tiktok-businessapi SDK, no provider identifier
- Registered at module import via publishing.register_adapter("tiktok",...)
- No new migration (platform_accounts table 0039 already covers TikTok accounts)
- Publishing route selects adapter per request.platform via get_adapter fallback Mock

**Tests**: 15 new tests (adapter registration, health, publish determinism, platform isolation, route roundtrip, DENIED handling)
**Suite**: 969 passed CI-env (91.27% cov), 958+11 SQLite-only
**Gates**: ruff format/check clean, mypy 70 files clean, secret_scan clean, architecture_guard 6/6 OK, state_audit verify 65/65

**M14 progress**: CLIP-058 contract/policy + 059 YouTube + 060 TikTok done; 061 Meta remains.

## 2026-10-12 - CLIP-061 Meta publishing/status adapter

**Ticket**: CLIP-061 (M14)
**Merge SHA**: 016c45f
**Status**: COMPLETE (67/102) — M14 MILESTONE CLOSED

**What was built**:
- MetaPublishingAdapter: deterministic mock implementing PublishingAdapter Protocol
  - health_check: ok with capabilities [upload, page_publish, status, privacy]
  - publish: external_id = "meta-mock-{sha1(key)[:10]}", SUCCEEDED status
  - get_status: always SUCCEEDED
  - No network, no facebook-business SDK, no provider identifier
- Registered at module import via publishing.register_adapter("meta",...)
- No new migration (platform_accounts table 0039 already covers Meta accounts)
- Publishing route selects adapter per request.platform via get_adapter fallback Mock

**M14 milestone CLOSED**: All three platform adapters (YouTube, TikTok, Meta) now registered at import with deterministic mocks.

**Tests**: 9 new tests (adapter registration, health, publish determinism, platform isolation, route roundtrip, DENIED handling)
**Suite**: 978 passed CI-env (91.25% cov), 967+11 SQLite-only
**Gates**: ruff format/check clean, mypy 71 files clean, secret_scan clean, architecture_guard 6/6 OK, state_audit verify 67/67

**Note**: Meta developer documentation returned HTTP 400 at implementation time. Real adapter integration requires current-doc verification when credentials provisioned.

## 2026-10-12 - CLIP-062 Analytics provider contract

**Ticket**: CLIP-062 (M15)
**Merge SHA**: e1dd57f
**Status**: COMPLETE (68/102)

**What was built**:
- AnalyticsAdapter Protocol: provider-neutral analytics backend (health_check, collect_metrics)
- MockAnalyticsAdapter: deterministic stand-in returning VIEW/LIKE/COMMENT metrics
- MetricKind enum: VIEW, LIKE, COMMENT, SHARE, WATCH_TIME_MINUTES, IMPRESSION, CLICK_THROUGH
- analytics_metrics table (migration 0040, CREATE-only, idempotent per (org, content, kind, collected_at window))
- collect_analytics flow: calls adapter, persists normalized metrics, audited
- 2 routes: POST /analytics/collect, GET /analytics/metrics?org_id=&content_ref=&kind=

**Tests**: 13 new tests (mock adapter, store idempotency, collect flow, routes)
**Suite**: 991 passed CI-env (91.35% cov), 980+11 SQLite-only
**Gates**: ruff format/check clean, mypy 72 files clean, secret_scan clean, architecture_guard 6/6 OK, state_audit verify 68/68, PG migration 0039->0040 verified

## 2026-10-12 - CLIP-063 Native metric ingestion/reconciliation

**Ticket**: CLIP-063 (M15)
**Merge SHA**: b247eaa
**Status**: COMPLETE (69/102)

**What was built**:
- reconcile_metrics: collects via AnalyticsAdapter, compares against previous snapshot, records discrepancies
- ReconciliationStatus: MATCH, DISCREPANCY, NO_PREVIOUS
- Discrepancy model: kind, previous_value, current_value, delta, delta_pct
- reconciliation_records table (migration 0041, CREATE-only, unique record_id, ix (org, content_ref))
- ReconciliationStore with list/filter
- 2 routes: POST /reconciliation/run, GET /reconciliation-records?org_id=&content_ref=
- Audited metric_reconciliation.reconciled

**Tests**: 8 new tests (reconciliation no-previous, match, store CRUD, routes)
**Suite**: 999 passed CI-env (91.41% cov), 988+11 SQLite-only
**Gates**: ruff format/check clean, mypy 73 files clean, secret_scan clean, architecture_guard 6/6 OK, state_audit verify 69/69, PG migration 0040->0041 verified

## 2026-10-12 - CLIP-064 Normalized performance snapshots

**Ticket**: CLIP-064 (M15)
**Merge SHA**: 38932a9
**Status**: COMPLETE (70/102)

**What was built**:
- build_snapshot_fields: pure derivation, engagement_rate = (LIKE+COMMENT+SHARE)/VIEW*100 (0 when views missing/zero), per-kind counts default 0
- performance_snapshots table (migration 0042, CREATE-only, unique (org_id, content_ref), unique snapshot_id)
- Build flow: 404 when content_ref has no stored analytics metrics; idempotent re-derivation; changed fields -> 409
- 3 routes: POST /performance-snapshots, GET /performance-snapshots/{snapshot_id}?org_id=, GET /performance-snapshots?org_id=
- Audited performance_snapshots.recorded

**Interleaved work**: Jev System One decision tools (operator-requested) landed at d07359d before this ticket — jev_tools.py + 22 tests, ad-hoc feature, no state change.

**Tests**: 13 new tests
**Suite**: 1034 passed CI-env (91.56% cov), 1023+11 SQLite-only
**Gates**: ruff clean, mypy 75 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 70/70, PG migration 0041->0042 verified

## 2026-10-12 - CLIP-065 Analytics dashboards and scheduled collection

**Ticket**: CLIP-065 (M15)
**Merge SHA**: b71b9b5
**Status**: COMPLETE (71/102)

**What was built**:
- summarize_snapshots: pure org-level roll-up over performance snapshots (totals, avg engagement, top-5 deterministic)
- GET /analytics/dashboard?org_id=
- collection_schedules table (migration 0043, CREATE-only, unique (org, content_ref))
- run_due: injected clock, always advances schedule, fail-soft on adapter failure
- Routes: POST /collection-schedules, GET /collection-schedules?org_id=&enabled=, POST /collection-schedules/run-due
- Audits: collection_schedules.registered, collection_schedules.executed

**Tests**: 20 new tests (dashboards 8 + schedules 12)
**Suite**: 1054 passed CI-env (91.70% cov), 1043+11 SQLite-only
**Gates**: ruff clean, mypy 77 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 71/71, PG migration 0042->0043 verified

## 2026-10-12 - CLIP-066 Experiment/hypothesis data model

**Ticket**: CLIP-066 (M16)
**Merge SHA**: 56a1647
**Status**: COMPLETE (72/102)

**What was built**:
- experiments table (migration 0044, CREATE-only, unique (org, experiment_id), slug-validated ids, target_metric reuses MetricKind, starts DRAFT)
- hypotheses table (unique (org, hypothesis_id), linked to experiment with same-org enforcement, direction enum, one-shot resolution)
- Pure transition tables: experiment DRAFT->ACTIVE->COMPLETED->ARCHIVED (+DRAFT->ARCHIVED); hypothesis PROPOSED->SUPPORTED/REJECTED then frozen
- 6 routes: POST/GET /experiments, POST /experiments/{id}/transition, POST/GET /hypotheses, POST /hypotheses/{id}/transition
- Audits: experiments.registered, experiments.status_changed, hypotheses.registered, hypotheses.status_changed

**Tests**: 19 new tests (pure tables, store, routes, 404/409/422)
**Suite**: 1073 passed CI-env (91.75% cov), 1062+11 SQLite-only
**Gates**: ruff clean, mypy 78 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 72/72, PG migration 0043->0044 verified

## 2026-10-12 - CLIP-067 Deterministic assignment/variant orchestration

**Ticket**: CLIP-067 (M16)
**Merge SHA**: 3fc44e8
**Status**: COMPLETE (73/102)

**What was built**:
- experiment_variants table (migration 0045, CREATE-only, unique (org, experiment_id, variant_key), DRAFT-only registration)
- experiment_assignments table (unique (org, experiment_id, subject_ref))
- Pure deterministic_bucket (sha256 mod total weight) + select_variant (cumulative weights, lexical key order)
- Assignment requires experiment ACTIVE + >=2 variants (409); persisted assignments never move stored subjects
- 5 routes: POST/GET /experiments/{id}/variants, POST/GET /experiments/{id}/assignments, GET /experiments/{id}/variant-summary
- Audits: experiment_variants.registered, experiment_assignments.assigned

**Tests**: 18 new tests (pure bucketing, store lifecycle, routes, 404/409)
**Suite**: 1091 passed CI-env (91.85% cov), 1080+11 SQLite-only
**Gates**: ruff clean, mypy 79 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 73/73, PG migration 0044->0045 verified

## 2026-10-12 - CLIP-068 Result evaluation/business significance

**Ticket**: CLIP-068 (M16)
**Merge SHA**: 62b5e89
**Status**: COMPLETE (74/102)

**What was built**:
- evaluate_significance: pure Welch two-sample test (normal approximation), statistical gate (|z| >= 1.96) + business gate (|lift| >= 5%), winner only when both pass
- experiment_evaluations table (migration 0046, CREATE-only, unique (org, eval_id))
- Store: experiment existence 404, idempotent replay, changed inputs 409
- Routes: POST /experiments/{id}/evaluate, GET /experiments/{id}/evaluations
- Audited experiment_evaluations.recorded

**Tests**: 15 new tests (pure math 7, store 4, routes 4)
**Suite**: 1106 passed CI-env (91.95% cov), 1095+11 SQLite-only
**Gates**: ruff clean, mypy 80 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 74/74, PG migration 0045->0046 verified

## 2026-10-12 - CLIP-069 Strategy-rule canary/promotion/rollback

**Ticket**: CLIP-069 (M16)
**Merge SHA**: 46c2612
**Status**: COMPLETE (75/102)

**What was built**:
- strategy_rules table (migration 0047, CREATE-only, unique (org, rule_id), JSON parameters, starts DRAFT)
- Guarded lifecycle: DRAFT->CANARY->ACTIVE/ROLLED_BACK, ACTIVE->ROLLED_BACK, ROLLED_BACK->CANARY; cannot skip canary (409)
- 4 routes: POST /strategy-rules, GET /strategy-rules/{rule_id}, GET /strategy-rules?org_id=&status=, POST /strategy-rules/{rule_id}/transition
- Audits: strategy_rules.registered, strategy_rules.status_changed (actor/reason refs)

**Tests**: 13 new tests (pure lifecycle, store flow, routes, 404/409/422)
**Suite**: 1119 passed CI-env (92.03% cov), 1108+11 SQLite-only
**Gates**: ruff clean, mypy 81 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 75/75, PG migration 0046->0047 verified

## 2026-10-13 - CLIP-070 Revenue-source/ledger adapters

**Ticket**: CLIP-070 (M17)
**Merge SHA**: 93431f3
**Status**: COMPLETE (76/102)

**What was built**:
- RevenueAdapter Protocol + MockRevenueAdapter (deterministic 2 entries per fetch, sha1-derived refs/amounts)
- revenue_sources table (migration 0048, CREATE-only, unique (org, source_id), RevenueKind enum, vault-only account_ref)
- revenue_entries append-only ledger (integer amount_cents, currency, period window, unique (org, source, external_ref))
- sync flow: source 404, period validation 422, idempotent skip on repeat, audited per insert
- 5 routes: POST/GET /revenue-sources, GET /revenue-sources/{source_id}, POST /revenue-sync, GET /revenue-entries
- Audits: revenue_sources.registered, revenue_entries.recorded

**Tests**: 14 new tests (adapter determinism, source CRUD, sync idempotency, routes)
**Suite**: 1133 passed CI-env (92.17% cov), 1122+11 SQLite-only
**Gates**: ruff clean, mypy 82 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 76/76, PG migration 0047->0048 verified

## 2026-10-13 - CLIP-071 Affiliate link/click/conversion attribution

**Ticket**: CLIP-071 (M17)
**Merge SHA**: 2b43bb7
**Status**: COMPLETE (77/102)

**What was built**:
- affiliate_links, affiliate_clicks, affiliate_conversions tables (migration 0049, CREATE-only)
- Clicks/conversions append-only, idempotent, link existence enforced (404)
- Conversion click_ref attribution validated to same link (404)
- summarize_attribution: pure counts, conversion rate, revenue per currency (never mixed)
- 8 routes: POST/GET /affiliate-links, GET /{link_id}, GET /{link_id}/attribution, POST/GET /affiliate-clicks, POST/GET /affiliate-conversions
- Audits: affiliate_links.registered, affiliate_clicks.recorded, affiliate_conversions.recorded

**Tests**: 14 new tests (pure attribution, store CRUD, routes)
**Suite**: 1147 passed CI-env (92.29% cov), 1136+11 SQLite-only
**Gates**: ruff clean, mypy 83 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 77/77, PG migration 0048->0049 verified

## 2026-10-13 - CLIP-072 Sponsorship campaign attribution

**Ticket**: CLIP-072 (M17)
**Merge SHA**: a1c167d
**Status**: COMPLETE (78/102)

**What was built**:
- sponsor_campaigns, sponsor_deliverables, sponsor_attributions tables (migration 0050, CREATE-only)
- Deliverables: campaign existence 404, one-shot PENDING -> DELIVERED (audited)
- Attributions: campaign existence 404, integer money, path/body campaign match 422
- summarize_sponsorship: pure delivery rate, revenue per currency, variance vs contracted
- 8 routes: campaigns POST/GET/summary/attributions, deliverables POST/GET/transition
- Audits: sponsor_campaigns.registered, sponsor_deliverables.registered, sponsor_deliverables.delivered, sponsor_attributions.recorded

**Tests**: 14 new tests (pure summary, store flows, routes, 404/409/422)
**Suite**: 1161 passed CI-env (92.41% cov), 1150+11 SQLite-only
**Gates**: ruff clean, mypy 84 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 78/78, PG migration 0049->0050 verified

## 2026-10-13 - CLIP-073 Revenue dashboards/reconciliation workflows

**Ticket**: CLIP-073 (M17)
**Merge SHA**: b177701
**Status**: COMPLETE (79/102)

**What was built**:
- summarize_revenue: pure dashboard roll-up (per-currency totals, per-kind/source keyed key:currency — currencies never mixed)
- GET /revenue/dashboard with exact-window filters
- reconcile_revenue: pure recorded-vs-expected (tolerance MATCH/DISCREPANCY, zero-expected rule)
- revenue_reconciliations table (migration 0051, CREATE-only, unique (org, reconciliation_id))
- run_reconciliation: source 404, period 422, idempotent conflict 409, insert race re-check, audited
- 3 routes: GET /revenue/dashboard, POST/GET /revenue-reconciliations

**Tests**: 14 new tests (pure math, dashboard, store flows, routes)
**Suite**: 1175 passed CI-env (92.48% cov), 1164+11 SQLite-only
**Gates**: ruff clean, mypy 85 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 79/79, PG migration 0050->0051 verified

## 2026-10-13 - CLIP-074 Cost ledger and provider cost ingestion

**Ticket**: CLIP-074 (M18)
**Merge SHA**: d6865f6
**Status**: COMPLETE (80/102)

**What was built**:
- CostAdapter Protocol + MockCostAdapter (deterministic USAGE+STORAGE entries, sha1-derived)
- cost_sources table (migration 0052, CREATE-only, unique (org, source_id), generic cost_center slug)
- cost_entries append-only ledger (integer amount_cents, currency, period window, unique (org, source, external_ref))
- sync flow: source 404, period 422, idempotent skip, audited per insert
- 5 routes: POST/GET /cost-sources, GET /cost-sources/{source_id}, POST /cost-sync, GET /cost-entries
- Audits: cost_sources.registered, cost_entries.recorded
- Distinct from llm_cost.CostLedger (dispatch spend)

**Tests**: 13 new tests (adapter determinism, source CRUD, sync idempotency, routes)
**Suite**: 1188 passed CI-env (92.59% cov), 1177+11 SQLite-only
**Gates**: ruff clean, mypy 86 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 80/80, PG migration 0051->0052 verified

## 2026-10-13 - CLIP-075 Budgets/spend authorization

**Ticket**: CLIP-075 (M18)
**Merge SHA**: 9bff106
**Status**: COMPLETE (81/102)

**What was built**:
- budgets table (migration 0053, CREATE-only, unique (org, budget_id), integer caps, exact window, generic scope_key)
- Pure authorize_spend: fail-closed, boundary allowed, remaining_after None on deny
- Store: currency mismatch hard deny, spent = cost_entries summed by exact window + currency + cost_center scope
- spend_authorizations table (every decision persisted, audited evaluated)
- 5 routes: POST/GET /budgets, GET /{budget_id}, POST /{budget_id}/authorize, GET /{budget_id}/authorizations
- Audits: budgets.registered, spend_authorizations.evaluated

**Tests**: 15 new tests (pure math, store flows, scope filter, currency deny, routes)
**Suite**: 1203 passed CI-env (92.70% cov), 1192+11 SQLite-only
**Gates**: ruff clean, mypy 87 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 81/81, PG migration 0052->0053 verified

## 2026-10-13 - CLIP-076 Profitability/reinvestment calculations

**Ticket**: CLIP-076 (M18)
**Merge SHA**: d021d4d
**Status**: COMPLETE (82/102)

**What was built**:
- calculate_profitability: pure profit/margin (zero-revenue rules), profitable flag
- calculate_reinvestment: integer floor split, reinvest+reserve=profit, non-positive zeros
- profitability_reports table (migration 0054, CREATE-only, unique (org, report_id))
- run_report: sums revenue + cost ledgers exact window+currency, persists, audited
- 2 routes: POST/GET /profitability/reports
- Audited profitability_reports.recorded

**Tests**: 14 new tests (pure math, store flows, routes, 409/422)
**Suite**: 1217 passed CI-env (92.76% cov), 1206+11 SQLite-only
**Gates**: ruff clean, mypy 88 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 82/82, PG migration 0053->0054 verified

## 2026-10-13 - CLIP-077 Anomaly detection and finance approval gates

**Ticket**: CLIP-077 (M18)
**Merge SHA**: 759a92c
**Status**: COMPLETE (83/102) - M18 MILESTONE CLOSED

**What was built**:
- detect_cost_anomaly: pure population z-score over-cost detector (INSUFFICIENT_DATA below 2 baseline points, zero-variance rules)
- run_anomaly_check: scores a cost entry against source+category peers (migration 0055 cost_anomalies, audited recorded)
- finance_approvals: one-shot PENDING -> APPROVED/REJECTED lifecycle (audited submitted/resolved)
- evaluate_approval_gate: fail-closed (not_approved / currency_mismatch / amount_exceeds_approved), audited checked
- 6 routes: cost-anomalies check/list, finance-approvals submit/get/list/resolve/gate

**Milestone bookkeeping**: M15/M16/M17/M18 recorded in completed_milestones; current_milestone -> M19.

**Tests**: 19 new tests (pure detection, gate, store flows, routes)
**Suite**: 1236 passed CI-env (92.86% cov), 1225+11 SQLite-only
**Gates**: ruff clean, mypy 89 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 83/83, PG migration 0054->0055 verified

## 2026-10-13 - CLIP-078 Brand/channel templates

**Ticket**: CLIP-078 (M19)
**Merge SHA**: 1c4cb95
**Status**: COMPLETE (84/102)

**What was built**:
- brand_templates table (migration 0056, CREATE-only, unique (org, template_id), hex-validated palette, refs only)
- channel_templates table (Visibility from CLIP-058, hashtags validated, brand link 404 same-org)
- Contract validators: bad hex/hashtag/slug fail 422 pre-persistence
- 6 routes: POST/GET /brand-templates, GET /{template_id}, POST/GET /channel-templates (+platform), GET /{template_id}
- Audits: brand_templates.registered, channel_templates.registered

**Tests**: 12 new tests (validators, store flows, routes, 404/409/422)
**Suite**: 1248 passed CI-env (92.94% cov), 1237+11 SQLite-only (one unrelated flaky redis rate-limit test passed on retry)
**Gates**: ruff clean, mypy 90 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 84/84, PG migration 0055->0056 verified

## 2026-10-13 - CLIP-079 Channel incubation/launch experiments

**Ticket**: CLIP-079 (M19)
**Merge SHA**: e85b325
**Status**: COMPLETE (85/102)

**What was built**:
- incubation_runs table (migration 0057, CREATE-only, unique (org, run_id), brand/channel template links validated 404)
- Pure lifecycle: DRAFT -> INCUBATING -> LAUNCHED/SUNSET -> ARCHIVED (cannot skip incubating, ARCHIVED terminal)
- 4 routes: POST /incubation-runs, GET list +status, GET /{run_id}, POST /{run_id}/transition
- Audits: incubation_runs.registered, incubation_runs.status_changed

**Tests**: 14 new tests (pure lifecycle, store flows, template links, routes)
**Suite**: 1262 passed CI-env (93.01% cov), 1251+11 SQLite-only
**Gates**: ruff clean, mypy 91 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 85/85, PG migration 0056->0057 verified

## 2026-10-13 - CLIP-080 Portfolio allocation/quota engine

**Ticket**: CLIP-080 (M19)
**Merge SHA**: d09266a
**Status**: COMPLETE (86/102)

**What was built**:
- quota_policies table (migration 0058, CREATE-only, unique (org, quota_id), ACCOUNT/PLATFORM scope, rolling windows)
- evaluate_quota: pure gate (boundary allowed), check_quota counts publication_jobs with window+scope filters (audited)
- allocate_portfolio: pure largest-remainder allocator (deterministic, always sums exact)
- portfolio_allocations table (unique (org, allocation_id), idempotent, audited)
- 5 routes: POST/GET /quota-policies, POST /{quota_id}/check, POST/GET /portfolio-allocations
- Audits: quota_policies.registered, quota_checks.evaluated, portfolio_allocations.recorded

**Tests**: 16 new tests (pure allocator, quota gate, store flows, routes)
**Suite**: 1278 passed CI-env (93.11% cov), 1267+11 SQLite-only
**Gates**: ruff clean, mypy 92 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 86/86, PG migration 0057->0058 verified

## 2026-10-13 - CLIP-081 Brand lifecycle and shutdown/archive policies

**Ticket**: CLIP-081 (M19)
**Merge SHA**: c97b5a2
**Status**: COMPLETE (87/102) - M19 MILESTONE CLOSED

**What was built**:
- brand_lifecycles table (migration 0059, CREATE-only, one lifecycle per brand, guarded DRAFT->ACTIVE->SUSPENDED->RETIRED with resume)
- shutdown_policies table (one per brand, KEEP/UNLIST/SCHEDULE_DELETE intent, grace 0..90)
- 5 routes: POST/GET /brand-lifecycles, POST /{lifecycle_id}/transition, POST/GET /shutdown-policies
- Audits: brand_lifecycles.registered, brand_lifecycles.status_changed, shutdown_policies.registered

**Milestone bookkeeping**: M19 recorded in completed_milestones; current_milestone -> M20.

**Tests**: 11 new tests (pure lifecycle, store flows, routes, 404/409/422)
**Suite**: 1289 passed CI-env (93.18% cov), 1278+11 SQLite-only
**Gates**: ruff clean, mypy 93 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 87/87, PG migration 0058->0059 verified

## 2026-10-13 - CLIP-082 Improvement proposal evidence model

**Ticket**: CLIP-082 (M20)
**Merge SHA**: 049d7d9
**Status**: COMPLETE (88/102)

**What was built**:
- improvement_proposals table (migration 0060, CREATE-only, unique (org, proposal_id))
- Pure lifecycle: PROPOSED -> IN_REVIEW -> ACCEPTED/REJECTED, ACCEPTED -> IMPLEMENTED -> VERIFIED/ROLLED_BACK (terminal states)
- proposal_evidence table (append-only refs-only evidence links, proposal 404)
- 6 routes: POST/GET /improvement-proposals, GET /{proposal_id}, POST /{proposal_id}/transition, POST/GET /proposal-evidence
- Audits: improvement_proposals.registered, improvement_proposals.status_changed, proposal_evidence.linked

**Tests**: 12 new tests (pure lifecycle, store flows, evidence links, routes)
**Suite**: 1301 passed CI-env (93.27% cov), 1290+11 SQLite-only
**Gates**: ruff clean, mypy 94 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 88/88, PG migration 0059->0060 verified

## 2026-10-13 - CLIP-083 Agent Architect proposal generator

**Ticket**: CLIP-083 (M20)
**Merge SHA**: b5ab7b2
**Status**: COMPLETE (89/102)

**What was built**:
- derive_proposal_id / derive_evidence_id: pure sha1 deterministic identities
- generate_proposal: registers PROPOSED proposal, links evidence, created/replayed counts
- Replay-safe: same weakness returns stored proposal; reworded identity 409
- Route: POST /improvement-proposals/generate
- Audits: proposal_generation.generated (+ CLIP-082 per-row audits)
- No new tables, no LLM/network

**Tests**: 10 new tests (identity derivation, generation, replay, conflict, routes)
**Suite**: 1311 passed CI-env (93.28% cov), 1300+11 SQLite-only
**Gates**: ruff clean, mypy 95 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 89/89

## 2026-10-13 - CLIP-084 Eval sandbox and safe promotion

**Ticket**: CLIP-084 (M20)
**Merge SHA**: 89b7909
**Status**: COMPLETE (90/102)

**What was built**:
- eval_sandboxes table (migration 0061, CREATE-only, proposal link 404, required evaluator_alias, lifecycle CREATED->RUNNING->PASSED/FAILED re-runnable)
- promotions table (safe gate: PASSED sandbox + ACCEPTED/IMPLEMENTED proposal, every decision persisted)
- 5 routes: POST/GET /eval-sandboxes, POST /{sandbox_id}/transition, POST/GET /promotions
- Audits: eval_sandboxes.registered, eval_sandboxes.status_changed, promotions.evaluated

**Tests**: 13 new tests (pure lifecycle + gate matrix, store flows, routes)
**Suite**: 1324 passed CI-env (93.34% cov), 1313+11 SQLite-only
**Gates**: ruff clean, mypy 96 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 90/90, PG migration 0060->0061 verified

## 2026-10-14 - CLIP-085 Codex engineering-ticket generation/export

**Ticket**: CLIP-085 (M20)
**Merge SHA**: ca9ac0b
**Status**: COMPLETE (91/102) - M20 MILESTONE CLOSED

**What was built**:
- generate_packet_markdown: pure renderer, validator-clean # Codex Task Packet (tests assert validate_packet == [])
- engineering_tickets table (migration 0062, CREATE-only, unique (org, ticket_id), stores markdown packet)
- Proposal gate: must exist (404) and be ACCEPTED/IMPLEMENTED (409 not ready)
- Lifecycle: GENERATED -> EXPORTED -> IMPLEMENTED (audited generated + status_changed)
- Export: GET .../export returns text/markdown attachment
- 5 routes: POST/GET /engineering-tickets, GET /{ticket_id}, GET /{ticket_id}/export, POST /{ticket_id}/transition

**Milestone bookkeeping**: M20 recorded in completed_milestones; current_milestone -> M21.

**Tests**: 11 new tests (validator acceptance, pure lifecycle, store, routes)
**Suite**: 1347 passed CI-env (93.33% cov), 1336+11 SQLite-only
**Gates**: ruff clean, mypy 98 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 91/91, PG migration 0061->0062 verified

## 2026-10-15 - CLIP-086 Health supervisor signals

**Ticket**: CLIP-086 (M21)
**Merge SHA**: 99e2003
**Status**: COMPLETE (92/102)

**What was built**:
- health_signals table (migration 0063, CREATE-only, unique (org, signal_id), append-only, status + bounded detail + LIVE/OPERATOR source)
- run_live_probes: database (SELECT 1), cache (Redis PING), storage (MinIO health), workflow (Temporal port) - never raises, 2-3s timeouts, errors carry class name only
- summarize_signals: pure latest-per-subsystem + worst-rank overall (DOWN > DEGRADED > UNKNOWN > HEALTHY)
- 3 routes: POST /health/probe, GET /health/signals, POST /health/signals
- Audited: health_signals.recorded

**Tests**: 13 new tests (live probes, aggregation, store, routes)
**Suite**: 1359 passed CI-env (93.31% cov), 1348+11 SQLite-only
**Gates**: ruff clean, mypy 99 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 92/92, PG migration 0062->0063 verified

## 2026-10-15 - CLIP-087 Incident lifecycle and runbooks

**Ticket**: CLIP-087 (M21)
**Merge SHA**: a537a2a
**Status**: COMPLETE (93/102)

**What was built**:
- runbooks + runbook_steps (migration 0064, CREATE-only, ordered steps append-only, unique (org, runbook_id, step_no))
- incidents (unique (org, incident_id), severity SEV1-4, optional runbook category must match 409, opening seeds timeline)
- Pure lifecycle OPEN -> ACKNOWLEDGED -> MITIGATED -> RESOLVED -> CLOSED (dismiss + regression reopen, CLOSED terminal)
- incident_timeline append-only refs-only log (idempotent, audited)
- 8 routes: POST/GET /runbooks, GET /{id}, POST/GET /incidents (+status), GET /{id}, POST /{id}/transition, POST/GET /{id}/timeline

**Tests**: 15 new tests (pure lifecycle, runbook steps, incident flows, timeline, routes)
**Suite**: 1374 passed CI-env (93.38% cov), 1363+11 SQLite-only
**Gates**: ruff clean, mypy 100 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 93/93, PG migration 0063->0064 verified

## 2026-10-15 - CLIP-088 Safe auto-remediation actions

**Ticket**: CLIP-088 (M21)
**Merge SHA**: 8a31bf8
**Status**: COMPLETE (94/102)

**What was built**:
- remediation_actions table (migration 0065, CREATE-only, allow-listed action_kind + {param}-only template, max_attempts, requires_approval, enabled)
- Template validation: placeholder names + shell-metacharacter rejection before persistence (422)
- evaluate_remediation: pure injection-proof gate (kill switch > disabled > params > approval > rate cap)
- remediation_runs table (denied persisted with empty command, executor never invoked, FAILD/SUCCEEDED recorded)
- 4 routes: POST/GET /remediation-actions, POST /{action_id}/run, GET /remediation-runs

**Tests**: 22 new tests incl. injection-attempt table (; && | $() backticks newline overlong nested braces)
**Suite**: 1396 passed CI-env (93.43% cov), 1385+11 SQLite-only
**Gates**: ruff clean, mypy 101 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 94/94, PG migration 0064->0065 verified

## 2026-10-15 - CLIP-089 Credential/quota/disk/provider health monitors

**Ticket**: CLIP-089 (M21)
**Merge SHA**: 61ad68a
**Status**: COMPLETE (95/102) - M21 MILESTONE CLOSED

**What was built**:
- Four pure monitors: credential expiry bands (vault credential_ref only, never secrets), quota headroom (limit_invalid guard, no division), disk usage (capacity unknown DOWN, disk full at 100%), provider health boolean mapping
- evaluate_monitor dispatcher: 422 when the measurement required by the kind is missing
- monitor_checks table (migration 0066, CREATE-only, append-only, unique (org, check_id))
- summarize_monitors: latest per kind + worst-rank overall + attention list of every non-HEALTHY monitor
- 3 routes: POST /monitors/check, GET /monitors, GET /monitors/checks (+kind filter)

**Milestone bookkeeping**: M21 recorded in completed_milestones; current_milestone -> M22.

**Tests**: 18 new tests (band boundaries, invalid limits, dispatcher errors, aggregation, routes)
**Suite**: 1412 passed CI-env (93.49% cov), 1401+11 SQLite-only
**Gates**: ruff clean, mypy 102 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 95/95, PG migration 0065->0066 verified

## 2026-10-15 - CLIP-090 Threat model and abuse cases

**Ticket**: CLIP-090 (M22)
**Merge SHA**: 99da055
**Status**: COMPLETE (96/102)

**What was built**:
- threats + threat_controls + threat_mitigations + abuse_cases (migration 0067, CREATE-only, unique identities)
- STRIDE category enum, severity, abuse-case narrative, review lifecycle IDENTIFIED -> MITIGATED/ACCEPTED with reopen
- Pure coverage audit: covered / unevidenced / uncovered using importlib spec resolution so a claim must point at real code
- Mitigations map control_ref + evidence_ref (append-only, conflict 409); abuse cases carry attack_surface + detection_ref + threat link
- 8 routes: POST/GET /threats (+category), GET /{id}, GET /coverage, POST /{id}/mitigations, POST /{id}/transition, POST/GET /abuse-cases

**Tests**: 16 new tests (lifecycle, coverage verdicts incl. fake/real module and path-shaped evidence, store flows, routes)
**Suite**: 1428 passed CI-env (93.57% cov), 1417+11 SQLite-only
**Gates**: ruff clean, mypy 103 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 96/96, PG migration 0066->0067 verified

## 2026-10-15 - CLIP-091 Prompt-injection hardening and tool authorization

**Ticket**: CLIP-091 (M22)
**Merge SHA**: e901fde
**Status**: COMPLETE (97/102)

**What was built**:
- scan_untrusted_text: six labelled detector classes (instruction override, role assumption, tool-grant attempt, exfiltration request, delimiter escape, hidden characters)
- neutralize_untrusted_text: offset-safe labelling of matched spans; control chars stripped last
- enforce_untrusted_boundary: fail-closed, never raises (SCANNER_ERROR / QUARANTINED / NEUTRALIZED / ALLOWED)
- authorize_tool_from_content: content_cannot_grant_tools for anything outside OPERATOR/POLICY/SYSTEM
- injection_events table (migration 0068, content-free: counts/decision/reason/severity/chars, never raw text)
- 4 routes: POST /security/injection/scan, POST /security/injection/enforce, GET /security/injection/events, POST /security/tool-authorization

**Bug caught by tests**: hidden-character deletion by range shifted offsets and removed a legitimate character; neutralizer now labels spans first and strips control characters last.

**Tests**: 25 new tests incl. benign false-positive check, all six classes, threshold tuning, scanner-failure fail-closed, tool-auth matrix, audit-privacy assertion
**Suite**: 1453 passed CI-env (93.64% cov), 1442+11 SQLite-only
**Gates**: ruff clean, mypy 104 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 97/97, PG migration 0067->0068 verified

## 2026-10-15 - CLIP-092 Security/tenant/fuzz/dependency tests

**Ticket**: CLIP-092 (M22)
**Merge SHA**: 30c3c32
**Status**: COMPLETE (98/102)

**What was built** (test-only: no new module, no migration):
- test_security_tenant.py: deny-by-default membership (unknown user/org, suspended org, removed member), final-active-owner floor, cross-tenant isolation across 8 stores
- test_fuzz_security.py: 26-item hostile corpus + constant-seeded PRNG; boundary never raises, CRITICAL always quarantined, no 500 on hostile HTTP input, safe-charset exactly enforced
- test_dependency_security.py: bounded deps, no non-PyPI index/pip config, CI lanes intact, non-root container, no docker.sock, in-process secret scan

**Defects found and fixed while writing the suites**: Organization exposes .id not .org_id; store tables needed the main import before create_all; hidden-character deletion by range shifted offsets (fixed in CLIP-091's neutralizer).

**Tests**: 107 new tests
**Suite**: 1560 passed CI-env (93.67% cov), 1549+12 SQLite-only
**Gates**: ruff clean, mypy 104 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 98/98, OpenAPI unchanged at 316 paths

## 2026-10-15 - CLIP-093 Production security qualification

**Ticket**: CLIP-093 (M22)
**Merge SHA**: cc793ca
**Status**: COMPLETE (99/102) - M22 MILESTONE CLOSED

**What was built**:
- scripts/security_qualification.py: 11 controls checked against a LIVE instance over HTTP (reachability, operator health privacy, OBSERVE denial, PUBLIC-refused-by-default, rights gate, injection quarantine + false-positive guard, tool authorization, remediation kill switch, audit growth, secret scan)
- Versioned JSON report with summary, per-check evidence, and findings (FAIL=HIGH, WARN=LOW)
- Exit codes 0 pass / 1 FAIL (or WARN under --strict) / 2 unreachable, so a broken environment is never mistaken for a pass
- Live run against the current service: VERDICT PASS 11/11

**Finding**: an initial run against the pre-CLIP-091 server correctly reported FAIL on C6/C7/C8/C10, demonstrating the harness detects stale/broken targets rather than rubber-stamping them.

**Milestone bookkeeping**: M22 recorded in completed_milestones; current_milestone -> M23.

**Tests**: 10 new tests incl. two deliberate-regression tests
**Suite**: 1570 passed CI-env (93.81% cov), 1559+12 SQLite-only
**Gates**: ruff clean, mypy 104 files clean, secret_scan clean, arch_guard 5/5, state_audit verify 99/99


## 2026-10-15 - CLIP-094 Hardened production Compose/staging topology

**Ticket**: CLIP-094 (M23 opener)
**Merge SHA**: fcebb64
**Status**: COMPLETE (100/102) - M23 IN PROGRESS

**What was built**:
- deploy/compose.production.yml and deploy/compose.staging.yml: hardened staging and production stacks
- Two bridges: clipos_edge faces the host; clipos_internal declared internal: true so postgres/redis/minio/temporal have no route off the host. Only the edge service publishes host ports (80/443 production, 8080 staging)
- Immutable digest-pinned images, non-'no' restart policies, healthchecks, cpu/memory limits, read-only app rootfs with tmpfs /tmp, secret-by-reference configuration
- deploy/Caddyfile (TLS, security headers) and deploy/Caddyfile.staging (plain HTTP, same header/upstream policy) so staging mirrors production service-for-service
- deploy/env.production.example and deploy/env.staging.example: variable names only, committed; filled .env.* is gitignored and secret-scanned
- services/api/clipos_api/deployment_topology.py: hardening invariants H1..H8 as pure functions over a parsed Compose mapping (one-shot init containers exempt from H3/H4/H6)
- scripts/deploy_topology_check.py: validates both topologies plus a staging/production mirror check; versioned JSON report; exit 0 pass / 1 FAIL or mirror drift / 2 unreadable; wired into the CI validate job
- pyyaml>=6.0 added to services/api deps (the CI test job installs only services/api[dev])

**Invariants**: H1 internal services not host-published; H2 no dev credential marker; H3 restart policy; H4 healthcheck; H5 no privileged/docker.sock; H6 resource limits; H7 no secret-like literal; H8 reference-only keys injected as ${VAR}

**Note**: the module deliberately holds no yaml import so mypy behaves identically whether or not types-PyYAML is installed; parsing lives in the CLI.

**Tests**: 26 new tests (pure invariants incl. regression cases, CLI harness, real repo topologies)
**Suite**: 1596 passed CI-env (93.85% cov), 1584 passed +12 skipped SQLite-only
**Gates**: ruff format+check clean, mypy clean over baseline (2 pre-existing unused-ignore errors), alembic at head, secret_scan clean, arch_guard 6/6 OK, state_audit verify 99/99


## 2026-10-16 - CLIP-095 CI/CD canary and rollback

**Ticket**: CLIP-095 (M23)
**Merge SHA**: ab6ffb7
**Status**: COMPLETE (101/102) - M23 IN PROGRESS

**What was built**:
- services/api/clipos_api/deployments.py: deployments and release_evidence persistence, pure rollout predicates, and the routes
- Immutable deployment identity: (commit SHA, image digest) pair, org-scoped, idempotent per (org, deployment_id); re-registering with a different SHA or digest is a 409, not an update
- Rollout state machine (pure): PENDING -> CANARY -> PROMOTED; FAILED from PENDING/CANARY; ROLLED_BACK from CANARY/PROMOTED/FAILED; ROLLED_BACK terminal, same-state no-op, else 409
- Promotion gate health_window_passed: non-empty window, every observation HEALTHY — promoted on evidence, not silence
- Rollback: one-command image re-pin enforcing expand/contract via rollback_conflicts; a target predating migrations this release applied is refused 409 naming the migration; self-rollback refused; backup restore deliberately not modelled (disaster recovery, not rollback)
- release_evidence: append-only, idempotent per (org, deployment_id), carrying the exact §Release evidence fields; rolled_back_at stamped when already ROLLED_BACK
- Audited deployments.registered, deployments.status_changed, deployments.rolled_back, release_evidence.recorded

**Migration**: 0069 (CREATE-only: deployments, release_evidence); applied on Postgres 0068 -> 0069

**Routes**: POST/GET /deployments, GET /deployments/{id}, POST /deployments/{id}/canary, /promote, /fail, /rollback, POST/GET /deployments/{id}/evidence (OpenAPI 243 paths, 7 new)

**Tests**: 31 new tests
**Suite**: 1628 passed CI-env (93.86% cov), 1 skipped
**Gates**: ruff format+check clean, mypy clean (106 source files), secret_scan clean, arch_guard 6/6 OK, deploy_topology_check PASS, state_audit verify 100/100


## 2026-10-16 - CLIP-096 Encrypted backup/restore verification

**Ticket**: CLIP-096 (M23)
**Merge SHA**: 3697e34
**Status**: COMPLETE

**What was built**:
- services/api/clipos_api/backup_crypto.py: stdlib-only authenticated-encryption envelope
- SHA-256 counter-mode keystream XORed with the plaintext, then encrypt-then-MAC (HMAC-SHA256 over the canonical header AND the ciphertext, so header tampering is caught too)
- Purpose-separated subkeys SHA256(label||key); constant-time tag comparison via hmac.compare_digest
- Envelope framing MAGIC|version|header_len|header(JSON)|ciphertext with explicit alg + version fields for migration
- Master key from CLIPOS_BACKUP_KEY (64 hex); never written to disk, never in a manifest or audit event, never logged — only an opaque key_ref (the variable NAME) and a truncated fingerprint are persisted
- Empty plaintext refused rather than producing an unverifiable "successful" artifact
- services/api/clipos_api/backup_verification.py: backup_artifacts (org-scoped, idempotent per (org, artifact_id), drift 409) and restore_tests (append-only, idempotent per (org, test_id))
- Restore outcome is COMPUTED from observations via evaluate_restore, never trusted from the caller
- Pure predicates: evaluate_restore (wrong-bytes distinct from byte-correct-but-over-RTO), check_backup_freshness (daily cadence -> HEALTHY/DEGRADED/DOWN), restore_test_due (weekly), evaluate_drill_cadence (monthly), summarize_backup_status
- Audited backup_artifacts.recorded, restore_tests.recorded

**Honesty note (recorded in the module docstring and docs/development.md)**: this is a correct encrypt-then-MAC AEAD assembled from a hash and a MAC, NOT a substitute for a vetted AEAD. The repository carries no third-party crypto dependency, so the envelope is stdlib-only by necessity; a future revision should switch `alg` to AES-GCM or ChaCha20-Poly1305, keeping version 1 readable. No claim of side-channel resistance beyond constant-time tag comparison.

**Migration**: 0070 (CREATE-only: backup_artifacts, restore_tests); applied on Postgres 0069 -> 0070

**Routes**: POST/GET /backups/artifacts, GET /backups/artifacts/{id}, POST/GET /backups/restore-tests, GET /backups/status (OpenAPI 247 paths, 4 new)

**Tests**: 49 new tests
**Suite**: 1677 passed CI-env (93.90% cov), 1 skipped
**Gates**: ruff format+check clean, mypy clean (108 source files), secret_scan clean, arch_guard 6/6 OK, deploy_topology_check PASS, state_audit verify 101/101


## 2026-10-17 - CLIP-097 Production bootstrap/secrets/TLS/runbook (M23 CLOSED)

**Ticket**: CLIP-097 (M23 closer)
**Merge SHA**: 39ff9a9
**Status**: COMPLETE - M23 MILESTONE CLOSED

**What was built**:
- services/api/clipos_api/production_bootstrap.py: contracts, pure validators, store, routes
- Bootstrap manifests declare environment, secret REFERENCES (name -> reference form), TLS material references and a domain — never a value
- Required secrets (DATABASE_URL, POSTGRES_PASSWORD, CACHE_REDIS_URL, STORAGE_ACCESS_KEY, STORAGE_SECRET_KEY, MINIO_ROOT_PASSWORD) must use an accepted scheme: ${VAR}, vault://, sops://, aws-sm://, gcp-sm://, azure-kv://
- A bare literal is SECRET_NOT_A_REFERENCE; anything secret-shaped is SECRET_LITERAL — both blocking. The store refuses a manifest carrying a secret-shaped literal outright, so a value cannot reach persistence even by mistake
- TLS: material must be referenced (never inline PEM), certificate and key must not share a reference, self-signed refused for production (permitted in staging)
- evaluate_bootstrap_readiness returns human_gates_remaining (production secret import, TLS issuance) alongside the verdict, so READY never reads as "safe to deploy unaided"
- rotation_gaps is pure; a validation checking a connector list raises each rotation-less connector as blocking ROTATION_MISSING, per the spec requirement that a rotation procedure exists for every connector. cadence_days is DERIVED from the RotationCadence enum, not supplied
- Audited bootstrap_manifests.registered, bootstrap_validations.recorded, secret_rotations.registered
- docs/PRODUCTION_BOOTSTRAP.md: operator runbook (inventory, TLS, rotation, validation, bring-up order, and an explicit "what the automated checks do not do")

**Fixed during the ticket**:
- /production/readiness became a POST: a readiness evaluation over a full declaration (including a secret_refs mapping) has no sensible query-string encoding, and FastAPI rejects a `dict` as a query parameter
- The manifest drift check was widened to cover domain_ref and tls_self_signed, which are part of the manifest identity

**Migration**: 0071 (CREATE-only: bootstrap_manifests, bootstrap_validations, secret_rotations); applied on Postgres 0070 -> 0071
**Routes**: POST/GET /production/bootstrap, GET /production/bootstrap/{manifest_id}, POST/GET /production/bootstrap/validations, POST/GET /production/rotations, POST /production/readiness (OpenAPI 252 paths, 5 new)

**Milestone bookkeeping**: M23 recorded in completed_milestones; current_milestone -> M24.

**Tests**: 45 new tests
**Suite**: 1726 passed CI-env (93.94% cov), 1 skipped
**Gates**: ruff format+check clean, mypy clean (109 files; 2 pre-existing baseline unused-ignore errors only), secret_scan clean, arch_guard 6/6 OK, deploy_topology_check PASS, state_audit verify 102/102


## 2026-10-17 - CLIP-098 End-to-end qualification in private mode (M24 opened)

**Ticket**: CLIP-098 (M24 opener)
**Merge SHA**: 3ea0479
**Status**: COMPLETE

**What was built**:
- scripts/private_mode_qualification.py: the M24 counterpart to the CLIP-093 security harness
- Pipeline walk: 28 steps across 8 stages (research, planning, produce, render, publish, analytics, revenue, experiments) against a LIVE instance over HTTP. A required step failing fails the qualification; optional steps are recorded as skipped; every step's observed HTTP status is captured
- Six private-mode invariants checked independently of how far the walk got: P1 OBSERVE denies all publishing; P2 PRIVATE_TEST cannot publish PUBLIC; P3 PUBLIC refused while public_enabled is false; P4 rights gate refuses uncleared content; P5 active kill switch denies a normally-allowed private post; P6 no secret-shaped material in any observed response
- Each policy-registering invariant uses its OWN platform: policy is unique per (org, platform), so sharing one would let an earlier probe's policy decide a later probe's outcome
- P5 exercises the real enforcement path (a policy with kill_switch True) rather than a standalone endpoint, since the control lives on the policy
- Versioned machine-checkable report (summary, per-stage counts, every step, every invariant, findings); exit 0 pass / 1 any required-step or invariant failure / 2 unreachable

**Findings on the first live run — all HARNESS defects, not product defects** (chasing that distinction was the substance of this work):
1. P3 "PUBLIC succeeded with public_enabled=False" looked like a real policy bypass. Cause: state bleed — P2 had already registered a public_enabled=True policy for the same (org, platform). Reading the evaluator on current main confirmed it does enforce public_disabled_by_default. Fixed by giving each invariant its own platform.
2. P1 "-> 404" was a server started days earlier from an older build. Restarting from current main gave DENIED (mode_OBSERVE_denies_publishing).
3. Render 422 was a genuine harness bug: inputs.candidate_id is required (1..128 chars). The walk now extracts clip candidates first and references a real candidate id.
Also fixed: per-stage counts always rendered zero because the aggregation keyed on status.lower() ("pass") against passed/failed/skipped buckets.
Operational note: two scp transfers silently failed mid-ticket, serving stale content and producing misleading runs. Verified on the server after every transfer.

**Live result**: VERDICT PASS — 28 steps / 0 failed; 6 invariants / 0 failed; 38 responses scanned, no secret-shaped material.

**Tests**: 11 new tests, including three deliberate-regression tests that patch evaluate_publish_policy and prove P1/P3/P5 flip the verdict to FAIL
**Suite**: 1737 passed CI-env (94.00% cov), 1 skipped
**Gates**: ruff format+check clean (API and script), mypy clean (109 files), secret_scan clean, arch_guard 6/6 OK
**Migration**: none

**Milestone bookkeeping**: M24 opened; current_milestone = M24.


## 2026-10-17 - CLIP-099 Owner-gated public publishing (HUMAN GATE)

**Ticket**: CLIP-099 (M24) - the ONLY human-gated ticket in the build
**Merge SHA**: 43de2a9
**Status**: COMPLETE (guard built and qualified; no real public post made)

**Gate handling**: public posting to a real platform account is the owner's act. Per the
operator's direction this ticket was built and tested against the Mock adapter only, with
`public_enabled` false and `public_publishing` false at deployment level. Nothing in this
repository posted publicly to a real account; `human_gates.first_public_post` remains false.

**What was built**:
- services/api/clipos_api/public_publishing_gate.py: owner approvals, private-test records, the pure gate, and routes
- owner_approvals: org-scoped, idempotent per (org, approval_id), binds one (org, platform, account_ref) to an explicit owner decision carrying the private-test evidence it follows, an owner reference and an optional expiry; revocable as an append-only status change
- private_tests: private-test outcomes per account
- evaluate_public_gate: pure, fail-closed; requires a granted + un-revoked + un-expired approval AND a successful private test for that same account, each missing precondition with its own reason code
- Per real platform account, so approving one channel cannot unlock another; a re-granted approval supersedes an earlier revoked one; private/unlisted visibility never consults the gate
- Enforcement in evaluate_publish_policy via account_approved: bool = False (fail-closed: omitting the gate denies publication), with the route resolving the decision from the gate store so the policy function stays pure

**Blast radius**: four pre-existing PUBLIC tests now correctly receive DENIED and were updated to assert what they actually test; two new tests pin the tightened contract. CLIP-098 harness patches updated for the new parameter; CLIP-093 security qualification unchanged and passing.

**Migration**: 0072 (CREATE-only: owner_approvals, private_tests); applied on Postgres 0071 -> 0072
**Routes**: POST/GET /publishing/owner-approvals, POST /publishing/owner-approvals/{approval_id}/revoke, POST/GET /publishing/private-tests, GET /publishing/gate-summary

**Tests**: 34 new tests
**Suite**: 1765 passed CI-env (94.01% cov), 1 skipped
**Gates**: ruff format+check clean from the repo root (299 files), mypy clean (110 files), secret_scan clean, arch_guard 6/6 OK, deploy_topology_check PASS, state_audit verify 104/104

**Note for reviewers**: services/api/clipos_api/publishing.py shows large line churn in this
diff; the semantic change is 28 insertions / 5 deletions (verify with `git diff -w`), the
remainder being ruff format reflow of pre-existing long lines.


## 2026-10-19 - CLIP-100 SLOs/cost caps/kill switches/incident drills

**Ticket**: CLIP-100 (M24)
**Merge SHA**: 46b24d8
**Status**: COMPLETE (106/108)

**What was built**:
- services/api/clipos_api/slo_qualification.py: the missing SLO concept plus one operator-visible verdict over the operational controls
- slo_objectives: org-scoped, idempotent per (org, objective_id); indicator slug, target in basis points (integer, never float), window in days, minimum sample count
- evaluate_slo: pure, three statuses MET / BREACHED / INSUFFICIENT_DATA - insufficient data fails the verdict and is reported as SLO unmeasured, never as a pass
- verify_cost_cap: takes the budgets authorizer as a parameter and fails in both directions (over-cap allowed is broken; within-cap refused is broken)
- ops_drills: append-only, idempotent per (org, drill_id), one exercised control per row with an evidence reference
- qualify_operations: aggregate PASS/FAIL over REQUIRED_CONTROLS = (kill_switch_closed, cost_cap_refused, incident_drill); an unexercised control appears in controls_unverified and fails the verdict
- Audited slo_objectives.registered and ops_drills.recorded

**Migration**: 0073 (CREATE-only: slo_objectives, ops_drills); applied on Postgres 0072 -> 0073
**Routes**: POST/GET /slo/objectives, GET /slo/objectives/{objective_id}, POST /slo/objectives/{objective_id}/evaluate, POST/GET /slo/drills, GET /slo/qualification

**Tests**: 34 new tests
**Suite**: 1799 passed CI-env (94.02% cov), 1 skipped
**Gates**: ruff format+check clean (302 files), mypy clean (111 files), secret_scan clean, arch_guard 6/6 OK, deploy_topology_check PASS, state_audit verify 105/105

**Environment fixes**: compose stack had stopped (Postgres refused connections) and was restarted; a pytest run executed as root left /tmp/clipos-100-slo-audit.log root-owned, which made the next clipos-user run fail with PermissionError - the suite must always run as clipos.


## 2026-10-20 - CLIP-101 Autonomy evidence and operations manual

**Ticket**: CLIP-101 (M24)
**Merge SHA**: a90bb9e
**Status**: COMPLETE (107/108)

**What was built**:
- services/api/clipos_api/autonomy_evidence.py: typed inputs + pure nine-check assembler (E1 ledger, E2 audit coverage, E3 evidence trail, E4 milestones, E5 operating mode, E6 publishing posture, E7 human-gate declarations, E8 harness inventory, E9 CI record)
- scripts/autonomy_evidence.py: CLI gathering the real record; versioned JSON report; exit 0 pass / 1 FAIL (or warning under --strict) / 2 unreadable
- docs/OPERATIONS_MANUAL.md: consolidated operator manual (health/status, verification commands, deployment lifecycle, publishing governance and the owner gate, incidents/remediation/drills, cost control, backups, secrets and rotation, human gates, what the automation does not do)

**Honesty rules enforced by code**:
- An uncheckable claim is UNKNOWN and flagged, never silently healthy (state_audit failing to run yields coverage UNKNOWN with a WARN)
- public_publishing=true while the first_public_post owner gate is closed is a contradiction that FAILS the report (PRD section 7 ties autonomous public operation to owner evidence)
- Open human gates are reported as facts without penalty

**Found on its first real run** (the harness working as designed):
- tickets_completed=106 vs tickets_total=102 — the six P001..P006 pre-tickets were folded into the ledger without updating the declared total (validate_handoff counts 108 ids; nothing asserted the total). Corrected to 108.
- Parser bug: state_audit prints audit: N/M covered; a bare isdigit token scan never matches the fraction, so coverage would have been UNKNOWN forever. Now parses the fraction.

**Migration**: none. **Routes**: none — evidence derives from state the build already records.

**Tests**: 23 new tests
**Suite**: 1825 passed CI-env (94.04% cov), 1 skipped
**Gates**: ruff clean (API + script), mypy clean (112 files), secret_scan clean, arch_guard 6/6 OK, deploy_topology_check PASS


## 2026-10-20 - CLIP-102 v1.0.0 release freeze (M24 CLOSED — BUILD COMPLETE)

**Ticket**: CLIP-102 (M24 closer — final ticket)
**Merge SHA**: 42e3fed
**Status**: COMPLETE (108/108) — M24 MILESTONE CLOSED — v1.0.0 FROZEN

**What was built**:
- release/v1.manifest.json: release 1.0.0; complete 108-ticket ledger; all milestones M0..M24; six qualification harnesses; all five human gates documented AND listed under deferred_to_post_launch (F6 enforces the deferred list equals the documented list)
- scripts/release_freeze.py: stdlib-only validator F1..F7 (manifest completeness, package-version agreement, ledger, milestones, harness presence, honest deferral, backlog); versioned JSON report; exit 0/1/2
- services/api version 0.1.0 -> 1.0.0 (GET /health)
- docs/BACKLOG.md: P0 (human gates, provider credentials, CI wiring for schema_drift/autonomy_evidence), P1 (registry digests, production bootstrap run, DR drill, real SLO measurements, alert routing, adapter certification), P2 (vetted-AEAD backup envelope, multi-tenant tests, performance)
- Terminal build state: current_ticket=NONE, current_milestone=NONE, status=M24_COMPLETE; validate_state.py exempts the NONE sentinel from the must-exist-in-YAML check; validate_handoff.py accepts NONE only while zero backlog tickets remain outside completed_tickets

**Bugs caught during the freeze**:
- validate() read the manifest from a module-level constant path, so --repo mutations were invisible — caught by the mutation test deleting a manifest key with no resulting failure
- A PowerShell write added a UTF-8 BOM to pyproject.toml; pytest's TOML parser refused the file

**Verification**: release_freeze VERDICT PASS on the frozen record; autonomy_evidence PASS; state_audit 108/108 covered (CLIP-102 pinned to evidence ref v1.0.0); 12 new tests; 1837 passed CI-env (94.05% cov)

**Milestone bookkeeping**: M24 recorded in completed_milestones. The autonomous build M-1..M24 is COMPLETE: 108/108 tickets, 25 milestones, CI green.

**Remaining (all owner acts, see docs/BACKLOG.md)**: production_secrets; real YouTube/TikTok/Meta accounts; first_public_post. The system operates in DRAFT with public publishing disabled until the owner executes them.
