/**
 * TypeScript mirrors of the Sentinel 2.0 API response models.
 *
 * These are hand-written against `api/models.py` so the UI is typed against the
 * real allowlisted payloads. Only the fields the UI consumes are declared; the
 * backend already projects an explicit allowlist, so nothing internal leaks in.
 */

/** Overall investigation status (mirrors `IncidentStatus`). */
export type InvestigationStatus =
  | "PENDING"
  | "RUNNING"
  | "COMPLETED"
  | "PARTIAL"
  | "FAILED";

/** Terminal statuses — no further events will be produced. */
export const TERMINAL_STATUSES: readonly InvestigationStatus[] = [
  "COMPLETED",
  "PARTIAL",
  "FAILED",
];

export function isTerminalStatus(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status);
}

/** `GET /investigations` → `ListInvestigationsResponse` */
export interface ListInvestigationsResponse {
  investigations: string[];
}

/** One pipeline stage summary (`StageSummaryResponse`). */
export interface StageSummary {
  status: string;
  llm_calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  error?: string | null;
  cache_hit: boolean;
}

/** One recorded human approval decision (`ApprovalRecordResponse`). */
export interface ApprovalRecord {
  proposal_id: string;
  status: string;
  decision: string;
  approved_by: string;
  timestamp: string;
  notes?: string | null;
}

/** The only decisions the approval endpoint accepts (`SubmitApprovalRequest`). */
export type ApprovalDecision = "approved" | "rejected";

/** `POST /investigations/{id}/approval` request body (`SubmitApprovalRequest`). */
export interface SubmitApprovalRequest {
  proposal_id: string;
  decision: ApprovalDecision;
  reviewer: string;
  notes?: string;
}

/** `POST /investigations/{id}/approval` → `SubmitApprovalResponse`. */
export interface SubmitApprovalResponse {
  investigation_id: string;
  /** The persisted decision. Its `status` is `APPROVED` or `REJECTED`. */
  approval: ApprovalRecord;
  /** The investigation's own status (e.g. `COMPLETED`) — not the decision. */
  status: string;
  human_approval_notice: string;
}

/** `GET /investigations/{id}` → `GetInvestigationResponse` */
export interface InvestigationDetail {
  investigation_id: string;
  status: InvestigationStatus | string;
  current_stage?: string | null;
  started_at: string;
  completed_at?: string | null;
  error?: string | null;
  llm_call_count: number;
  version: number;
  lease_owner?: string | null;
  lease_expires_at?: string | null;
  stages: Record<string, StageSummary>;
  approvals: ApprovalRecord[];
}

/** `POST /investigations` with `Prefer: respond-async` → `AcceptedInvestigationResponse` */
export interface AcceptedInvestigation {
  investigation_id: string;
  status: string;
  message: string;
  status_url: string;
  events_url: string;
}

/** One durable outbox event (`EventHistoryItemResponse`). */
export interface EventHistoryItem {
  event_id: string;
  investigation_id: string;
  event_type: string;
  timestamp?: string | null;
  created_at: string;
  processed_at?: string | null;
  status: string;
  retry_count: number;
  error_message?: string | null;
  payload: Record<string, unknown>;
}

/** `GET /investigations/{id}/events` (JSON) → `EventHistoryResponse` */
export interface EventHistoryResponse {
  investigation_id: string;
  total: number;
  events: EventHistoryItem[];
  backend_supports_event_history: boolean;
}

/** One persisted evidence item (`EvidenceResultResponse`). */
export interface EvidenceResult {
  evidence_id: string;
  source?: string | null;
  type?: string | null;
  reference?: string | null;
  description?: string | null;
  excerpt?: string | null;
  timestamp?: string | null;
  interpretation?: string | null;
  metric?: string | null;
  value?: number | null;
}

/** One persisted hypothesis (`HypothesisResultResponse`). */
export interface HypothesisResult {
  hypothesis_id: string;
  claim: string;
  evidence_ids: string[];
  verdict?: string | null;
  supporting_reasoning?: string | null;
  falsification_criteria: string[];
  verification_plan: string[];
}

/** One deterministic verification check (`VerificationCheckResponse`). */
export interface VerificationCheck {
  check_id?: string | null;
  description?: string | null;
  result?: string | null;
  reference?: string | null;
  detail?: string | null;
  evidence_ids: string[];
  /** Deterministic check family, e.g. `code_invariant` | `metric_correlation`. */
  check_type?: string | null;
  /**
   * Whether the check tests the mechanism the hypothesis asserts. `false`
   * marks the evidence-grounding fallback — a PASS that only confirms the
   * referenced evidence exists and cannot, alone, confirm a hypothesis.
   */
  discriminating?: boolean | null;
}

/** Verification outcome for one hypothesis (`VerificationResultResponse`). */
export interface VerificationResult {
  hypothesis_id: string;
  verdict: string;
  confidence?: number | null;
  reasoning?: string | null;
  checks: VerificationCheck[];
}

/** One proposed file change (`FileChangeResponse`). */
export interface FileChange {
  file: string;
  description?: string | null;
  start_line?: number | null;
  end_line?: number | null;
}

/** One persisted fix proposal (`ProposalResultResponse`). */
export interface ProposalResult {
  proposal_id: string;
  hypothesis_id?: string | null;
  status?: string | null;
  summary?: string | null;
  rationale?: string | null;
  patch?: string | null;
  expected_effect?: string | null;
  risks: string[];
  validation_plan: string[];
  rollback_plan?: string | null;
  evidence_ids: string[];
  changes: FileChange[];
  human_approval_notice?: string | null;
}

/** `GET /investigations/{id}/result` → `InvestigationResultResponse` */
export interface InvestigationResult {
  investigation_id: string;
  status: string;
  current_stage?: string | null;
  completed_at?: string | null;
  result_available: boolean;
  hypotheses: HypothesisResult[];
  evidence: EvidenceResult[];
  verification: VerificationResult[];
  proposals: ProposalResult[];
}

/** Standardized error payload (`ErrorResponse`). */
export interface ApiErrorResponse {
  error: string;
  detail: string;
}
