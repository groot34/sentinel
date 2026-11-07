/**
 * Renders the real captured backend response through the actual components.
 *
 * `real_result_inc_02.json` is the verbatim `GET /investigations/{id}/result`
 * payload for the real, completed `inc_02_cache_stampede` investigation. This
 * test proves the workspace can display a genuine persisted diagnosis (evidence
 * groups, hypotheses and verification outcomes) without inventing any data.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { InvestigationResult } from "../api/types";
import realResultJson from "../test/real_result_inc_02.json";
import { ResultPanel } from "./ResultPanel";

const result = realResultJson as unknown as InvestigationResult;

describe("ResultPanel with a real captured backend response", () => {
  it("renders the real persisted diagnosis and evidence groups", () => {
    render(<ResultPanel result={result} />);

    expect(
      screen.getByRole("heading", { name: "Diagnosis" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Hypotheses and verification" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Evidence" })).toBeInTheDocument();

    // Real evidence counts drive the source filters.
    expect(
      screen.getByRole("button", { name: "All sources (22)" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Metrics (13)" }),
    ).toBeInTheDocument();

    // Every source the backend actually returned becomes its own group.
    for (const label of [
      "Logs",
      "Metrics",
      "Git diff",
      "Source code",
      "Configuration",
    ]) {
      expect(screen.getByRole("heading", { name: label })).toBeInTheDocument();
    }

    // Real hypothesis ids and the real verdict value.
    expect(screen.getAllByText("HYP-001").length).toBeGreaterThan(0);
    expect(screen.getAllByText("CONFIRMED").length).toBeGreaterThan(0);

    // A real metric evidence item renders its metric name and value.
    expect(screen.getAllByText("p95_latency_ms").length).toBeGreaterThan(0);
  });
});
