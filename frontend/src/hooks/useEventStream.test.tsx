import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useEventStream } from "./useEventStream";

/** Minimal EventSource stand-in capturing listeners so tests can emit frames. */
class FakeEventSource {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  static instances: FakeEventSource[] = [];

  readonly url: string;
  readyState = FakeEventSource.CONNECTING;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  private readonly listeners = new Map<string, EventListener[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    const existing = this.listeners.get(type) ?? [];
    existing.push(listener);
    this.listeners.set(type, existing);
  }

  removeEventListener(type: string, listener: EventListener) {
    const existing = this.listeners.get(type) ?? [];
    this.listeners.set(
      type,
      existing.filter((entry) => entry !== listener),
    );
  }

  close() {
    this.closed = true;
    this.readyState = FakeEventSource.CLOSED;
  }

  emit(type: string, payload: unknown) {
    this.readyState = FakeEventSource.OPEN;
    const event = { data: JSON.stringify(payload) } as MessageEvent<string>;
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

function eventFrame(eventId: string, eventType: string) {
  return {
    event_id: eventId,
    investigation_id: "inc_01",
    event_type: eventType,
    timestamp: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    status: "processed",
    retry_count: 0,
    payload: { stage: "logs_agent" },
  };
}

afterEach(() => {
  FakeEventSource.instances = [];
  vi.unstubAllGlobals();
});

describe("useEventStream", () => {
  it("stays idle when disabled", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const { result } = renderHook(() =>
      useEventStream("inc_01", { enabled: false }),
    );

    expect(result.current.connection).toBe("idle");
    expect(FakeEventSource.instances).toHaveLength(0);
  });

  it("appends live events and reports the open state", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const { result } = renderHook(() => useEventStream("inc_01", { enabled: true }));

    const source = FakeEventSource.instances[0];
    expect(source.url).toBe("/investigations/inc_01/events");

    act(() => {
      source.onopen?.();
      source.emit("StageCompleted", eventFrame("evt_1", "StageCompleted"));
      source.emit("InvestigationCompleted", eventFrame("evt_2", "InvestigationCompleted"));
    });

    expect(result.current.connection).toBe("open");
    expect(result.current.events.map((event) => event.event_id)).toEqual([
      "evt_1",
      "evt_2",
    ]);
  });

  it("signals a reconnect when the stream drops", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const { result } = renderHook(() => useEventStream("inc_01", { enabled: true }));

    act(() => {
      FakeEventSource.instances[0].onerror?.();
    });

    expect(result.current.connection).toBe("reconnecting");
    expect(result.current.error).toMatch(/reconnecting/i);
  });

  it("closes the connection on unmount", () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const { unmount } = renderHook(() => useEventStream("inc_01", { enabled: true }));

    const source = FakeEventSource.instances[0];
    unmount();

    expect(source.closed).toBe(true);
  });
});
