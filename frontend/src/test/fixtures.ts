/**
 * Small builders for API payloads used across the frontend tests.
 *
 * These mirror the real allowlisted response shapes (see `api/models.py`) so
 * tests exercise the same fields the backend actually returns.
 */

import type {
  EvidenceResult,
  EventHistoryItem,
  HypothesisResult,
  InvestigationResult,
  ProposalResult,
  VerificationResult,
} from "../api/types";

export function makeEvidence(
  overrides: Partial<EvidenceResult> = {},
): EvidenceResult {
  return {
    evidence_id: "EV-LOG-001",
    source: "logs",
    type: "error",
    reference: "logs/application.log:6",
    description: "Postgres connection pool spike",
    excerpt: "ERROR Postgres connection pool spike: 50 concurrent queries",
    timestamp: "2026-08-28T14:04:20Z",
    interpretation: "Reflects increased DB load.",
    metric: null,
    value: null,
    ...overrides,
  };
}

export function makeHypothesis(
  overrides: Partial<HypothesisResult> = {},
): HypothesisResult {
  return {
    hypothesis_id: "HYP-001",
    claim: "A misconfigured cache TTL caused a stampede.",
    evidence_ids: ["EV-LOG-001"],
    verdict: null,
    supporting_reasoning: "Log and metric evidence agree.",
    falsification_criteria: ["TTL stayed above 60s"],
    verification_plan: ["Read the deployed TTL"],
    ...overrides,
  };
}

export function makeVerification(
  overrides: Partial<VerificationResult> = {},
): VerificationResult {
  return {
    hypothesis_id: "HYP-001",
    verdict: "CONFIRMED",
    confidence: 0.95,
    reasoning: "All required checks passed.",
    checks: [
      {
        check_id: "CHK-001",
        description: "Referenced evidence excerpts verified against source files.",
        result: "PASS",
        reference: "service/app.py:22",
        detail: "5 of 6 referenced evidence items grounded.",
        evidence_ids: ["EV-LOG-001"],
      },
    ],
    ...overrides,
  };
}

export function makeProposal(
  overrides: Partial<ProposalResult> = {},
): ProposalResult {
  return {
    proposal_id: "FIX-001",
    hypothesis_id: "HYP-001",
    status: "PROPOSED",
    summary: "Restore the 1-hour TTL.",
    rationale: "The shortened TTL caused the stampede.",
    patch: "--- a\n+++ b\n",
    expected_effect: "Cache hit ratio recovers.",
    risks: [],
    validation_plan: ["Run the load test"],
    rollback_plan: "Revert the commit.",
    evidence_ids: ["EV-LOG-001"],
    changes: [{ file: "service/app.py", description: "Restore TTL", start_line: 22, end_line: 22 }],
    human_approval_notice: "AWAITING HUMAN APPROVAL — this fix has not been applied.",
    ...overrides,
  };
}

export function makeResult(
  overrides: Partial<InvestigationResult> = {},
): InvestigationResult {
  return {
    investigation_id: "inc_02_cache_stampede",
    status: "COMPLETED",
    current_stage: null,
    completed_at: "2026-08-28T14:10:00Z",
    result_available: true,
    hypotheses: [makeHypothesis()],
    evidence: [makeEvidence()],
    verification: [makeVerification()],
    proposals: [makeProposal()],
    ...overrides,
  };
}

export function makeEvent(
  id: string,
  type: string,
  payload: Record<string, unknown> = {},
  overrides: Partial<EventHistoryItem> = {},
): EventHistoryItem {
  return {
    event_id: id,
    investigation_id: "inc_02_cache_stampede",
    event_type: type,
    timestamp: "2026-08-28T14:04:00Z",
    created_at: "2026-08-28T14:04:00Z",
    status: "processed",
    retry_count: 0,
    payload,
    ...overrides,
  };
}
