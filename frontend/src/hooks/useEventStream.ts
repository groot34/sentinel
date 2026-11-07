/**
 * Live investigation event stream hook.
 *
 * Wraps the browser `EventSource` around the backend SSE endpoint
 * (`GET /investigations/{id}/events` with `Accept: text/event-stream`).
 *
 * The backend names every SSE frame (`event: StageCompleted`, …), so a default
 * `onmessage` handler never fires — one listener is registered per known event
 * type. `EventSource` reconnects automatically and replays the `Last-Event-ID`
 * cursor, so the backend resumes the stream without duplicating events; this
 * hook only surfaces the reconnect state to the UI.
 *
 * A browser `EventSource` cannot send an `Authorization` header. When the
 * backend's API-key guard protects reads, this stream will fail closed and the
 * workspace reports the connection error instead of showing stale data.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { eventStreamUrl } from "../api/client";
import type { EventHistoryItem } from "../api/types";

/** Every named event the backend can emit, plus the two control frames. */
export const SSE_EVENT_TYPES = [
  "InvestigationStarted",
  "InvestigationResumed",
  "StageStarted",
  "StageCompleted",
  "StageFailed",
  "StageCached",
  "StageSkipped",
  "InvestigationCompleted",
  "notice",
  "error",
] as const;

export type StreamConnectionState =
  | "idle"
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed";

export interface EventStreamResult {
  /** Events received so far, in arrival order. */
  events: EventHistoryItem[];
  /** Current connection state, for status messaging. */
  connection: StreamConnectionState;
  /** Last connection error message, cleared on a successful (re)connect. */
  error: string | null;
}

/**
 * Subscribe to an investigation's live SSE stream.
 *
 * @param investigationId Target investigation, or null to stay disconnected.
 * @param options.enabled When false, no connection is opened (e.g. a finished
 *   investigation whose events were already loaded as history).
 * @param options.onEvent Called for every event as it arrives, for consumers
 *   that need to react (refresh status, mark completion).
 */
export function useEventStream(
  investigationId: string | null,
  options: { enabled: boolean; onEvent?: (event: EventHistoryItem) => void },
): EventStreamResult {
  const [events, setEvents] = useState<EventHistoryItem[]>([]);
  const [connection, setConnection] = useState<StreamConnectionState>("idle");
  const [error, setError] = useState<string | null>(null);

  // Keep the latest callback without re-opening the stream when it changes.
  const onEventRef = useRef(options.onEvent);
  useEffect(() => {
    onEventRef.current = options.onEvent;
  }, [options.onEvent]);

  const { enabled } = options;

  const handleFrame = useCallback((raw: MessageEvent<string>) => {
    let parsed: EventHistoryItem;
    try {
      parsed = JSON.parse(raw.data) as EventHistoryItem;
    } catch {
      // Ignore frames we cannot parse rather than breaking the stream.
      return;
    }
    setEvents((previous) => [...previous, parsed]);
    onEventRef.current?.(parsed);
  }, []);

  useEffect(() => {
    if (!enabled || !investigationId) {
      setConnection("idle");
      return;
    }

    // jsdom (and very old browsers) may not implement EventSource.
    if (typeof EventSource === "undefined") {
      setConnection("closed");
      setError("This browser does not support live event streaming.");
      return;
    }

    setConnection("connecting");
    const source = new EventSource(eventStreamUrl(investigationId));

    // `onopen` fires on the initial connect and on every transparent reconnect.
    source.onopen = () => {
      setConnection("open");
      setError(null);
    };

    source.onerror = () => {
      if (source.readyState === EventSource.CLOSED) {
        setConnection("closed");
        setError("The live event stream was closed by the server.");
      } else {
        // The browser is retrying; the UI shows a reconnecting hint.
        setConnection("reconnecting");
        setError("Lost connection to the event stream. Reconnecting…");
      }
    };

    for (const eventType of SSE_EVENT_TYPES) {
      source.addEventListener(eventType, handleFrame as EventListener);
    }

    return () => {
      for (const eventType of SSE_EVENT_TYPES) {
        source.removeEventListener(eventType, handleFrame as EventListener);
      }
      source.close();
      setConnection("idle");
    };
  }, [enabled, investigationId, handleFrame]);

  return { events, connection, error };
}
