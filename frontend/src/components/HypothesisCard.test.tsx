import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { HypothesisCard } from "./HypothesisCard";
import type { HypothesisOutcome } from "../lib/diagnosis";
import { makeEvidence, makeHypothesis, makeVerification } from "../test/fixtures";

function makeOutcome(overrides: Partial<HypothesisOutcome> = {}): HypothesisOutcome {
  return {
    hypothesis: makeHypothesis(),
    verification: makeVerification(),
    evidence: [makeEvidence()],
    missingEvidenceIds: [],
    ...overrides,
  };
}

describe("HypothesisCard", () => {
  it("shows the backend verdict and the verification reasoning", () => {
    render(<HypothesisCard outcome={makeOutcome()} onFocusEvidence={vi.fn()} />);

    expect(screen.getByText("CONFIRMED")).toBeInTheDocument();
    expect(screen.getByText("A misconfigured cache TTL caused a stampede.")).toBeInTheDocument();
    expect(screen.getByText("All required checks passed.")).toBeInTheDocument();
    expect(screen.getByText(/confidence 95%/)).toBeInTheDocument();
  });

  it("marks a hypothesis as unverified when no verification was persisted", () => {
    render(
      <HypothesisCard
        outcome={makeOutcome({ verification: null })}
        onFocusEvidence={vi.fn()}
      />,
    );

    expect(screen.getByText("UNVERIFIED")).toBeInTheDocument();
    expect(
      screen.getByText("No verification result was persisted for this hypothesis."),
    ).toBeInTheDocument();
  });

  it("lists verification checks with their result, detail and reference", () => {
    render(<HypothesisCard outcome={makeOutcome()} onFocusEvidence={vi.fn()} />);

    expect(screen.getByText("PASS")).toBeInTheDocument();
    expect(
      screen.getByText("Referenced evidence excerpts verified against source files."),
    ).toBeInTheDocument();
    expect(screen.getByText("5 of 6 referenced evidence items grounded.")).toBeInTheDocument();
    expect(screen.getByText("service/app.py:22")).toBeInTheDocument();
  });

  it("focuses the evidence explorer when a cited evidence chip is activated", async () => {
    const user = userEvent.setup();
    const onFocusEvidence = vi.fn();
    render(<HypothesisCard outcome={makeOutcome()} onFocusEvidence={onFocusEvidence} />);

    // The same evidence is cited by the supporting-evidence chip and by the
    // verification check, so the first (supporting evidence) chip is targeted.
    await user.click(screen.getAllByRole("button", { name: /EV-LOG-001/ })[0]);

    expect(onFocusEvidence).toHaveBeenCalledWith("EV-LOG-001");
  });

  it("exposes a rejected hypothesis using the real verdict value", () => {
    render(
      <HypothesisCard
        outcome={makeOutcome({
          hypothesis: makeHypothesis({ verdict: "REJECTED" }),
          verification: makeVerification({ verdict: "REJECTED" }),
        })}
        onFocusEvidence={vi.fn()}
      />,
    );

    // The verdict is shown both on the card header and in the verification
    // outcome line, both sourced from the persisted result.
    expect(screen.getAllByText("REJECTED")).toHaveLength(2);
  });

  it("reports evidence cited by the hypothesis but missing from the projection", () => {
    render(
      <HypothesisCard
        outcome={makeOutcome({ evidence: [], missingEvidenceIds: ["EV-GONE-999"] })}
        onFocusEvidence={vi.fn()}
      />,
    );

    expect(screen.getByText(/EV-GONE-999/)).toBeInTheDocument();
    expect(screen.getByText(/not present in the persisted evidence projection/)).toBeInTheDocument();
  });

  it("keeps falsification criteria and the verification plan available", () => {
    render(<HypothesisCard outcome={makeOutcome()} onFocusEvidence={vi.fn()} />);

    expect(screen.getByText("Falsification criteria (1)")).toBeInTheDocument();
    expect(screen.getByText("Verification plan (1)")).toBeInTheDocument();
  });

  it("labels a non-discriminating check as evidence grounding", () => {
    render(
      <HypothesisCard
        outcome={makeOutcome({
          verification: makeVerification({
            checks: [
              {
                check_id: "CHK-001",
                description: "AST scan detected database calls inside a for/while loop.",
                result: "PASS",
                reference: "service/app.py:41",
                detail: null,
                evidence_ids: ["EV-CODE-001"],
                check_type: "code_invariant",
                discriminating: true,
              },
              {
                check_id: "CHK-002",
                description: "Referenced evidence excerpts verified against source files.",
                result: "PASS",
                reference: "logs/application.log:9",
                detail: null,
                evidence_ids: ["EV-LOG-001"],
                check_type: "code_invariant",
                discriminating: false,
              },
            ],
          }),
        })}
        onFocusEvidence={vi.fn()}
      />,
    );

    // Only the non-discriminating (grounding) check is labelled; the
    // mechanism-specific check is left unlabelled.
    expect(screen.getAllByText("evidence grounding")).toHaveLength(1);
    expect(screen.getAllByText("PASS")).toHaveLength(2);
  });
});
