import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ApiError,
  createInvestigation,
  getInvestigation,
  listInvestigations,
  submitApproval,
} from "./client";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("listInvestigations", () => {
  it("returns the investigation IDs from the allowlisted payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ investigations: ["a", "b"] })),
    );

    await expect(listInvestigations()).resolves.toEqual(["a", "b"]);
  });
});

describe("error handling", () => {
  it("maps a backend error body onto a typed ApiError", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ error: "incident_not_found", detail: "Incident 'x' not found." }, 404),
      ),
    );

    await expect(getInvestigation("x")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      code: "incident_not_found",
      message: "Incident 'x' not found.",
    });
  });

  it("falls back to a generic message for a non-JSON error body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("<html>nope</html>", { status: 502 })),
    );

    const error = await getInvestigation("x").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(502);
    expect((error as ApiError).code).toBe("request_failed");
  });

  it("reports an unreachable backend as a network error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("failed to fetch")));

    const error = await listInvestigations().catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("network_error");
    expect((error as ApiError).status).toBe(0);
  });
});

describe("createInvestigation", () => {
  it("posts the backend request shape and opts into async dispatch", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ investigation_id: "inc_01", status: "RUNNING" }, 202),
      );
    vi.stubGlobal("fetch", fetchMock);

    await createInvestigation("inc_01");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/investigations");
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ incident_id: "inc_01" }));
    expect((init.headers as Record<string, string>).Prefer).toBe("respond-async");
  });
});

describe("submitApproval", () => {
  it("posts the backend approval contract and returns the persisted record", async () => {
    const body = {
      investigation_id: "inc_01",
      approval: {
        proposal_id: "FIX-001",
        status: "APPROVED",
        decision: "approved",
        approved_by: "dana",
        timestamp: "2026-08-28T15:00:00Z",
        notes: null,
      },
      status: "APPROVED",
      human_approval_notice:
        "HUMAN REVIEW RECORDED — fix patch has not been applied.",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(body));
    vi.stubGlobal("fetch", fetchMock);

    const response = await submitApproval("inc_01", {
      proposal_id: "FIX-001",
      decision: "approved",
      reviewer: "dana",
      notes: "looks correct",
    });

    expect(response).toEqual(body);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/investigations/inc_01/approval");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Content-Type"]).toBe(
      "application/json",
    );
    expect(init.body).toBe(
      JSON.stringify({
        proposal_id: "FIX-001",
        decision: "approved",
        reviewer: "dana",
        notes: "looks correct",
      }),
    );
  });

  it("surfaces a 409 from a repeated decision as a typed error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse(
          {
            error: "proposal_already_reviewed",
            detail: "Proposal 'FIX-001' has already been reviewed.",
          },
          409,
        ),
      ),
    );

    await expect(
      submitApproval("inc_01", {
        proposal_id: "FIX-001",
        decision: "rejected",
        reviewer: "dana",
      }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
      code: "proposal_already_reviewed",
    });
  });
});
