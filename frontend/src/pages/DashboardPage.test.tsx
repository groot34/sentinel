import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { DashboardPage } from "./DashboardPage";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderDashboard() {
  return render(
    <MemoryRouter>
      <DashboardPage />
    </MemoryRouter>,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("DashboardPage", () => {
  it("shows an empty state when the backend has no investigations", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ investigations: [] })),
    );

    renderDashboard();

    expect(await screen.findByText(/no investigations yet/i)).toBeInTheDocument();
  });

  it("lists investigations with status, ordered newest first", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url === "/investigations") {
        return Promise.resolve(jsonResponse({ investigations: ["older", "newer"] }));
      }
      if (url === "/investigations/older") {
        return Promise.resolve(
          jsonResponse({
            investigation_id: "older",
            status: "FAILED",
            current_stage: null,
            started_at: "2026-01-01T00:00:00Z",
            completed_at: "2026-01-01T00:05:00Z",
            llm_call_count: 1,
            version: 1,
            stages: {},
            approvals: [],
          }),
        );
      }
      if (url === "/investigations/newer") {
        return Promise.resolve(
          jsonResponse({
            investigation_id: "newer",
            status: "RUNNING",
            current_stage: "hypothesis_engine",
            started_at: "2026-02-01T00:00:00Z",
            completed_at: null,
            llm_call_count: 3,
            version: 2,
            stages: {},
            approvals: [],
          }),
        );
      }
      return Promise.reject(new Error(`unexpected url ${url}`));
    });
    vi.stubGlobal("fetch", fetchMock);

    renderDashboard();

    const rows = await screen.findAllByRole("row");
    // rows[0] is the header row; rows[1] must be the newest investigation.
    expect(rows[1]).toHaveTextContent("newer");
    expect(rows[2]).toHaveTextContent("older");
    expect(screen.getByText("RUNNING")).toBeInTheDocument();
    expect(screen.getByText("FAILED")).toBeInTheDocument();
  });

  it("reports a load failure with a retry action", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ error: "persistence_unavailable", detail: "Backend down." }, 503),
      ),
    );

    renderDashboard();

    expect(await screen.findByRole("alert")).toHaveTextContent(/backend down/i);
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });
});
