import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  ApiError,
  getEventHistory,
  getInvestigation,
  getInvestigationResult,
  submitApproval,
} from "../api/client";
import type {
  EventHistoryItem,
  InvestigationDetail,
  InvestigationResult,
} from "../api/types";
import { isTerminalStatus } from "../api/types";
import { EventTimeline } from "../components/EventTimeline";
import type { ProposalDecisionInput } from "../components/ProposalCard";
import { ResultPanel } from "../components/ResultPanel";
import { StatusBadge } from "../components/StatusBadge";
import { ErrorState, LoadingState } from "../components/States";
import { useEventStream } from "../hooks/useEventStream";
import {
  formatDuration,
  formatTimestamp,
  humanizeStageName,
} from "../lib/format";
import { upsertApproval } from "../lib/proposals";

/** Merge replayed history with live events, dropping duplicate event IDs. */
export function mergeEvents(
  history: EventHistoryItem[],
  live: EventHistoryItem[],
): EventHistoryItem[] {
  const seen = new Set<string>();
  const merged: EventHistoryItem[] = [];
  for (const event of [...history, ...live]) {
    if (seen.has(event.event_id)) continue;
    seen.add(event.event_id);
    merged.push(event);
  }
  return merged;
}

/**
 * Investigation workspace.
 *
 * Shows the persisted state and diagnosis, replays durable event history, then
 * follows the run live over SSE until it reaches a terminal state. A finished
 * investigation stays fully useful: its stages, history and diagnosis are all
 * rendered without any live connection.
 */
export function WorkspacePage() {
  const { id: rawId } = useParams<{ id: string }>();
  const investigationId = rawId ?? "";

  const [detail, setDetail] = useState<InvestigationDetail | null>(null);
  const [result, setResult] = useState<InvestigationResult | null>(null);
  const [history, setHistory] = useState<EventHistoryItem[]>([]);
  const [historySupported, setHistorySupported] = useState(true);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    if (!investigationId) return;
    setLoading(true);
    setLoadError(null);
    setNotFound(false);
    try {
      const [detailData, resultData, historyData] = await Promise.all([
        getInvestigation(investigationId),
        getInvestigationResult(investigationId).catch(() => null),
        getEventHistory(investigationId).catch(() => null),
      ]);
      setDetail(detailData);
      setResult(resultData);
      setHistory(historyData?.events ?? []);
      setHistorySupported(historyData?.backend_supports_event_history ?? true);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        setNotFound(true);
      } else if (error instanceof ApiError) {
        setLoadError(error.message);
      } else if (error instanceof Error) {
        setLoadError(error.message);
      } else {
        setLoadError("An unexpected error occurred.");
      }
    } finally {
      setLoading(false);
    }
  }, [investigationId]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  /** Re-fetch state (and diagnosis/history once finished) without a full reload. */
  const refresh = useCallback(async () => {
    if (!investigationId) return;
    try {
      const detailData = await getInvestigation(investigationId);
      setDetail(detailData);
      if (isTerminalStatus(detailData.status)) {
        const [resultData, historyData] = await Promise.all([
          getInvestigationResult(investigationId).catch(() => null),
          getEventHistory(investigationId).catch(() => null),
        ]);
        if (resultData) setResult(resultData);
        if (historyData) {
          setHistory(historyData.events);
          setHistorySupported(historyData.backend_supports_event_history);
        }
      }
    } catch {
      // A transient refresh failure leaves the last good state on screen.
    }
  }, [investigationId]);

  const streaming = Boolean(
    detail && historySupported && !isTerminalStatus(detail.status),
  );

  const handleEvent = useCallback(
    (event: EventHistoryItem) => {
      const settleTypes = new Set([
        "StageStarted",
        "StageCompleted",
        "StageFailed",
        "InvestigationCompleted",
      ]);
      if (settleTypes.has(event.event_type)) {
        void refresh();
      }
    },
    [refresh],
  );

  const { events: liveEvents, connection, error: streamError } = useEventStream(
    investigationId,
    { enabled: streaming, onEvent: handleEvent },
  );

  const events = useMemo(
    () => mergeEvents(history, liveEvents),
    [history, liveEvents],
  );

  /**
   * Record a human approve/reject decision through the real approval endpoint
   * and fold the persisted record back into the loaded detail, so the proposal
   * reflects exactly what the backend stored. Nothing here applies a patch.
   */
  const handleDecide = useCallback(
    async (input: ProposalDecisionInput) => {
      const response = await submitApproval(investigationId, {
        proposal_id: input.proposalId,
        decision: input.decision,
        reviewer: input.reviewer,
        ...(input.notes ? { notes: input.notes } : {}),
      });
      setDetail((previous) =>
        previous
          ? {
              ...previous,
              approvals: upsertApproval(previous.approvals, response.approval),
            }
          : previous,
      );
      return response;
    },
    [investigationId],
  );

  if (loading) {
    return <LoadingState message="Loading investigation…" />;
  }

  if (notFound) {
    return (
      <ErrorState
        title="Investigation not found"
        message={`No investigation with ID '${investigationId}' exists.`}
      />
    );
  }

  if (loadError || !detail) {
    return (
      <ErrorState
        title="Could not load investigation"
        message={loadError ?? "The investigation could not be loaded."}
        onRetry={() => void loadAll()}
      />
    );
  }

  const duration = formatDuration(detail.started_at, detail.completed_at);
  const stages = Object.entries(detail.stages);
  const settledStages = stages.filter(([, stage]) =>
    ["SUCCEEDED", "FAILED", "SKIPPED", "CACHED"].includes(stage.status),
  ).length;

  return (
    <section className="page">
      <div className="page__head">
        <div>
          <p className="breadcrumb">
            <Link to="/">Investigations</Link> / {detail.investigation_id}
          </p>
          <h1 className="mono">{detail.investigation_id}</h1>
        </div>
        <div className="page__actions">
          <StatusBadge status={detail.status} />
          <button type="button" className="button" onClick={() => void refresh()}>
            Refresh
          </button>
        </div>
      </div>

      <div className="facts">
        <div className="fact">
          <span className="fact__label">Current stage</span>
          <span className="fact__value">
            {detail.current_stage
              ? humanizeStageName(detail.current_stage)
              : "—"}
          </span>
        </div>
        <div className="fact">
          <span className="fact__label">Started</span>
          <span className="fact__value">{formatTimestamp(detail.started_at)}</span>
        </div>
        <div className="fact">
          <span className="fact__label">Completed</span>
          <span className="fact__value">{formatTimestamp(detail.completed_at)}</span>
        </div>
        <div className="fact">
          <span className="fact__label">Duration</span>
          <span className="fact__value">{duration ?? "—"}</span>
        </div>
        <div className="fact">
          <span className="fact__label">LLM calls</span>
          <span className="fact__value">{detail.llm_call_count}</span>
        </div>
      </div>

      {detail.error && (
        <div className="alert alert--error" role="alert">
          {detail.error}
        </div>
      )}

      {stages.length > 0 && (
        <section className="panel">
          <div className="panel__head">
            <h2>Pipeline stages</h2>
            <span className="muted">
              {settledStages}/{stages.length} settled
            </span>
          </div>
          <ul className="stages">
            {stages.map(([name, stage]) => (
              <li key={name} className="stages__item">
                <span className="stages__name">{humanizeStageName(name)}</span>
                <StatusBadge status={stage.status} />
                {stage.total_tokens > 0 && (
                  <span className="muted">{stage.total_tokens} tokens</span>
                )}
                {stage.error && <span className="stages__error">{stage.error}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}

      <EventTimeline
        events={events}
        connection={connection}
        streamError={streamError}
        historySupported={historySupported}
      />

      {result && (
        <ResultPanel
          result={result}
          approvals={detail.approvals}
          onDecide={handleDecide}
        />
      )}
    </section>
  );
}
