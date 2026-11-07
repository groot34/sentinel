import { useEffect, useMemo, useRef, useState } from "react";

import type { EvidenceResult } from "../api/types";
import {
  EMPTY_EVIDENCE_FILTER,
  filterEvidence,
  groupEvidenceBySource,
  hasActiveEvidenceFilter,
  evidenceSources,
  evidenceTypes,
  type EvidenceFilter,
} from "../lib/diagnosis";
import { formatTimestamp, humanizeToken } from "../lib/format";

interface EvidenceExplorerProps {
  evidence: EvidenceResult[];
  /** Evidence ID → hypothesis IDs that cite it. */
  citedByHypotheses: Map<string, string[]>;
  /** Evidence ID → proposal IDs that cite it. */
  citedByProposals: Map<string, string[]>;
  /** Evidence highlighted from a hypothesis/proposal link, if any. */
  focusedEvidenceId: string | null;
  onClearFocus: () => void;
}

/** Human-readable label for a persisted evidence `source` value. */
export function evidenceSourceLabel(source: string): string {
  switch (source) {
    case "logs":
      return "Logs";
    case "metrics":
      return "Metrics";
    case "git_diff":
      return "Git diff";
    case "code":
      return "Source code";
    case "config":
      return "Configuration";
    case "unknown":
      return "Unknown source";
    default:
      return humanizeToken(source);
  }
}

/** Human-readable label for a persisted evidence `type` value. */
export function evidenceTypeLabel(type: string): string {
  return humanizeToken(type);
}

/** One attribute of an evidence item, rendered as a labelled cell. */
function EvidenceMeta({ label, value }: { label: string; value: string }) {
  return (
    <div className="evidence__meta">
      <span className="evidence__meta-label">{label}</span>
      <span className="evidence__meta-value mono">{value}</span>
    </div>
  );
}

/**
 * Evidence explorer.
 *
 * Groups the persisted evidence by its backend-declared `source`, lets the user
 * narrow by source, type or free text, and shows which hypotheses/proposals
 * cite each item. Any evidence that is linked from elsewhere is highlighted and
 * scrolled into view.
 */
export function EvidenceExplorer({
  evidence,
  citedByHypotheses,
  citedByProposals,
  focusedEvidenceId,
  onClearFocus,
}: EvidenceExplorerProps) {
  const [filter, setFilter] = useState<EvidenceFilter>(EMPTY_EVIDENCE_FILTER);
  const focusedRef = useRef<HTMLLIElement | null>(null);

  const sources = useMemo(() => evidenceSources(evidence), [evidence]);
  const types = useMemo(() => evidenceTypes(evidence), [evidence]);
  const visible = useMemo(
    () => filterEvidence(evidence, filter),
    [evidence, filter],
  );
  const groups = useMemo(() => groupEvidenceBySource(visible), [visible]);

  // A link from a hypothesis must never be hidden by the active filter.
  useEffect(() => {
    if (focusedEvidenceId) setFilter(EMPTY_EVIDENCE_FILTER);
  }, [focusedEvidenceId]);

  // Bring the focused item into view once it is rendered.
  useEffect(() => {
    focusedRef.current?.scrollIntoView?.({ block: "center" });
  }, [focusedEvidenceId, visible]);

  if (evidence.length === 0) {
    return (
      <p className="muted">
        No evidence was persisted for this investigation. Evidence appears once
        the log, metric and code agents have completed.
      </p>
    );
  }

  const focusVisible =
    focusedEvidenceId !== null &&
    evidence.some((item) => item.evidence_id === focusedEvidenceId);

  return (
    <div className="evidence">
      <div className="evidence__toolbar">
        <div className="evidence__sources" role="group" aria-label="Filter evidence by source">
          <button
            type="button"
            className={`chip ${filter.source === null ? "chip--active" : ""}`}
            onClick={() => setFilter((f) => ({ ...f, source: null }))}
            aria-pressed={filter.source === null}
          >
            All sources ({evidence.length})
          </button>
          {sources.map((source) => {
            const count = evidence.filter(
              (item) => (item.source ?? "unknown") === source,
            ).length;
            return (
              <button
                key={source}
                type="button"
                className={`chip ${filter.source === source ? "chip--active" : ""}`}
                onClick={() =>
                  setFilter((f) => ({
                    ...f,
                    source: f.source === source ? null : source,
                  }))
                }
                aria-pressed={filter.source === source}
              >
                {evidenceSourceLabel(source)} ({count})
              </button>
            );
          })}
        </div>

        <div className="evidence__controls">
          <label className="field field--inline">
            <span className="field__label">Type</span>
            <select
              className="input"
              value={filter.type ?? ""}
              onChange={(event) =>
                setFilter((f) => ({ ...f, type: event.target.value || null }))
              }
            >
              <option value="">All types</option>
              {types.map((type) => (
                <option key={type} value={type}>
                  {evidenceTypeLabel(type)}
                </option>
              ))}
            </select>
          </label>
          <label className="field field--inline">
            <span className="field__label">Search</span>
            <input
              className="input"
              type="search"
              value={filter.query}
              placeholder="ID, text, reference…"
              onChange={(event) =>
                setFilter((f) => ({ ...f, query: event.target.value }))
              }
            />
          </label>
          {hasActiveEvidenceFilter(filter) && (
            <button
              type="button"
              className="button button--ghost"
              onClick={() => setFilter(EMPTY_EVIDENCE_FILTER)}
            >
              Clear filters
            </button>
          )}
        </div>
      </div>

      {focusVisible && (
        <div className="alert alert--info evidence__focus" role="status">
          Showing <span className="mono">{focusedEvidenceId}</span> from a
          linked hypothesis or proposal.
          <button type="button" className="button button--ghost" onClick={onClearFocus}>
            Clear
          </button>
        </div>
      )}

      {visible.length === 0 && (
        <p className="muted">No evidence matches the current filters.</p>
      )}

      {groups.map((group) => (
        <section key={group.source} className="evidence__group">
          <div className="evidence__group-head">
            <h3>{evidenceSourceLabel(group.source)}</h3>
            <span className="muted">{group.items.length} item(s)</span>
          </div>
          <ul className="cards">
            {group.items.map((item) => {
              const hypothesisRefs = citedByHypotheses.get(item.evidence_id) ?? [];
              const proposalRefs = citedByProposals.get(item.evidence_id) ?? [];
              const focused = item.evidence_id === focusedEvidenceId;
              return (
                <li
                  key={item.evidence_id}
                  id={`evidence-${item.evidence_id}`}
                  ref={focused ? focusedRef : undefined}
                  className={`card card--nested evidence__item ${
                    focused ? "evidence__item--focus" : ""
                  }`}
                >
                  <div className="card__head">
                    <span className="mono">{item.evidence_id}</span>
                    <span className="evidence__tags">
                      {item.type && (
                        <span className="tag">{evidenceTypeLabel(item.type)}</span>
                      )}
                      <span className="tag tag--muted">
                        {evidenceSourceLabel(item.source ?? "unknown")}
                      </span>
                    </span>
                  </div>

                  {item.description && (
                    <p className="card__body">{item.description}</p>
                  )}

                  {(item.reference ||
                    item.timestamp ||
                    item.metric ||
                    item.value !== null && item.value !== undefined) && (
                    <div className="evidence__meta-grid">
                      {item.reference && (
                        <EvidenceMeta label="Reference" value={item.reference} />
                      )}
                      {item.metric && (
                        <EvidenceMeta label="Metric" value={item.metric} />
                      )}
                      {item.value !== null && item.value !== undefined && (
                        <EvidenceMeta label="Value" value={String(item.value)} />
                      )}
                      {item.timestamp && (
                        <EvidenceMeta
                          label="Observed"
                          value={formatTimestamp(item.timestamp)}
                        />
                      )}
                    </div>
                  )}

                  {item.excerpt && <pre className="excerpt">{item.excerpt}</pre>}

                  {item.interpretation && (
                    <p className="card__body evidence__interpretation">
                      {item.interpretation}
                    </p>
                  )}

                  {(hypothesisRefs.length > 0 || proposalRefs.length > 0) && (
                    <p className="evidence__links">
                      <span className="muted">Cited by</span>
                      {hypothesisRefs.map((hypothesisId) => (
                        <a
                          key={hypothesisId}
                          className="chip chip--link mono"
                          href={`#hypothesis-${hypothesisId}`}
                        >
                          {hypothesisId}
                        </a>
                      ))}
                      {proposalRefs.map((proposalId) => (
                        <a
                          key={proposalId}
                          className="chip chip--link mono"
                          href={`#proposal-${proposalId}`}
                        >
                          {proposalId}
                        </a>
                      ))}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
