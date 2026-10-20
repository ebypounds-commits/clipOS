import ReviewBoard from "./ReviewBoard";
import type { ClipCandidateView, ClipScore } from "./types";

export const dynamic = "force-dynamic";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

async function loadCandidates(org: string): Promise<ClipCandidateView[]> {
  try {
    const response = await fetch(
      `${API_BASE}/clip-candidates?org_id=${encodeURIComponent(org)}`,
      { cache: "no-store" },
    );
    if (!response.ok) return [];
    const data: unknown = await response.json();
    return Array.isArray(data) ? (data as ClipCandidateView[]) : [];
  } catch {
    return [];
  }
}

async function loadScores(org: string): Promise<ClipScore[]> {
  try {
    const response = await fetch(
      `${API_BASE}/clip-scores?org_id=${encodeURIComponent(org)}`,
      { cache: "no-store" },
    );
    if (!response.ok) return [];
    const data: unknown = await response.json();
    return Array.isArray(data) ? (data as ClipScore[]) : [];
  } catch {
    return [];
  }
}

export default async function ReviewsView({ orgId }: { orgId: string }) {
  const candidates = orgId ? await loadCandidates(orgId) : [];
  const scores = orgId ? await loadScores(orgId) : [];
  return (
    <main className="mx-auto max-w-5xl p-8">
      <h1 className="mb-2 text-2xl font-bold">Clip Review</h1>
      <p className="mb-6 text-sm text-slate-600">
        Score, run duplicate checks, and record APPROVE/REJECT decisions for clip candidates.
      </p>
      <ReviewBoard orgId={orgId} candidates={candidates} scores={scores} />
    </main>
  );
}
