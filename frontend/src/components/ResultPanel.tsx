import { useMemo, useState } from "react";

import type {
  ApprovalRecord,
  InvestigationResult,
  SubmitApprovalResponse,
} from "../api/types";
import {
  buildEvidenceIndex,
  hypothesisOutcomes,
  verificationCounts,
} from "../lib/diagnosis";
import { proposalViews } from "../lib/proposals";
import { EvidenceExplorer } from "./EvidenceExplorer";
import { HypothesisCard } from "./HypothesisCard";
import { ProposalCard, type ProposalDecisionInput } from "./ProposalCard";

interface ResultPanelProps {
  result: InvestigationResult;
  /** Persisted decisions from `GET /investigations/{id}` → `approvals`. */
  approvals?: ApprovalRecord[];
  /** Records a decision through the real API. Omit for a read-only view. */
  onDecide?: (input: ProposalDecisionInput) => Promise<SubmitApprovalResponse>;
}

/** Headline counters derived entirely from the persisted result projection. */
function DiagnosisSummary({ result }: { result: InvestigationResult }) {
  const counts = useMemo(() => verificationCounts(result), [result]);
  return (
    <div className="facts">
      <div className="fact">
        <span className="fact__label">Hypotheses</span>
        <span className="fact__value">{result.hypotheses.length}</span>
      </div>
      <div className="fact">
        <span className="fact__label">Confirmed</span>
        <span className="fact__value">{counts.confirmed}</span>
      </div>
      <div className="fact">
        <span className="fact__label">Rejected</span>
        <span className="fact__value">{counts.rejected}</span>
      </div>
      <div className="fact">
        <span className="fact__label">Inconclusive</span>
        <span className="fact__value">{counts.inconclusive}</span>
      </div>
      {counts.unverified > 0 && (
        <div className="fact">
          <span className="fact__label">Unverified</span>
          <span className="fact__value">{counts.unverified}</span>
        </div>
      )}
      <div className="fact">
        <span className="fact__label">Evidence</span>
        <span className="fact__value">{result.evidence.length}</span>
      </div>
      <div className="fact">
        <span className="fact__label">Fix proposals</span>
        <span className="fact__value">{result.proposals.length}</span>
      </div>
    </div>
  );
}

/**
 * Read-only projection of the persisted diagnosis.
 *
 * Presents the investigation as three navigable parts: the hypotheses with
 * their verification outcomes, the evidence behind them, and the fix proposals.
 * Selecting an evidence reference on a hypothesis (or a verification check)
 * highlights and scrolls to that evidence item.
 *
 * Fix proposals show their mandatory human-approval notice. When `onDecide` is
 * supplied the operator can record an explicit approve/reject decision through
 * the backend's approval endpoint; that only records the decision and never
 * applies a patch.
 */
export function ResultPanel({
  result,
  approvals = [],
  onDecide,
}: ResultPanelProps) {
  const [focusedEvidenceId, setFocusedEvidenceId] = useState<string | null>(
    null,
  );

  const index = useMemo(() => buildEvidenceIndex(result), [result]);
  const outcomes = useMemo(() => hypothesisOutcomes(result), [result]);
  const proposals = useMemo(
    () => proposalViews(result.proposals, approvals),
    [result.proposals, approvals],
  );

  const hasContent =
    result.hypotheses.length > 0 ||
    result.evidence.length > 0 ||
    result.verification.length > 0 ||
    result.proposals.length > 0;

  if (!result.result_available || !hasContent) {
    return (
      <section className="panel">
        <div className="panel__head">
          <h2>Diagnosis</h2>
        </div>
        <p className="muted">
          No diagnosis has been persisted yet. It appears once the pipeline
          completes.
        </p>
      </section>
    );
  }

  return (
    <section className="panel" id="diagnosis">
      <div className="panel__head">
        <h2>Diagnosis</h2>
        <span className="muted">{result.status}</span>
      </div>

      <DiagnosisSummary result={result} />

      <div className="result-block">
        <h3>Hypotheses and verification</h3>
        {outcomes.length === 0 ? (
          <p className="muted">No hypotheses were persisted for this incident.</p>
        ) : (
          <ul className="cards">
            {outcomes.map((outcome) => (
              <HypothesisCard
                key={outcome.hypothesis.hypothesis_id}
                outcome={outcome}
                onFocusEvidence={setFocusedEvidenceId}
              />
            ))}
          </ul>
        )}
      </div>

      <div className="result-block">
        <h3>Evidence</h3>
        <EvidenceExplorer
          evidence={result.evidence}
          citedByHypotheses={index.citedByHypotheses}
          citedByProposals={index.citedByProposals}
          focusedEvidenceId={focusedEvidenceId}
          onClearFocus={() => setFocusedEvidenceId(null)}
        />
      </div>

      <div className="result-block">
        <h3>Fix proposals (awaiting human approval)</h3>
        {proposals.length === 0 ? (
          <p className="muted">
            No fix proposals were generated for this investigation.
          </p>
        ) : (
          <ul className="cards">
            {proposals.map((view) => (
              <ProposalCard
                key={view.proposal.proposal_id}
                view={view}
                onDecide={onDecide}
                onFocusEvidence={setFocusedEvidenceId}
              />
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
