import { describe, expect, it } from "vitest";

import type { EventHistoryItem } from "../api/types";
import { summarizeEvent } from "../components/EventTimeline";
import { mergeEvents } from "./WorkspacePage";

function makeEvent(id: string, type: string, payload: Record<string, unknown> = {}) {
  const item: EventHistoryItem = {
    event_id: id,
    investigation_id: "inc_01",
    event_type: type,
    created_at: "2026-01-01T00:00:00Z",
    status: "processed",
    retry_count: 0,
    payload,
  };
  return item;
}

describe("mergeEvents", () => {
  it("keeps history order and appends new live events", () => {
    const history = [makeEvent("evt_1", "StageStarted"), makeEvent("evt_2", "StageCompleted")];
    const live = [makeEvent("evt_3", "StageStarted")];

    expect(mergeEvents(history, live).map((e) => e.event_id)).toEqual([
      "evt_1",
      "evt_2",
      "evt_3",
    ]);
  });

  it("drops live events that were already replayed from history", () => {
    const history = [makeEvent("evt_1", "StageStarted")];
    const live = [makeEvent("evt_1", "StageStarted"), makeEvent("evt_2", "StageCompleted")];

    expect(mergeEvents(history, live).map((e) => e.event_id)).toEqual([
      "evt_1",
      "evt_2",
    ]);
  });
});

describe("summarizeEvent", () => {
  it("includes the stage and token counts for a completed stage", () => {
    const summary = summarizeEvent(
      makeEvent("evt_1", "StageCompleted", {
        stage: "metrics_agent",
        total_tokens: 120,
        item_count: 4,
      }),
    );
    expect(summary).toContain("metrics_agent");
    expect(summary).toContain("120 tokens");
    expect(summary).toContain("4 item(s)");
  });

  it("surfaces the error for a failed stage", () => {
    const summary = summarizeEvent(
      makeEvent("evt_1", "StageFailed", { stage: "code_agent", error: "boom" }),
    );
    expect(summary).toContain("boom");
  });
});
