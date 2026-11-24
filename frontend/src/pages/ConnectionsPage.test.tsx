import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ConnectionsPage, validateRepository } from "./ConnectionsPage";
import { makeGitHubConnection } from "../test/fixtures";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/connections"]}>
      <ConnectionsPage />
    </MemoryRouter>,
  );
}

/** A deferred promise so a test can assert on the in-flight state. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("validateRepository", () => {
  it("requires both owner and repository name", () => {
    expect(validateRepository("", "checkout")).toMatch(/required/i);
    expect(validateRepository("acme", "  ")).toMatch(/required/i);
  });

  it("accepts a complete owner/repo pair", () => {
    expect(validateRepository("acme", "checkout")).toBeNull();
  });
});

describe("ConnectionsPage", () => {
  it("lists configured connections and distinguishes their statuses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          connections: [
            makeGitHubConnection({
              connection_id: "ghconn_ok",
              owner: "acme",
              repo: "checkout",
              status: "connected",
            }),
            makeGitHubConnection({
              connection_id: "ghconn_bad",
              owner: "acme",
              repo: "legacy",
              status: "error",
            }),
            makeGitHubConnection({
              connection_id: "ghconn_off",
              owner: "acme",
              repo: "archived",
              status: "disabled",
            }),
          ],
        }),
      ),
    );

    renderPage();

    expect(await screen.findByText("acme/checkout")).toBeInTheDocument();
    expect(screen.getByText("acme/legacy")).toBeInTheDocument();
    expect(screen.getByText("acme/archived")).toBeInTheDocument();
    expect(screen.getByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("Error")).toBeInTheDocument();
    expect(screen.getByText("Disabled")).toBeInTheDocument();
  });

  it("adds a connection through the API and shows it in the list", async () => {
    const created = makeGitHubConnection({
      connection_id: "ghconn_new",
      owner: "octocat",
      repo: "hello-world",
    });
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(jsonResponse(created));
      }
      return Promise.resolve(jsonResponse({ connections: [] }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await screen.findByText("No GitHub connections");

    await user.type(screen.getByLabelText(/owner/i), "octocat");
    await user.type(screen.getByLabelText(/repository/i), "hello-world");
    await user.click(
      screen.getByRole("button", { name: /connect repository/i }),
    );

    expect(await screen.findByText("octocat/hello-world")).toBeInTheDocument();

    const postCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "POST",
    );
    expect(postCall?.[0]).toBe("/integrations/github/connections");
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      owner: "octocat",
      repo: "hello-world",
    });
  });

  it("surfaces an integration-unavailable error when adding", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        if (init?.method === "POST") {
          return Promise.resolve(
            jsonResponse(
              {
                error: "github_integration_unavailable",
                detail: "GitHub integration is not configured.",
              },
              503,
            ),
          );
        }
        return Promise.resolve(jsonResponse({ connections: [] }));
      }),
    );
    const user = userEvent.setup();

    renderPage();
    await screen.findByText("No GitHub connections");

    await user.type(screen.getByLabelText(/owner/i), "octocat");
    await user.type(screen.getByLabelText(/repository/i), "hello-world");
    await user.click(
      screen.getByRole("button", { name: /connect repository/i }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /credential before repositories can be connected/i,
    );
  });

  it("verifies a connection and updates its status from the response", async () => {
    const initial = makeGitHubConnection({
      connection_id: "ghconn_01",
      owner: "acme",
      repo: "checkout",
      status: "error",
    });
    const verified = makeGitHubConnection({
      connection_id: "ghconn_01",
      owner: "acme",
      repo: "checkout",
      status: "connected",
    });
    const fetchMock = vi.fn((url: string, init?: RequestInit) => {
      if (String(url).endsWith("/verify")) {
        return Promise.resolve(
          jsonResponse({
            connection: verified,
            verified: true,
            message: "Connection verified.",
            verified_at: "2026-08-28T14:10:00Z",
          }),
        );
      }
      void init;
      return Promise.resolve(jsonResponse({ connections: [initial] }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await screen.findByText("acme/checkout");
    expect(screen.getByText("Error")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Verify" }));

    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Connection verified.");
    const verifyCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith("/verify"),
    );
    expect(verifyCall?.[0]).toBe(
      "/integrations/github/connections/ghconn_01/verify",
    );
    expect(verifyCall?.[1]?.method).toBe("POST");
  });

  it("deletes a connection only after confirmation", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return Promise.resolve(
          jsonResponse({ connection_id: "ghconn_01", deleted: true }),
        );
      }
      return Promise.resolve(
        jsonResponse({
          connections: [
            makeGitHubConnection({ connection_id: "ghconn_01", owner: "acme", repo: "checkout" }),
          ],
        }),
      );
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await screen.findByText("acme/checkout");

    await user.click(screen.getByRole("button", { name: "Disconnect" }));
    // No delete request until the operator confirms.
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE"),
    ).toBe(false);

    await user.click(
      screen.getByRole("button", { name: /confirm disconnect/i }),
    );

    await waitFor(() => {
      expect(screen.queryByText("acme/checkout")).not.toBeInTheDocument();
    });
    const deleteCall = fetchMock.mock.calls.find(
      ([, init]) => init?.method === "DELETE",
    );
    expect(deleteCall?.[0]).toBe(
      "/integrations/github/connections/ghconn_01",
    );
  });

  it("shows a loading state while connections are being fetched", async () => {
    const pending = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(pending.promise));

    renderPage();

    expect(screen.getByRole("status")).toHaveTextContent(
      /loading github connections/i,
    );

    pending.resolve(jsonResponse({ connections: [] }));
    expect(await screen.findByText("No GitHub connections")).toBeInTheDocument();
  });

  it("shows an empty state when there are no connections", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ connections: [] })),
    );

    renderPage();

    expect(await screen.findByText("No GitHub connections")).toBeInTheDocument();
  });

  it("shows an error state with a retry action when loading fails", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          {
            error: "github_integration_unavailable",
            detail: "GitHub integration is not configured.",
          },
          503,
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({
          connections: [makeGitHubConnection({ owner: "acme", repo: "checkout" })],
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();

    expect(
      await screen.findByText("Could not load GitHub connections"),
    ).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /credential before repositories can be connected/i,
    );

    await user.click(screen.getByRole("button", { name: /try again/i }));

    expect(await screen.findByText("acme/checkout")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("prevents duplicate submissions while a connection is being added", async () => {
    const pending = deferred<Response>();
    const fetchMock = vi.fn((_url: string, init?: RequestInit) => {
      if (init?.method === "POST") return pending.promise;
      return Promise.resolve(jsonResponse({ connections: [] }));
    });
    vi.stubGlobal("fetch", fetchMock);
    const user = userEvent.setup();

    renderPage();
    await screen.findByText("No GitHub connections");

    await user.type(screen.getByLabelText(/owner/i), "octocat");
    await user.type(screen.getByLabelText(/repository/i), "hello-world");
    const submit = screen.getByRole("button", { name: /connect repository/i });
    await user.click(submit);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /connecting/i })).toBeDisabled();
    });
    // A second click while in-flight must not issue another POST.
    await user.click(screen.getByRole("button", { name: /connecting/i }));
    expect(
      fetchMock.mock.calls.filter(([, init]) => init?.method === "POST"),
    ).toHaveLength(1);

    pending.resolve(
      jsonResponse(makeGitHubConnection({ owner: "octocat", repo: "hello-world" })),
    );
    await waitFor(() => {
      expect(screen.getByText("octocat/hello-world")).toBeInTheDocument();
    });
  });

  it("never renders a credential reference", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({
          connections: [makeGitHubConnection({ owner: "acme", repo: "checkout" })],
        }),
      ),
    );

    const { container } = renderPage();
    await screen.findByText("acme/checkout");

    expect(container.textContent ?? "").not.toMatch(/credential_ref/i);
  });
});
