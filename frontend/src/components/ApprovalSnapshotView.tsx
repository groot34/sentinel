import type {
  ApprovalSnapshotResponse,
  EvidenceResult,
  HypothesisResult,
  ProposalResult,
  VerificationResult,
} from "../api/types";
import { formatTimestamp } from "../lib/format";
import { hasRenderablePatch, parseUnifiedDiff } from "../lib/proposals";
import { StatusBadge } from "./StatusBadge";

interface ApprovalSnapshotViewProps {
  snapshot: ApprovalSnapshotResponse;
  /**
   * The investigation's current evidence. Used only to list what has arrived
   * *since* the decision, always labelled as not part of the decision-time
   * context. Omit for a pure historical view.
   */
  currentEvidence?: readonly EvidenceResult[];
  /** Current hypothesis, compared against the decision-time verdict. */
  currentHypothesis?: HypothesisResult | null;
  /** Current verification for the linked hypothesis, same comparison. */
  currentVerification?: VerificationResult | null;
  onFocusEvidence?: (evidenceId: string) => void;
}

function lineRange(start?: number | null, end?: number | null): string {
  if (start === null || start === undefined) return "";
  if (end === null || end === undefined || end === start) return `:${start}`;
  return `:${start}-${end}`;
}

function DecisionHeadline({ snapshot }: { snapshot: ApprovalSnapshotResponse }) {
  return (
    <p className="card__refs">
      {snapshot.decision ? (
        <StatusBadge status={snapshot.decision.toUpperCase()} />
      ) : (
        <span className="tag tag--muted">decision</span>
      )}
      <span className="muted">
        {" "}
        recorded by {snapshot.reviewer ?? "—"} ·{" "}
        {formatTimestamp(snapshot.decided_at)}
        {snapshot.notes ? ` · ${snapshot.notes}` : ""}
      </span>
    </p>
  );
}

/** Original proposal as it stood at decision time (verbatim snapshot section). */
function SnapshotProposal({ proposal }: { proposal: ProposalResult | null }) {
  if (!proposal) {
    return (
      <p className="muted">
        The proposal linked to this decision was not present in the snapshot.
      </p>
    );
  }

  const diffLines = hasRenderablePatch(proposal.patch)
    ? parseUnifiedDiff(proposal.patch as string)
    : [];

  return (
    <>
      {proposal.summary && <p className="card__claim">{proposal.summary}</p>}
      {proposal.rationale && (
        <div className="subblock">
          <h4>Rationale</h4>
          <p className="card__body">{proposal.rationale}</p>
        </div>
      )}
      {proposal.expected_effect && (
        <div className="subblock">
          <h4>Expected effect</h4>
          <p className="card__body">{proposal.expected_effect}</p>
        </div>
      )}
      <div className="subblock">
        <h4>Proposed changes</h4>
        {proposal.changes.length === 0 ? (
          <p className="muted">No file changes were recorded in the snapshot.</p>
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
          <pre
            className="diff"
            aria-label={`Decision-time patch for ${proposal.proposal_id}`}
          >
            {diffLines.map((line, position) => (
              <span
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
            Only file references were recorded in the snapshot; no patch text is
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
    </>
  );
}

/**
 * The immutable evidence snapshot captured for one approval decision.
 *
 * Everything under "captured at decision time" comes verbatim from the stored
 * snapshot and never changes. The current investigation state is shown
 * separately, and only as a delta — it is never presented as the context the
 * reviewer saw. A legacy approval with no captured snapshot gets an honest
 * explanation instead of fabricated history.
 */
export function ApprovalSnapshotView({
  snapshot,
  currentEvidence,
  currentHypothesis,
  currentVerification,
  onFocusEvidence,
}: ApprovalSnapshotViewProps) {
  if (!snapshot.snapshot_available) {
    return (
      <div className="subblock snapshot">
        <h4>Historical snapshot</h4>
        <DecisionHeadline snapshot={snapshot} />
        <p className="notice">
          {snapshot.message ??
            "No historical evidence snapshot was captured for this approval decision."}
        </p>
        <p className="muted">
          The investigation context available when this decision was recorded was
          never stored, so it cannot be reconstructed. Current investigation
          evidence is not shown here, because it would not be what the reviewer
          saw.
        </p>
      </div>
    );
  }

  const document = snapshot.snapshot;
  const proposal = document.proposal ?? null;
  const hypothesis = document.hypothesis ?? null;
  const evidence = document.evidence ?? [];
  const verification = document.verification ?? [];
  const integrity = snapshot.integrity ?? null;

  const historicalEvidenceIds = new Set(
    evidence.map((item) => item.evidence_id),
  );
  const newEvidence = (currentEvidence ?? []).filter(
    (item) => !historicalEvidenceIds.has(item.evidence_id),
  );
  const comparesWithCurrent = currentEvidence !== undefined;

  const decisionTimeVerdict =
    hypothesis?.verdict ?? verification[0]?.verdict ?? null;
  const decisionTimeConfidence =
    hypothesis?.confidence ?? verification[0]?.confidence ?? null;
  const currentVerdict =
    currentHypothesis?.verdict ?? currentVerification?.verdict ?? null;
  const verdictChanged =
    Boolean(currentVerdict) && currentVerdict !== decisionTimeVerdict;

  return (
    <div className="subblock snapshot">
      <h4>
        Historical snapshot{" "}
        {snapshot.snapshot_version !== null &&
          snapshot.snapshot_version !== undefined && (
            <span className="tag tag--muted">v{snapshot.snapshot_version}</span>
          )}
      </h4>

      <p className="notice">
        Captured at decision time. This section is an immutable record and does
        not change when the investigation is updated later.
      </p>

      <DecisionHeadline snapshot={snapshot} />

      {integrity && (
        <p className="card__refs">
          <span className="tag tag--muted">
            integrity {integrity.verified ? "verified" : "unverified"}
          </span>
          <span className="muted">
            {" "}
            {integrity.algorithm}{" "}
            <span className="mono" title={integrity.hash}>
              {integrity.hash.slice(0, 16)}…
            </span>
          </span>
        </p>
      )}

      <div className="subblock">
        <h4>Proposal at decision time</h4>
        <SnapshotProposal proposal={proposal} />
      </div>

      <div className="subblock">
        <h4>Linked hypothesis at decision time</h4>
        {hypothesis ? (
          <>
            <p className="card__refs">
              {proposal?.hypothesis_id && (
                <a
                  className="chip chip--link mono"
                  href={`#hypothesis-${proposal.hypothesis_id}`}
                >
                  {proposal.hypothesis_id}
                </a>
              )}
              {decisionTimeVerdict ? (
                <StatusBadge
                  status={decisionTimeVerdict}
                  title={`Decision-time verdict: ${decisionTimeVerdict}`}
                />
              ) : (
                <span className="tag tag--muted">no verdict recorded</span>
              )}
              {decisionTimeConfidence !== null &&
                decisionTimeConfidence !== undefined && (
                  <span className="muted">
                    {" "}
                    confidence {decisionTimeConfidence}
                  </span>
                )}
            </p>
            <p className="card__body">{hypothesis.claim}</p>
            {hypothesis.supporting_reasoning && (
              <p className="card__body">{hypothesis.supporting_reasoning}</p>
            )}
            {hypothesis.evidence_ids.length > 0 && (
              <div className="chips">
                {hypothesis.evidence_ids.map((evidenceId) => (
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
            )}
          </>
        ) : (
          <p className="muted">
            No hypothesis was linked to this proposal in the snapshot.
          </p>
        )}
      </div>

      <div className="subblock">
        <h4>Supporting evidence at decision time ({evidence.length})</h4>
        {evidence.length === 0 ? (
          <p className="muted">No evidence was recorded in the snapshot.</p>
        ) : (
          <ul className="refs">
            {evidence.map((item) => (
              <li key={item.evidence_id}>
                <div className="card__refs">
                  <span className="mono">{item.evidence_id}</span>
                  {item.source && <span className="tag tag--muted">{item.source}</span>}
                  {item.type && <span className="tag tag--muted">{item.type}</span>}
                  {item.timestamp && (
                    <span className="muted">{formatTimestamp(item.timestamp)}</span>
                  )}
                </div>
                {item.reference && <span className="muted mono">{item.reference}</span>}
                {item.description && <p className="card__body">{item.description}</p>}
                {item.excerpt && <p className="muted mono">{item.excerpt}</p>}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="subblock">
        <h4>Verification at decision time</h4>
        {verification.length === 0 ? (
          <p className="muted">No verification result was recorded in the snapshot.</p>
        ) : (
          verification.map((result) => (
            <div key={result.hypothesis_id} className="subblock">
              <p className="card__refs">
                <StatusBadge status={result.verdict} />
                {result.confidence !== null && result.confidence !== undefined && (
                  <span className="muted"> confidence {result.confidence}</span>
                )}
              </p>
              {result.reasoning && <p className="card__body">{result.reasoning}</p>}
              {result.checks.length > 0 && (
                <ul className="bullets">
                  {result.checks.map((check, index) => (
                    <li key={check.check_id ?? `${result.hypothesis_id}-${index}`}>
                      <span className="mono">
                        {check.check_id ?? "check"}
                      </span>{" "}
                      {check.result && <StatusBadge status={check.result} />}{" "}
                      {check.description}
                      {check.reference && (
                        <span className="muted mono"> {check.reference}</span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))
        )}
      </div>

      {comparesWithCurrent && (
        <div className="subblock">
          <h4>Current investigation state (not part of this decision)</h4>
          <p className="muted">
            Shown only to contrast with the snapshot above. None of it was
            available when this decision was recorded.
          </p>

          {newEvidence.length > 0 ? (
            <>
              <p className="muted">
                Evidence recorded since this decision ({newEvidence.length}):
              </p>
              <div className="chips">
                {newEvidence.map((item) => (
                  <button
                    key={item.evidence_id}
                    type="button"
                    className="chip"
                    onClick={() => onFocusEvidence?.(item.evidence_id)}
                  >
                    <span className="mono">{item.evidence_id}</span>
                  </button>
                ))}
              </div>
            </>
          ) : (
            <p className="muted">
              No additional evidence has been recorded since this decision.
            </p>
          )}

          {verdictChanged && (
            <p className="muted">
              The linked hypothesis verdict has since changed to{" "}
              <span className="mono">{currentVerdict}</span>; at decision time it
              was{" "}
              <span className="mono">{decisionTimeVerdict ?? "not recorded"}</span>.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
