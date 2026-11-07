import type { VerificationCheck } from "../api/types";
import type { HypothesisOutcome } from "../lib/diagnosis";
import { evidenceSourceLabel, evidenceTypeLabel } from "./EvidenceExplorer";
import { StatusBadge } from "./StatusBadge";

interface HypothesisCardProps {
  outcome: HypothesisOutcome;
  /** Highlight and scroll to an evidence item in the explorer. */
  onFocusEvidence: (evidenceId: string) => void;
}

function CheckList({
  checks,
  onFocusEvidence,
}: {
  checks: VerificationCheck[];
  onFocusEvidence: (evidenceId: string) => void;
}) {
  return (
    <ul className="checks">
      {checks.map((check, index) => (
        <li key={check.check_id ?? index} className="checks__item">
          <div className="checks__row">
            {check.result && <StatusBadge status={check.result} />}
            <span>{check.description ?? check.check_id ?? "Check"}</span>
            {check.check_id && (
              <span className="mono checks__ref">{check.check_id}</span>
            )}
          </div>
          {check.detail && <p className="checks__detail">{check.detail}</p>}
          {check.reference && (
            <p className="checks__ref mono">{check.reference}</p>
          )}
          {check.evidence_ids.length > 0 && (
            <div className="chips">
              {check.evidence_ids.map((evidenceId) => (
                <button
                  key={evidenceId}
                  type="button"
                  className="chip chip--link mono"
                  onClick={() => onFocusEvidence(evidenceId)}
                >
                  {evidenceId}
                </button>
              ))}
            </div>
          )}
        </li>
      ))}
    </ul>
  );
}

/**
 * One hypothesis with the evidence it cites and its verification outcome.
 *
 * `verdict` is the backend's joined verdict (CONFIRMED / REJECTED /
 * INCONCLUSIVE) and stays absent while a hypothesis is still unverified; the
 * card only ever shows values the result projection actually contains.
 */
export function HypothesisCard({
  outcome,
  onFocusEvidence,
}: HypothesisCardProps) {
  const { hypothesis, verification, evidence, missingEvidenceIds } = outcome;

  return (
    <li
      className="card card--nested hypothesis"
      id={`hypothesis-${hypothesis.hypothesis_id}`}
    >
      <div className="card__head">
        <span className="mono">{hypothesis.hypothesis_id}</span>
        {hypothesis.verdict ? (
          <StatusBadge status={hypothesis.verdict} />
        ) : (
          <span className="badge badge--muted" title="No verification result persisted">
            UNVERIFIED
          </span>
        )}
      </div>

      <p className="card__claim">{hypothesis.claim}</p>
      {hypothesis.supporting_reasoning && (
        <p className="card__body">{hypothesis.supporting_reasoning}</p>
      )}

      <div className="subblock">
        <h4>Supporting evidence ({hypothesis.evidence_ids.length})</h4>
        {evidence.length === 0 && missingEvidenceIds.length === 0 && (
          <p className="muted">This hypothesis cites no evidence.</p>
        )}
        {evidence.length > 0 && (
          <div className="chips">
            {evidence.map((item) => (
              <button
                key={item.evidence_id}
                type="button"
                className="chip chip--link"
                title={item.description ?? item.reference ?? item.evidence_id}
                onClick={() => onFocusEvidence(item.evidence_id)}
              >
                <span className="mono">{item.evidence_id}</span>
                <span className="chip__tag">
                  {item.type
                    ? evidenceTypeLabel(item.type)
                    : evidenceSourceLabel(item.source ?? "unknown")}
                </span>
              </button>
            ))}
          </div>
        )}
        {missingEvidenceIds.length > 0 && (
          <p className="muted" role="note">
            Cited but not present in the persisted evidence projection:{" "}
            <span className="mono">{missingEvidenceIds.join(", ")}</span>
          </p>
        )}
      </div>

      <div className="subblock">
        <h4>Verification</h4>
        {!verification && (
          <p className="muted">
            No verification result was persisted for this hypothesis.
          </p>
        )}
        {verification && (
          <>
            <p className="hypothesis__verdict">
              Outcome: <StatusBadge status={verification.verdict} />
              {typeof verification.confidence === "number" && (
                <span className="muted">
                  confidence {Math.round(verification.confidence * 100)}%
                </span>
              )}
            </p>
            {verification.reasoning && (
              <p className="card__body">{verification.reasoning}</p>
            )}
            {verification.checks.length > 0 ? (
              <CheckList
                checks={verification.checks}
                onFocusEvidence={onFocusEvidence}
              />
            ) : (
              <p className="muted">No discrete checks were recorded.</p>
            )}
          </>
        )}
      </div>

      {hypothesis.falsification_criteria.length > 0 && (
        <details className="subblock details">
          <summary>
            Falsification criteria ({hypothesis.falsification_criteria.length})
          </summary>
          <ul className="bullets">
            {hypothesis.falsification_criteria.map((criterion) => (
              <li key={criterion}>{criterion}</li>
            ))}
          </ul>
        </details>
      )}

      {hypothesis.verification_plan.length > 0 && (
        <details className="subblock details">
          <summary>
            Verification plan ({hypothesis.verification_plan.length})
          </summary>
          <ul className="bullets">
            {hypothesis.verification_plan.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ul>
        </details>
      )}
    </li>
  );
}
