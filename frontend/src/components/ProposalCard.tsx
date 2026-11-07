import { useState } from "react";

import { ApiError } from "../api/client";
import type { SubmitApprovalResponse } from "../api/types";
import { formatTimestamp } from "../lib/format";
import {
  hasRenderablePatch,
  parseUnifiedDiff,
  type ProposalView,
} from "../lib/proposals";
import { StatusBadge } from "./StatusBadge";

/**
 * Identity recorded as the reviewer when the operator does not type one.
 *
 * The approval endpoint requires `reviewer` (1–128 chars) and the frontend has
 * no authenticated user identity, so a sensible default is offered. It is
 * always displayed and editable in the confirmation step, and the value the
 * backend echoes back is what the card then shows as the recorded reviewer.
 */
export const DEFAULT_REVIEWER = "operator";

/** One decision submitted to `POST /investigations/{id}/approval`. */
export interface ProposalDecisionInput {
  proposalId: string;
  decision: "approved" | "rejected";
  reviewer: string;
  notes?: string;
}

interface ProposalCardProps {
  view: ProposalView;
  /** Submits the decision through the real API. Absent in read-only contexts. */
  onDecide?: (input: ProposalDecisionInput) => Promise<SubmitApprovalResponse>;
  onFocusEvidence?: (evidenceId: string) => void;
}

function lineRange(start?: number | null, end?: number | null): string {
  if (start === null || start === undefined) return "";
  if (end === null || end === undefined || end === start) return `:${start}`;
  return `:${start}-${end}`;
}

/**
 * One fix proposal: what it changes, why, the evidence behind it, and the
 * recorded human decision.
 *
 * Approving or rejecting only records a decision through the existing backend
 * endpoint. Nothing here applies a patch, writes a file, commits or deploys —
 * the backend's own `human_approval_notice` is shown verbatim.
 */
export function ProposalCard({
  view,
  onDecide,
  onFocusEvidence,
}: ProposalCardProps) {
  const { proposal } = view;
  const [mode, setMode] = useState<"approved" | "rejected" | null>(null);
  const [reviewer, setReviewer] = useState(DEFAULT_REVIEWER);
  const [notes, setNotes] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [validation, setValidation] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<SubmitApprovalResponse | null>(null);

  const canDecide = Boolean(onDecide) && !view.terminal;

  function openConfirmation(decision: "approved" | "rejected") {
    if (submitting) return;
    setValidation(null);
    setError(null);
    setMode(decision);
  }

  function cancelConfirmation() {
    if (submitting) return;
    setValidation(null);
    setMode(null);
  }

  async function confirmDecision() {
    if (!onDecide || !mode || submitting) return;
    const trimmedReviewer = reviewer.trim();
    if (!trimmedReviewer) {
      setValidation("Enter a reviewer identity before submitting.");
      return;
    }
    setSubmitting(true);
    setValidation(null);
    setError(null);
    try {
      const response = await onDecide({
        proposalId: proposal.proposal_id,
        decision: mode,
        reviewer: trimmedReviewer,
        notes: notes.trim() ? notes.trim() : undefined,
      });
      setOutcome(response);
      setMode(null);
      setNotes("");
    } catch (caught) {
      setError(
        caught instanceof ApiError || caught instanceof Error
          ? caught.message
          : "An unexpected error occurred while recording the decision.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const diffLines = hasRenderablePatch(proposal.patch)
    ? parseUnifiedDiff(proposal.patch as string)
    : [];

  return (
    <li
      id={`proposal-${proposal.proposal_id}`}
      className="card card--nested proposal"
    >
      <div className="card__head">
        <span className="mono">{proposal.proposal_id}</span>
        <StatusBadge status={view.effectiveStatus} />
        {view.terminal && <span className="tag tag--muted">decision recorded</span>}
        {view.awaitingDecision && view.approval && (
          <span className="tag tag--muted">automated decision — overridable</span>
        )}
      </div>

      {proposal.hypothesis_id && (
        <p className="card__refs">
          Addresses{" "}
          <a
            className="chip chip--link mono"
            href={`#hypothesis-${proposal.hypothesis_id}`}
          >
            {proposal.hypothesis_id}
          </a>
        </p>
      )}

      {proposal.summary && <p className="card__claim">{proposal.summary}</p>}

      <div className="subblock">
        <h4>Rationale</h4>
        {proposal.rationale ? (
          <p className="card__body">{proposal.rationale}</p>
        ) : (
          <p className="muted">No rationale was persisted for this proposal.</p>
        )}
      </div>

      {proposal.expected_effect && (
        <div className="subblock">
          <h4>Expected effect</h4>
          <p className="card__body">{proposal.expected_effect}</p>
        </div>
      )}

      {proposal.evidence_ids.length > 0 && (
        <div className="subblock">
          <h4>Supporting evidence ({proposal.evidence_ids.length})</h4>
          <div className="chips">
            {proposal.evidence_ids.map((evidenceId) => (
              <button
                key={evidenceId}
                type="button"
                className="chip"
                onClick={() => onFocusEvidence?.(evidenceId)}
              >
                <span className="mono">{evidenceId}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="subblock">
        <h4>Proposed changes</h4>
        {proposal.changes.length === 0 ? (
          <p className="muted">No file changes were persisted for this proposal.</p>
        ) : (
          <ul className="refs">
            {proposal.changes.map((change) => (
              <li key={`${change.file}${lineRange(change.start_line, change.end_line)}`}>
                <span className="mono">
                  {change.file}
                  {lineRange(change.start_line, change.end_line) && (
                    <span className="muted">
                      {lineRange(change.start_line, change.end_line)}
                    </span>
                  )}
                </span>
                {change.description && (
                  <span className="muted"> — {change.description}</span>
                )}
              </li>
            ))}
          </ul>
        )}

        {diffLines.length > 0 ? (
          <pre className="diff" aria-label={`Proposed patch for ${proposal.proposal_id}`}>
            {diffLines.map((line, position) => (
              <span
                // Lines are positional; identical text can repeat within a patch.
                // eslint-disable-next-line react/no-array-index-key
                key={`${position}-${line.kind}`}
                className={`diff__line diff__line--${line.kind}`}
              >
                {line.text || " "}
                {"\n"}
              </span>
            ))}
          </pre>
        ) : (
          <p className="muted">
            Only file references are stored for this proposal; no patch text is
            available to render as a diff.
          </p>
        )}
      </div>

      {proposal.risks.length > 0 && (
        <div className="subblock">
          <h4>Risks</h4>
          <ul className="bullets">
            {proposal.risks.map((risk) => (
              <li key={risk}>{risk}</li>
            ))}
          </ul>
        </div>
      )}

      {proposal.validation_plan.length > 0 && (
        <div className="subblock">
          <h4>Validation plan</h4>
          <ol className="bullets">
            {proposal.validation_plan.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        </div>
      )}

      {proposal.rollback_plan && (
        <div className="subblock">
          <h4>Rollback plan</h4>
          <p className="card__body">{proposal.rollback_plan}</p>
        </div>
      )}

      {proposal.human_approval_notice && (
        <p className="notice">{proposal.human_approval_notice}</p>
      )}

      <div className="subblock approval">
        <h4>Approval status</h4>

        {view.approval ? (
          <p className="card__refs">
            <StatusBadge status={view.approval.status} />
            <span className="muted">
              {" "}
              recorded by {view.approval.approved_by} ·{" "}
              {formatTimestamp(view.approval.timestamp)}
              {view.approval.notes ? ` · ${view.approval.notes}` : ""}
            </span>
          </p>
        ) : (
          <p className="muted">No decision has been recorded yet.</p>
        )}

        {view.awaitingDecision && view.approval && (
          <p className="muted">
            Recorded by the non-interactive pipeline gate. A human decision can
            still be submitted.
          </p>
        )}

        {view.terminal && (
          <p className="muted">
            This proposal has already been reviewed. The backend rejects a
            second decision.
          </p>
        )}

        {canDecide && !mode && (
          <div className="approval__actions">
            <button
              type="button"
              className="button button--primary"
              disabled={submitting}
              onClick={() => openConfirmation("approved")}
            >
              Approve
            </button>
            <button
              type="button"
              className="button button--danger"
              disabled={submitting}
              onClick={() => openConfirmation("rejected")}
            >
              Reject
            </button>
          </div>
        )}

        {mode && (
          <div className="approval__confirm">
            <p className="approval__confirm-text">
              {mode === "approved" ? "Record approval for " : "Record rejection of "}
              <span className="mono">{proposal.proposal_id}</span>? This only
              records your decision — no code is modified, committed, pushed or
              deployed.
            </p>

            <label className="field field--inline">
              <span className="field__label">Reviewer</span>
              <input
                className="input"
                value={reviewer}
                disabled={submitting}
                placeholder="name or email"
                onChange={(event) => setReviewer(event.target.value)}
              />
            </label>

            <label className="field field--inline">
              <span className="field__label">Notes (optional)</span>
              <textarea
                className="input"
                rows={2}
                value={notes}
                disabled={submitting}
                onChange={(event) => setNotes(event.target.value)}
              />
            </label>

            {validation && (
              <p className="alert alert--error" role="alert">
                {validation}
              </p>
            )}

            <div className="approval__actions">
              <button
                type="button"
                className="button button--primary"
                disabled={submitting}
                onClick={() => void confirmDecision()}
              >
                {submitting
                  ? "Submitting…"
                  : mode === "approved"
                    ? "Confirm approval"
                    : "Confirm rejection"}
              </button>
              <button
                type="button"
                className="button"
                disabled={submitting}
                onClick={cancelConfirmation}
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {error && (
          <p className="alert alert--error" role="alert">
            {error}
          </p>
        )}

        {outcome && (
          <p className="notice notice--ok" role="status">
            Decision recorded: {outcome.approval.status} by{" "}
            {outcome.approval.approved_by}. {outcome.human_approval_notice}
          </p>
        )}
      </div>
    </li>
  );
}
