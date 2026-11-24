import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { GitHubProvenanceView } from "./GitHubProvenance";
import { makeGitHubProvenance } from "../test/fixtures";
import { shortCommitSha } from "../lib/github";

describe("GitHubProvenanceView", () => {
  it("shows repository, path, shortened commit, reference and retrieval time", () => {
    const provenance = makeGitHubProvenance();
    render(<GitHubProvenanceView provenance={provenance} />);

    expect(screen.getByText("GitHub")).toBeInTheDocument();
    expect(screen.getByText("acme/checkout")).toBeInTheDocument();
    expect(screen.getByText("service/cache.py")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
    // The SHA is shortened visually but keeps the full value on hover.
    expect(screen.getByText("abcdef1")).toBeInTheDocument();
    expect(screen.getByText("abcdef1")).toHaveAttribute(
      "title",
      provenance.commit_sha,
    );
    expect(screen.getByText("Retrieved")).toBeInTheDocument();
  });

  it("links to the backend-supplied commit-pinned source URL in a new tab", () => {
    const provenance = makeGitHubProvenance();
    render(<GitHubProvenanceView provenance={provenance} />);

    const link = screen.getByRole("link", { name: /view at commit abcdef1/i });
    expect(link).toHaveAttribute("href", provenance.source_url);
    expect(link.getAttribute("href")).toContain(provenance.commit_sha);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    // The line anchor is surfaced on the link text.
    expect(link.textContent).toContain("#L12");
  });

  it("omits absent fields instead of rendering empty placeholders", () => {
    render(
      <GitHubProvenanceView
        provenance={makeGitHubProvenance({
          ref: null,
          path: null,
          line_anchor: null,
          retrieved_at: null,
          source_url: null,
        })}
      />,
    );

    expect(screen.getByText("acme/checkout")).toBeInTheDocument();
    expect(screen.queryByText("Path")).not.toBeInTheDocument();
    expect(screen.queryByText("Reference")).not.toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

describe("shortCommitSha", () => {
  it("shortens long SHAs and keeps short or absent values neutral", () => {
    expect(shortCommitSha("abcdef1234567890")).toBe("abcdef1");
    expect(shortCommitSha("abc1234")).toBe("abc1234");
    expect(shortCommitSha(null)).toBeNull();
    expect(shortCommitSha("")).toBeNull();
  });
});
