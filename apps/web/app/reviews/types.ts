export type ClipCandidateStatus = "NEW" | "SCORED" | "APPROVED" | "REJECTED";

export type ClipCandidateView = {
  candidate_id: string;
  media_asset_id: string;
  org_id: string;
  start_ms: number;
  end_ms: number;
  source: string;
  reason: string;
  status: ClipCandidateStatus;
};

export type ClipScore = {
  score_id: string;
  candidate_id: string;
  org_id: string;
  components: Record<string, number>;
  total: number;
  scorer_alias: string | null;
  scored_at: string | null;
};

export type DuplicateFinding = {
  candidate_id: string;
  media_asset_id: string;
  start_ms: number;
  end_ms: number;
};

export type DuplicateCheckReport = {
  candidate_id: string;
  org_id: string;
  threshold: number;
  fingerprints: unknown[];
  window_duplicates: DuplicateFinding[];
  content_duplicates: DuplicateFinding[];
};
