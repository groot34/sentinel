import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";

import { EvidenceExplorer } from "./EvidenceExplorer";
import { buildEvidenceIndex } from "../lib/diagnosis";
import { makeEvidence, makeResult } from "../test/fixtures";

const evidence = [
  makeEvidence({
    evidence_id: "EV-LOG-001",
    source: "logs",
    type: "warning",
    excerpt: "WARN Simultaneous cache miss on key 'category:electronics:page1'",
  }),
  makeEvidence({
    evidence_id: "EV-MET-001",
    source: "metrics",
    type: "spike",
    metric: "p95_latency_ms",
    value: 5000,
    reference: "metrics/metrics.csv:row 8",
    excerpt: null,
    description: "p95 latency spiked",
  }),
  makeEvidence({
    evidence_id: "EV-CODE-001",
    source: "git_diff",
    type: "added_code",
    description: "Shortened cache TTL",
    reference: "service/cache.py:12",
    excerpt: "+    DEFAULT_TTL_SECONDS = 5",
  }),
];

function renderWithProps(
  props: Partial<ComponentProps<typeof EvidenceExplorer>> = {},
) {
  const index = buildEvidenceIndex(
    makeResult({ evidence, hypotheses: [], proposals: [] }),
  );
  const onClearFocus = vi.fn();
  const utils = render(
    <EvidenceExplorer
      evidence={evidence}
      citedByHypotheses={index.citedByHypotheses}
      citedByProposals={index.citedByProposals}
      focusedEvidenceId={null}
      onClearFocus={onClearFocus}
      {...props}
    />,
  );
  return { ...utils, onClearFocus };
}

describe("EvidenceExplorer", () => {
  it("groups evidence by source with per-source counts", () => {
    renderWithProps();

    expect(screen.getByRole("heading", { name: "Logs" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Metrics" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Git diff" })).toBeInTheDocument();
    expect(screen.getByText("EV-LOG-001")).toBeInTheDocument();
    expect(screen.getByText("EV-MET-001")).toBeInTheDocument();
    expect(screen.getByText("EV-CODE-001")).toBeInTheDocument();
  });

  it("renders real evidence metadata (metric, value, reference, excerpt)", () => {
    renderWithProps();

    expect(screen.getByText("Metric")).toBeInTheDocument();
    expect(screen.getByText("p95_latency_ms")).toBeInTheDocument();
    expect(screen.getByText("5000")).toBeInTheDocument();
    expect(screen.getByText("logs/application.log:6")).toBeInTheDocument();
    expect(screen.getByText(/DEFAULT_TTL_SECONDS = 5/)).toBeInTheDocument();
  });

  it("filters the list to a single source and back again", async () => {
    const user = userEvent.setup();
    renderWithProps();

    await user.click(screen.getByRole("button", { name: "Metrics (1)" }));

    expect(screen.getByText("EV-MET-001")).toBeInTheDocument();
    expect(screen.queryByText("EV-LOG-001")).not.toBeInTheDocument();
    expect(screen.queryByText("EV-CODE-001")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Metrics (1)" }));

    expect(screen.getByText("EV-LOG-001")).toBeInTheDocument();
    expect(screen.getByText("EV-CODE-001")).toBeInTheDocument();
  });

  it("narrows by free-text search and offers to clear filters", async () => {
    const user = userEvent.setup();
    renderWithProps();

    await user.type(screen.getByRole("searchbox"), "EV-CODE");

    expect(screen.getByText("EV-CODE-001")).toBeInTheDocument();
    expect(screen.queryByText("EV-LOG-001")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Clear filters" }));

    expect(screen.getByText("EV-LOG-001")).toBeInTheDocument();
  });

  it("shows an empty message when no evidence matches the filters", async () => {
    const user = userEvent.setup();
    renderWithProps();

    await user.type(screen.getByRole("searchbox"), "nothing-matches-this");

    expect(
      screen.getByText("No evidence matches the current filters."),
    ).toBeInTheDocument();
  });

  it("links each evidence item to the hypotheses and proposals that cite it", () => {
    const index = buildEvidenceIndex(
      makeResult({
        evidence,
        hypotheses: [
          {
            hypothesis_id: "HYP-001",
            claim: "c",
            evidence_ids: ["EV-MET-001"],
            verdict: null,
            supporting_reasoning: null,
            falsification_criteria: [],
            verification_plan: [],
          },
        ],
      }),
    );

    render(
      <EvidenceExplorer
        evidence={evidence}
        citedByHypotheses={index.citedByHypotheses}
        citedByProposals={index.citedByProposals}
        focusedEvidenceId={null}
        onClearFocus={vi.fn()}
      />,
    );

    expect(screen.getByRole("link", { name: "HYP-001" })).toHaveAttribute(
      "href",
      "#hypothesis-HYP-001",
    );
    expect(screen.getByRole("link", { name: "FIX-001" })).toHaveAttribute(
      "href",
      "#proposal-FIX-001",
    );
  });

  it("highlights a focused item and lets the user clear the focus", async () => {
    const user = userEvent.setup();
    const { onClearFocus } = renderWithProps({ focusedEvidenceId: "EV-MET-001" });

    const item = document.getElementById("evidence-EV-MET-001");
    expect(item).toHaveClass("evidence__item--focus");
    expect(screen.getByRole("status")).toHaveTextContent("EV-MET-001");

    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(onClearFocus).toHaveBeenCalledTimes(1);
  });

  it("handles an investigation with no persisted evidence", () => {
    render(
      <EvidenceExplorer
        evidence={[]}
        citedByHypotheses={new Map()}
        citedByProposals={new Map()}
        focusedEvidenceId={null}
        onClearFocus={vi.fn()}
      />,
    );

    expect(
      screen.getByText(/No evidence was persisted for this investigation/),
    ).toBeInTheDocument();
  });
});
