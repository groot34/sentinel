import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import {
  CreateInvestigationPage,
  describeSubmitError,
  validateGitHubSelection,
  validateIncidentId,
} from "./CreateInvestigationPage";
import { makeGitHubConnection } from "../test/fixtures";

function WorkspaceStub() {
  const { id } = useParams<{ id: string }>();
  return <div>Workspace for {id}</div>;
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/new"]}>
      <Routes>
        <Route path="/new" element={<CreateInvestigationPage />} />
        <Route path="/investigations/:id" element={<WorkspaceStub />} />
        <Route path="/connections" element={<div>Connections</div>} />
        <Route path="/" element={<div>Dashboard</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** A fetch mock that answers connection reads and the async create call. */
function makeApiMock(connections: unknown[]) {
  return vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      return Promise.resolve(
        jsonResponse({ investigation_id: "inc_02", status: "RUNNING" }, 202),
      );
    }
    if (String(url).startsWith("/integrations/github/connections")) {
      return Promise.resolve(jsonResponse({ connections }));
    }
    return Promise.resolve(jsonResponse({ investigations: [] }));
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("validateIncidentId", () => {
  it("requires a non-empty value", () => {
    expect(validateIncidentId("   ")).toMatch(/required/i);
  });

  it("rejects path traversal and unsafe characters", () => {
    expect(validateIncidentId("../etc")).not.toBeNull();
    expect(validateIncidentId("inc 01")).not.toBeNull();
  });

  it("accepts a canonical incident ID", () => {
    expect(validateIncidentId("inc_01_n_plus_one_query")).toBeNull();
  });
});

describe("describeSubmitError", () => {
  it("explains that a missing bundle must exist on the backend", () => {
    const message = describeSubmitError(
      new ApiError(404, "incident_not_found", "Incident 'x' not found."),
    );
    expect(message).toMatch(/incidents directory/i);
  });
});

describe("CreateInvestigationPage", () => {
  it("blocks submission and shows a validation error when empty", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.click(screen.getByRole("button", { name: /start investigation/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/required/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("submits a valid incident ID and navigates to the created investigation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ investigation_id: "inc_02", status: "RUNNING" }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const user = userEvent.setup();

    renderPage();
    await user.type(screen.getByLabelText(/incident id/i), "inc_02");
    await user.click(screen.getByRole("button", { name: /start investigation/i }));

    expect(await screen.findByText("Workspace for inc_02")).toBeInTheDocument();
  });

  it("shows the backend error when submission fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ error: "incident_not_found", detail: "Incident 'nope' not found." }),
          { status: 404, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    const user = userEvent.setup();

    renderPage();
    await user.type(screen.getByLabelText(/incident id/i), "nope");
    await user.click(screen.getByRole("button", { name: /start investigation/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/incidents directory/i);
    });
    expect(screen.queryByText(/Workspace for/)).not.toBeInTheDocument();
  });
});

describe("validateGitHubSelection", () => {
  it("is only required once GitHub enrichment is switched on", () => {
    expect(validateGitHubSelection(false, "")).toBeNull();
    expect(validateGitHubSelection(true, "")).toMatch(/select a github connection/i);
    expect(validateGitHubSelection(true, "ghconn_01")).toBeNull();
  });
});

describe("CreateInvestigationPage GitHub integration", () => {
  it("keeps bundle-only creation unchanged and issues no GitHub request", async () => {
    const fetchMock = makeApiMock([]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.type(screen.getByLabelText(/incident id/i), "inc_02");
    await user.click(screen.getByRole("button", { name: /start investigation/i }));

    expect(await screen.findByText("Workspace for inc_02")).toBeInTheDocument();

    // Exactly one request (the create call) — the connections list is never read.
    const postCalls = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(postCalls).toHaveLength(1);
    expect(postCalls[0][0]).toBe("/investigations");
    expect(JSON.parse(String(postCalls[0][1]?.body))).toEqual({
      incident_id: "inc_02",
    });
    expect(
      fetchMock.mock.calls.some(([url]) =>
        String(url).startsWith("/integrations/github/connections"),
      ),
    ).toBe(false);
  });

  it("selects a connection during creation and sends its identifier", async () => {
    const fetchMock = makeApiMock([
      makeGitHubConnection({ connection_id: "ghconn_01", owner: "acme", repo: "checkout" }),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.type(screen.getByLabelText(/incident id/i), "inc_02");
    await user.click(screen.getByLabelText(/enrich with a github repository/i));

    // A single connection is auto-selected once the list loads.
    expect(await screen.findByRole("option", { name: "acme/checkout" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /start investigation/i }));

    expect(await screen.findByText("Workspace for inc_02")).toBeInTheDocument();
    const postCall = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      incident_id: "inc_02",
      github_connection_id: "ghconn_01",
    });
  });

  it("submits a GitHub reference together with the connection", async () => {
    const fetchMock = makeApiMock([
      makeGitHubConnection({ connection_id: "ghconn_01", owner: "acme", repo: "checkout" }),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.type(screen.getByLabelText(/incident id/i), "inc_02");
    await user.click(screen.getByLabelText(/enrich with a github repository/i));
    await screen.findByRole("option", { name: "acme/checkout" });

    await user.type(screen.getByLabelText(/reference \(optional\)/i), "release/2026.08");
    await user.click(screen.getByRole("button", { name: /start investigation/i }));

    expect(await screen.findByText("Workspace for inc_02")).toBeInTheDocument();
    const postCall = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      incident_id: "inc_02",
      github_connection_id: "ghconn_01",
      github_ref: "release/2026.08",
    });
  });

  it("does not silently fall back to bundle-only when no connection is selected", async () => {
    const fetchMock = makeApiMock([
      makeGitHubConnection({ connection_id: "ghconn_01", owner: "acme", repo: "checkout" }),
      makeGitHubConnection({ connection_id: "ghconn_02", owner: "acme", repo: "legacy" }),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.type(screen.getByLabelText(/incident id/i), "inc_02");
    await user.click(screen.getByLabelText(/enrich with a github repository/i));
    await screen.findByRole("option", { name: "acme/checkout" });

    // Two usable connections => no auto-selection; the operator must choose.
    await user.click(screen.getByRole("button", { name: /start investigation/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /select a github connection/i,
    );
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);
    expect(screen.queryByText(/Workspace for/)).not.toBeInTheDocument();
  });

  it("surfaces a disconnected-connection error without switching to bundle-only", async () => {
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(
          jsonResponse(
            {
              error: "github_connection_unusable",
              detail: "GitHub connection 'ghconn_01' is not usable.",
            },
            409,
          ),
        );
      }
      if (String(url).startsWith("/integrations/github/connections")) {
        return Promise.resolve(
          jsonResponse({
            connections: [
              makeGitHubConnection({ connection_id: "ghconn_01", owner: "acme", repo: "checkout" }),
            ],
          }),
        );
      }
      return Promise.resolve(jsonResponse({ investigations: [] }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.type(screen.getByLabelText(/incident id/i), "inc_02");
    await user.click(screen.getByLabelText(/enrich with a github repository/i));
    await screen.findByRole("option", { name: "acme/checkout" });
    await user.click(screen.getByRole("button", { name: /start investigation/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(
        /verify the connection on the github connections page/i,
      );
    });
    expect(screen.queryByText(/Workspace for/)).not.toBeInTheDocument();
  });

  it("reports a disabled connection as unusable in the picker", async () => {
    const fetchMock = makeApiMock([
      makeGitHubConnection({
        connection_id: "ghconn_off",
        owner: "acme",
        repo: "archived",
        status: "disabled",
      }),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.click(screen.getByLabelText(/enrich with a github repository/i));

    const option = await screen.findByRole("option", { name: /acme\/archived \(disabled\)/i });
    expect(option).toBeDisabled();
  });

  it("auto-selects the connection when exactly one is connected", async () => {
    const fetchMock = makeApiMock([
      makeGitHubConnection({ connection_id: "ghconn_01", owner: "acme", repo: "checkout" }),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.click(screen.getByLabelText(/enrich with a github repository/i));

    await screen.findByRole("option", { name: "acme/checkout" });
    expect(screen.getByLabelText(/github connection/i)).toHaveValue("ghconn_01");
  });

  it("does not auto-select the only connection when it is disabled", async () => {
    const fetchMock = makeApiMock([
      makeGitHubConnection({
        connection_id: "ghconn_off",
        owner: "acme",
        repo: "archived",
        status: "disabled",
      }),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.click(screen.getByLabelText(/enrich with a github repository/i));

    await screen.findByRole("option", { name: /acme\/archived \(disabled\)/i });
    expect(screen.getByLabelText(/github connection/i)).toHaveValue("");
  });

  it("does not auto-select the only connection when it is in error", async () => {
    const fetchMock = makeApiMock([
      makeGitHubConnection({
        connection_id: "ghconn_err",
        owner: "acme",
        repo: "broken",
        status: "error",
      }),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.click(screen.getByLabelText(/enrich with a github repository/i));

    await screen.findByRole("option", { name: /acme\/broken \(error\)/i });
    expect(screen.getByLabelText(/github connection/i)).toHaveValue("");
  });

  it("auto-selects the single connected connection among several", async () => {
    const fetchMock = makeApiMock([
      makeGitHubConnection({
        connection_id: "ghconn_ok",
        owner: "acme",
        repo: "checkout",
        status: "connected",
      }),
      makeGitHubConnection({
        connection_id: "ghconn_off",
        owner: "acme",
        repo: "archived",
        status: "disabled",
      }),
      makeGitHubConnection({
        connection_id: "ghconn_err",
        owner: "acme",
        repo: "broken",
        status: "error",
      }),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.click(screen.getByLabelText(/enrich with a github repository/i));

    await screen.findByRole("option", { name: "acme/checkout" });
    expect(screen.getByLabelText(/github connection/i)).toHaveValue("ghconn_ok");
  });

  it("requires explicit selection when multiple connections are connected", async () => {
    const fetchMock = makeApiMock([
      makeGitHubConnection({
        connection_id: "ghconn_01",
        owner: "acme",
        repo: "checkout",
        status: "connected",
      }),
      makeGitHubConnection({
        connection_id: "ghconn_02",
        owner: "acme",
        repo: "payments",
        status: "connected",
      }),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.click(screen.getByLabelText(/enrich with a github repository/i));

    await screen.findByRole("option", { name: "acme/checkout" });
    await screen.findByRole("option", { name: "acme/payments" });
    expect(screen.getByLabelText(/github connection/i)).toHaveValue("");
  });

  it("cannot accidentally submit an unusable connection", async () => {
    const fetchMock = makeApiMock([
      makeGitHubConnection({
        connection_id: "ghconn_off",
        owner: "acme",
        repo: "archived",
        status: "disabled",
      }),
    ]);
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await user.type(screen.getByLabelText(/incident id/i), "inc_02");
    await user.click(screen.getByLabelText(/enrich with a github repository/i));
    await screen.findByRole("option", { name: /acme\/archived \(disabled\)/i });

    // The unusable connection is never selected...
    expect(screen.getByLabelText(/github connection/i)).toHaveValue("");

    await user.click(screen.getByRole("button", { name: /start investigation/i }));

    // ...submission is blocked with a validation message, and no POST is sent.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /select a github connection/i,
    );
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "POST"),
    ).toBe(false);
    expect(screen.queryByText(/Workspace for/)).not.toBeInTheDocument();
  });
});
