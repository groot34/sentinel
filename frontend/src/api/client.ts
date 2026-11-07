/**
 * Thin typed client for the Sentinel 2.0 HTTP API.
 *
 * The base URL is read from `VITE_SENTINEL_API_BASE_URL`. It defaults to an
 * empty string so requests are same-origin, which the Vite dev server proxies
 * to the backend (see `vite.config.ts`). Only set the variable when the
 * frontend is served from a different origin than the API.
 */

import type {
  AcceptedInvestigation,
  EventHistoryResponse,
  InvestigationDetail,
  InvestigationResult,
  ListInvestigationsResponse,
  SubmitApprovalRequest,
  SubmitApprovalResponse,
} from "./types";

export const API_BASE_URL: string = (
  import.meta.env.VITE_SENTINEL_API_BASE_URL ?? ""
).replace(/\/+$/, "");

/** Canonical incident-ID rule, shared with the backend request model. */
export const INCIDENT_ID_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/;

/** Error thrown for any non-2xx API response or transport failure. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/** Build an absolute URL for an API path, honouring the configured base URL. */
export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}

/**
 * Parse an error response body into an {@link ApiError}.
 *
 * The backend returns `{"error": <code>, "detail": <message>}`; anything else
 * (HTML error pages, empty bodies, network failures) falls back to a generic
 * message so the UI always has something meaningful to show.
 */
async function toApiError(response: Response): Promise<ApiError> {
  let code = "request_failed";
  let detail = `Request failed with status ${response.status}.`;
  try {
    const body = (await response.json()) as Partial<{
      error: string;
      detail: string;
    }>;
    if (typeof body.error === "string") code = body.error;
    if (typeof body.detail === "string" && body.detail) detail = body.detail;
  } catch {
    // Non-JSON body — keep the generic message built above.
  }
  return new ApiError(response.status, code, detail);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(apiUrl(path), init);
  } catch {
    throw new ApiError(
      0,
      "network_error",
      `Could not reach the Sentinel API at ${apiUrl(path)}. Is the backend running?`,
    );
  }
  if (!response.ok) {
    throw await toApiError(response);
  }
  return (await response.json()) as T;
}

/** `GET /investigations` — every stored investigation ID. */
export async function listInvestigations(): Promise<string[]> {
  const data = await request<ListInvestigationsResponse>("/investigations");
  return data.investigations;
}

/** `GET /investigations/{id}` — persisted investigation state. */
export function getInvestigation(id: string): Promise<InvestigationDetail> {
  return request<InvestigationDetail>(
    `/investigations/${encodeURIComponent(id)}`,
  );
}

/** `GET /investigations/{id}/result` — diagnosis (evidence, hypotheses, …). */
export function getInvestigationResult(
  id: string,
): Promise<InvestigationResult> {
  return request<InvestigationResult>(
    `/investigations/${encodeURIComponent(id)}/result`,
  );
}

/** `GET /investigations/{id}/events` (JSON) — durable event history snapshot. */
export function getEventHistory(id: string): Promise<EventHistoryResponse> {
  return request<EventHistoryResponse>(
    `/investigations/${encodeURIComponent(id)}/events`,
    { headers: { Accept: "application/json" } },
  );
}

/**
 * `POST /investigations` — start a real investigation.
 *
 * `Prefer: respond-async` asks the backend to accept the run and return
 * `202 Accepted` immediately, so the UI can navigate straight to the workspace
 * and follow progress over SSE instead of blocking on the whole pipeline.
 */
export function createInvestigation(
  incidentId: string,
): Promise<AcceptedInvestigation> {
  return request<AcceptedInvestigation>("/investigations", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Prefer: "respond-async",
    },
    body: JSON.stringify({ incident_id: incidentId }),
  });
}

/** URL of the SSE stream for one investigation (used by `EventSource`). */
export function eventStreamUrl(id: string): string {
  return apiUrl(`/investigations/${encodeURIComponent(id)}/events`);
}

/**
 * `POST /investigations/{id}/approval` — record a human decision for one fix
 * proposal.
 *
 * The backend records the reviewer's decision and returns the persisted
 * approval record. It does NOT apply the patch, write files, commit or deploy;
 * `human_approval_notice` in the response states this explicitly.
 */
export function submitApproval(
  id: string,
  body: SubmitApprovalRequest,
): Promise<SubmitApprovalResponse> {
  return request<SubmitApprovalResponse>(
    `/investigations/${encodeURIComponent(id)}/approval`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}
