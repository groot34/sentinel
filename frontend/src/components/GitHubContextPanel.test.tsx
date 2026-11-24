import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { GitHubContext } from "../api/types";
import { GitHubContextPanel } from "./GitHubContextPanel";
import { makeGitHubContext } from "../test/fixtures";

describe("GitHubContextPanel", () => {
  it("shows repository identity and retrieval facts for a successful run", () => {
    render(
      <GitHubContextPanel
        context={makeGitHubContext({ status: "success" })}
        association={{ connection_id: "ghconn_01", ref: "main" }}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "GitHub repository context" }),
    ).toBeInTheDocument();
    // The status badge ("Retrieved") is distinct from the "Retrieved" fact label.
    expect(screen.getByTitle("success")).toHaveTextContent("Retrieved");
    expect(screen.getByText("acme/checkout")).toBeInTheDocument();
    // Requested ref, resolved commit (shortened) and counts are all surfaced.
    expect(screen.getByText("main")).toBeInTheDocument();
    expect(screen.getByText("abcdef1")).toBeInTheDocument();
    expect(screen.getByText("Files retrieved")).toBeInTheDocument();
    expect(screen.getByText("8")).toBeInTheDocument();
    expect(screen.getByText("Evidence items")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("renders a partial retrieval with truncation and skipped files", () => {
    render(
      <GitHubContextPanel
        context={makeGitHubContext({
          status: "partial",
          truncated: true,
          truncation_reason: "file limit reached",
          skipped: [
            { path: "vendor/big.js", reason: "too large", detail: "5 MB" },
          ],
        })}
      />,
    );

    expect(screen.getByText("Partial")).toBeInTheDocument();
    expect(screen.getByText(/retrieval was truncated/i)).toBeInTheDocument();
    expect(screen.getByText(/file limit reached/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /skipped files \(1\)/i })).toBeInTheDocument();
    expect(screen.getByText("vendor/big.js")).toBeInTheDocument();
    expect(screen.getByText(/too large/)).toBeInTheDocument();
  });

  it("renders a failed retrieval with a safe error message", () => {
    render(
      <GitHubContextPanel
        context={makeGitHubContext({
          status: "failure",
          error: {
            reason: "github_not_found",
            message: "The repository or reference could not be found.",
          },
          retrieved_file_count: 0,
          evidence_count: 0,
        })}
      />,
    );

    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(
      /the repository or reference could not be found/i,
    );
  });

  it("falls back to the association when no retrieval context was captured", () => {
    render(
      <GitHubContextPanel
        context={null}
        association={{ connection_id: "ghconn_01", ref: "release/2026.08" }}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "GitHub repository context" }),
    ).toBeInTheDocument();
    expect(screen.getByText("release/2026.08")).toBeInTheDocument();
    expect(
      screen.getByText(/no retrieval context was captured/i),
    ).toBeInTheDocument();
  });

  it("renders nothing for a bundle-only investigation (no context, no association)", () => {
    const { container } = render(
      <GitHubContextPanel context={null} association={null} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for a legacy investigation without GitHub metadata", () => {
    const { container } = render(<GitHubContextPanel />);

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByText(/github/i)).not.toBeInTheDocument();
  });

  it("renders without crashing when the payload omits `skipped`", () => {
    const { skipped: discarded, ...contextWithoutSkipped } = makeGitHubContext({
      status: "partial",
    });
    void discarded;

    render(
      <GitHubContextPanel
        context={contextWithoutSkipped as GitHubContext}
        association={{ connection_id: "ghconn_01", ref: "main" }}
      />,
    );

    expect(
      screen.getByRole("heading", { name: "GitHub repository context" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Partial")).toBeInTheDocument();
    // No skipped-file section is rendered when the list is absent.
    expect(
      screen.queryByRole("heading", { name: /skipped files/i }),
    ).not.toBeInTheDocument();
  });
});
