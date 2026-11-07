import type { EventHistoryItem } from "../api/types";
import type { StreamConnectionState } from "../hooks/useEventStream";
import {
  groupEventsByStage,
  stageProgress,
  type StageEventGroup,
  type StageEventStatus,
} from "../lib/events";
import { formatTimestamp, humanizeEventType } from "../lib/format";
import { StatusBadge } from "./StatusBadge";

/**
 * Build a short, readable detail line from an event's payload.
 *
 * Only allowlisted payload keys are read (the backend already strips anything
 * else), and nothing is rendered as a raw payload dump.
 */
export function summarizeEvent(event: EventHistoryItem): string {
  const payload = event.payload ?? {};
  const parts: string[] = [];
  const stage = payload.stage ?? payload.initial_stage ?? payload.resume_stage;

  if (typeof stage === "string" && stage) {
    parts.push(stage);
  }
  if (typeof payload.status === "string" && payload.status) {
    parts.push(`status: ${payload.status}`);
  }
  if (typeof payload.llm_calls === "number" && payload.llm_calls > 0) {
    parts.push(`${payload.llm_calls} LLM call(s)`);
  } else if (
    typeof payload.total_llm_calls === "number" &&
    payload.total_llm_calls > 0
  ) {
    parts.push(`${payload.total_llm_calls} LLM call(s) total`);
  }
  if (typeof payload.total_tokens === "number" && payload.total_tokens > 0) {
    parts.push(`${payload.total_tokens} tokens`);
  }
  if (typeof payload.item_count === "number") {
    parts.push(`${payload.item_count} item(s)`);
  }
  if (typeof payload.confirmed_hypotheses === "number") {
    parts.push(`${payload.confirmed_hypotheses} confirmed`);
  }
  if (
    typeof payload.proposals_generated === "number" &&
    payload.proposals_generated > 0
  ) {
    parts.push(`${payload.proposals_generated} proposal(s)`);
  }
  if (typeof payload.reason === "string" && payload.reason) {
    parts.push(payload.reason);
  }
  if (typeof payload.error === "string" && payload.error) {
    parts.push(payload.error);
  }
  return parts.join(" · ");
}

interface EventTimelineProps {
  events: EventHistoryItem[];
  connection: StreamConnectionState;
  streamError: string | null;
  historySupported: boolean;
}

const CONNECTION_LABEL: Record<StreamConnectionState, string> = {
  idle: "Not streaming",
  connecting: "Connecting…",
  open: "Live",
  reconnecting: "Reconnecting…",
  closed: "Stream closed",
};

/** Badge label for a stage's latest implied status. */
const STAGE_STATUS_LABEL: Record<StageEventStatus, string> = {
  running: "RUNNING",
  succeeded: "SUCCEEDED",
  failed: "FAILED",
  skipped: "SKIPPED",
  cached: "CACHED",
};

function StageGroup({ group }: { group: StageEventGroup }) {
  return (
    <section className="timeline__group">
      <div className="timeline__group-head">
        <h3>{group.label}</h3>
        {group.status && <StatusBadge status={STAGE_STATUS_LABEL[group.status]} />}
        <span className="muted">{group.events.length} event(s)</span>
        {group.completedAt && (
          <time className="timeline__time muted">
            {formatTimestamp(group.completedAt)}
          </time>
        )}
      </div>
      <ol className="timeline__events">
        {group.events.map((event) => (
          <li key={event.event_id} className="timeline__item">
            <div className="timeline__meta">
              <span className="timeline__type">
                {humanizeEventType(event.event_type)}
              </span>
              <time className="timeline__time">
                {formatTimestamp(event.timestamp ?? event.created_at)}
              </time>
            </div>
            {summarizeEvent(event) && (
              <p className="timeline__detail">{summarizeEvent(event)}</p>
            )}
          </li>
        ))}
      </ol>
    </section>
  );
}

/**
 * Agent activity for an investigation.
 *
 * Events are grouped by pipeline stage (with investigation-level events in a
 * leading group) and each group shows its latest implied status, so progress is
 * readable at a glance. Every event keeps its `event_id` as React key, so a
 * reconnect that replays frames or a refresh that replaces the history cannot
 * produce duplicate entries.
 */
export function EventTimeline({
  events,
  connection,
  streamError,
  historySupported,
}: EventTimelineProps) {
  const groups = groupEventsByStage(events);
  const progress = stageProgress(groups);

  return (
    <section className="panel">
      <div className="panel__head">
        <h2>Agent activity</h2>
        <span className={`conn conn--${connection}`} role="status" aria-live="polite">
          <span className="conn__dot" aria-hidden="true" />
          {CONNECTION_LABEL[connection]}
        </span>
      </div>

      {!historySupported && (
        <div className="alert alert--info" role="note">
          The active persistence backend does not store durable event history, so
          no events are available for this investigation.
        </div>
      )}

      {streamError && (
        <div className="alert alert--error" role="alert">
          {streamError}
        </div>
      )}

      {historySupported && events.length === 0 && (
        <p className="muted">No events yet.</p>
      )}

      {events.length > 0 && (
        <>
          <p className="timeline__summary muted">
            {events.length} event(s) · {progress.completed}/{progress.total}{" "}
            stage(s) completed
            {progress.running > 0 && ` · ${progress.running} running`}
            {progress.failed > 0 && ` · ${progress.failed} failed`}
          </p>
          <div className="timeline" data-testid="event-timeline">
            {groups.map((group) => (
              <StageGroup key={group.stage ?? "lifecycle"} group={group} />
            ))}
          </div>
        </>
      )}
    </section>
  );
}
