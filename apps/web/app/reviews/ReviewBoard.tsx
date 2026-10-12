"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import type {
  ClipCandidateStatus,
  ClipCandidateView,
  ClipScore,
  DuplicateCheckReport,
} from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

const EQUAL_WEIGHTS = {
  length_fit: 1.0,
  hook_coverage: 1.0,
  claim_coverage: 1.0,
  transcript_density: 1.0,
};

const STATUS_CLASSES: Record<ClipCandidateStatus, string> = {
  NEW: "bg-slate-200 text-slate-700",
  SCORED: "bg-amber-100 text-amber-800",
  APPROVED: "bg-emerald-100 text-emerald-800",
  REJECTED: "bg-rose-100 text-rose-800",
};

type DupState = {
  windowIds: string[];
  contentIds: string[];
  threshold: number;
};

type Props = {
  orgId: string;
  candidates: ClipCandidateView[];
  scores: ClipScore[];
};

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload: unknown = await response.json();
      if (
        typeof payload === "object" &&
        payload !== null &&
        "detail" in payload &&
        typeof payload.detail === "string"
      ) {
        detail = payload.detail;
      }
    } catch {
      // keep status fallback
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
}

export default function ReviewBoard({ orgId, candidates, scores }: Props) {
  const router = useRouter();
  const [orgInput, setOrgInput] = useState(orgId);
  const [actor, setActor] = useState("operator");
  const [threshold, setThreshold] = useState("0.5");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dups, setDups] = useState<Record<string, DupState>>({});

  const scoresByCandidate = new Map<string, ClipScore>(
    scores.map((score) => [score.candidate_id, score]),
  );

  const run = async (candidateId: string, action: () => Promise<void>) => {
    setBusyId(candidateId);
    setError(null);
    try {
      await action();
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  const decide = (candidateId: string, decision: "APPROVE" | "REJECT") =>
    run(candidateId, async () => {
      await postJson(`/clip-candidates/${candidateId}/decisions`, {
        org_id: orgId,
        decision,
        actor_ref: actor || "operator",
      });
    });

  const scoreCandidate = (candidateId: string) =>
    run(candidateId, async () => {
      await postJson(`/clip-candidates/${candidateId}/scores`, {
        org_id: orgId,
        weights: EQUAL_WEIGHTS,
      });
    });

  const checkDuplicates = (candidateId: string) =>
    run(candidateId, async () => {
      const parsed = Number(threshold);
      const report = await postJson<DuplicateCheckReport>(
        `/clip-candidates/${candidateId}/duplicate-check`,
        { org_id: orgId, threshold: Number.isFinite(parsed) && parsed > 0 ? parsed : 0.5 },
      );
      setDups((current) => ({
        ...current,
        [candidateId]: {
          windowIds: report.window_duplicates.map((finding) => finding.candidate_id),
          contentIds: report.content_duplicates.map((finding) => finding.candidate_id),
          threshold: report.threshold,
        },
      }));
    });

  if (!orgId) {
    return (
      <section className="rounded-lg border p-6">
        <p className="mb-3 font-medium">No organization selected.</p>
        <p className="mb-4 text-sm text-slate-600">
          Set <code>NEXT_PUBLIC_ORG_ID</code> or enter an org id below.
        </p>
        <OrgField
          value={orgInput}
          onChange={setOrgInput}
          onSubmit={() => router.push(`/reviews?org=${encodeURIComponent(orgInput.trim())}`)}
        />
      </section>
    );
  }

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end gap-4 rounded-lg border p-4">
        <label className="flex flex-col gap-1 text-sm">
          Organization
          <input
            className="rounded border px-2 py-1"
            value={orgInput}
            onChange={(event) => setOrgInput(event.target.value)}
          />
        </label>
        <button
          type="button"
          className="rounded bg-slate-800 px-3 py-1.5 text-sm text-white"
          onClick={() => router.push(`/reviews?org=${encodeURIComponent(orgInput.trim())}`)}
        >
          Load
        </button>
        <label className="flex flex-col gap-1 text-sm">
          Actor
          <input
            className="rounded border px-2 py-1"
            value={actor}
            onChange={(event) => setActor(event.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Dup threshold
          <input
            className="w-20 rounded border px-2 py-1"
            value={threshold}
            onChange={(event) => setThreshold(event.target.value)}
          />
        </label>
        {error ? (
          <p className="text-sm text-rose-700" role="alert">
            {error}
          </p>
        ) : null}
      </div>

      {candidates.length === 0 ? (
        <p className="text-slate-600">No candidates for this organization.</p>
      ) : (
        <ul className="space-y-3">
          {candidates.map((candidate) => {
            const score = scoresByCandidate.get(candidate.candidate_id);
            const dup = dups[candidate.candidate_id];
            const busy = busyId === candidate.candidate_id;
            return (
              <li key={candidate.candidate_id} className="rounded-lg border p-4">
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <div className="font-mono text-sm">
                    {candidate.media_asset_id} · {candidate.start_ms}–{candidate.end_ms} ms ·{" "}
                    {candidate.source}
                  </div>
                  <span
                    className={`rounded px-2 py-0.5 text-xs font-medium ${STATUS_CLASSES[candidate.status]}`}
                  >
                    {candidate.status}
                  </span>
                </div>
                <div className="mb-3 text-sm text-slate-600">
                  {score ? (
                    <span>
                      score <strong>{score.total.toFixed(2)}</strong>
                      {Object.entries(score.components).map(([name, value]) => (
                        <span key={name} className="ml-2 rounded bg-slate-100 px-1.5 py-0.5 text-xs">
                          {name}: {value.toFixed(2)}
                        </span>
                      ))}
                    </span>
                  ) : (
                    <span>unscored</span>
                  )}
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    disabled={busy}
                    className="rounded bg-slate-700 px-3 py-1 text-xs text-white disabled:opacity-50"
                    onClick={() => scoreCandidate(candidate.candidate_id)}
                  >
                    Score (equal weights)
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    className="rounded bg-slate-700 px-3 py-1 text-xs text-white disabled:opacity-50"
                    onClick={() => checkDuplicates(candidate.candidate_id)}
                  >
                    Check duplicates
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    className="rounded bg-emerald-700 px-3 py-1 text-xs text-white disabled:opacity-50"
                    onClick={() => decide(candidate.candidate_id, "APPROVE")}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    className="rounded bg-rose-700 px-3 py-1 text-xs text-white disabled:opacity-50"
                    onClick={() => decide(candidate.candidate_id, "REJECT")}
                  >
                    Reject
                  </button>
                </div>
                {dup ? (
                  <p className="mt-2 text-xs text-slate-600">
                    duplicates (threshold {dup.threshold}): {dup.windowIds.length} window ·{" "}
                    {dup.contentIds.length} content
                    {dup.windowIds.length + dup.contentIds.length > 0 ? (
                      <>
                        {" "}
                        — {dup.windowIds.join(", ")}
                        {dup.contentIds.length ? ` / ${dup.contentIds.join(", ")}` : ""}
                      </>
                    ) : null}
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function OrgField({
  value,
  onChange,
  onSubmit,
}: {
  value: string;
  onChange: (next: string) => void;
  onSubmit: () => void;
}) {
  return (
    <div className="flex gap-2">
      <input
        className="rounded border px-2 py-1"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="org id"
      />
      <button
        type="button"
        className="rounded bg-slate-800 px-3 py-1.5 text-sm text-white"
        onClick={onSubmit}
      >
        Load
      </button>
    </div>
  );
}
