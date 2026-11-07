import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import {
  CreateInvestigationPage,
  describeSubmitError,
  validateIncidentId,
} from "./CreateInvestigationPage";

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
        <Route path="/" element={<div>Dashboard</div>} />
      </Routes>
    </MemoryRouter>,
  );
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
