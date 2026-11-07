import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EventTimeline } from "./EventTimeline";
import { makeEvent } from "../test/fixtures";

const events = [
  makeEvent("e1", "InvestigationStarted", { initial_stage: "logs" }),
  makeEvent("e2", "StageStarted", { stage: "logs" }),
  makeEvent("e3", "StageCompleted", { stage: "logs", llm_calls: 1, total_tokens: 120 }),
  makeEvent("e4", "StageStarted", { stage: "metrics" }),
  makeEvent("e5", "StageFailed", { stage: "metrics", error: "metrics tool failed" }),
  makeEvent("e6", "InvestigationCompleted", { status: "PARTIAL", confirmed_hypotheses: 1 }),
];

describe("EventTimeline", () => {
  it("groups events by pipeline stage and shows each stage's status", () => {
    render(
      <EventTimeline
        events={events}
        connection="open"
        streamError={null}
        historySupported
      />,
    );

    expect(screen.getByRole("heading", { name: "Investigation" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Logs" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Metrics" })).toBeInTheDocument();
    expect(screen.getByText("SUCCEEDED")).toBeInTheDocument();
    expect(screen.getByText("FAILED")).toBeInTheDocument();
  });

  it("summarizes event and stage progress", () => {
    render(
      <EventTimeline events={events} connection="open" streamError={null} historySupported />,
    );

    expect(screen.getByText(/6 event\(s\)/)).toBeInTheDocument();
    expect(screen.getByText(/1\/2 stage\(s\) completed/)).toBeInTheDocument();
    expect(screen.getByText(/1 failed/)).toBeInTheDocument();
  });

  it("renders readable detail lines instead of raw payloads", () => {
    render(
      <EventTimeline events={events} connection="open" streamError={null} historySupported />,
    );

    expect(screen.getByText(/120 tokens/)).toBeInTheDocument();
    expect(screen.getByText(/status: PARTIAL/)).toBeInTheDocument();
    expect(screen.getByText(/metrics tool failed/)).toBeInTheDocument();
  });

  it("reports the connection state", () => {
    const { rerender } = render(
      <EventTimeline events={events} connection="open" streamError={null} historySupported />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Live");

    rerender(
      <EventTimeline
        events={events}
        connection="reconnecting"
        streamError="Lost connection to the event stream. Reconnecting…"
        historySupported
      />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Reconnecting");
    expect(
      screen.getByText("Lost connection to the event stream. Reconnecting…"),
    ).toBeInTheDocument();
  });

  it("explains when the persistence backend has no durable event history", () => {
    render(
      <EventTimeline events={[]} connection="idle" streamError={null} historySupported={false} />,
    );

    expect(
      screen.getByText(/does not store durable event history/),
    ).toBeInTheDocument();
  });

  it("shows an empty hint before any event arrives", () => {
    render(
      <EventTimeline events={[]} connection="connecting" streamError={null} historySupported />,
    );

    expect(screen.getByText("No events yet.")).toBeInTheDocument();
  });
});
