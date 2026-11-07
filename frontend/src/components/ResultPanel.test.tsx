import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ResultPanel } from "./ResultPanel";
import { makeEvidence, makeHypothesis, makeResult, makeVerification } from "../test/fixtures";

describe("ResultPanel", () => {
  it("summarizes hypotheses, evidence and proposals from the persisted result", () => {
    render(<ResultPanel result={makeResult()} />);

    expect(screen.getByText("Hypotheses")).toBeInTheDocument();
    expect(screen.getByText("Confirmed")).toBeInTheDocument();
    expect(screen.getByText("Fix proposals")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Hypotheses and verification" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Evidence" })).toBeInTheDocument();
  });

  it("focuses the matching evidence card when a hypothesis evidence chip is activated", async () => {
    const user = userEvent.setup();
    const result = makeResult({
      hypotheses: [makeHypothesis({ hypothesis_id: "HYP-001", evidence_ids: ["EV-MET-001"] })],
      evidence: [
        makeEvidence({ evidence_id: "EV-MET-001", source: "metrics", type: "spike" }),
        makeEvidence({ evidence_id: "EV-LOG-001", source: "logs" }),
      ],
      verification: [
        makeVerification({ hypothesis_id: "HYP-001", checks: [] }),
      ],
      proposals: [],
    });

    render(<ResultPanel result={result} />);

    expect(document.getElementById("evidence-EV-MET-001")).not.toHaveClass(
      "evidence__item--focus",
    );

    await user.click(screen.getByRole("button", { name: /EV-MET-001/ }));

    expect(document.getElementById("evidence-EV-MET-001")).toHaveClass(
      "evidence__item--focus",
    );
  });

  it("keeps the mandatory human approval notice on fix proposals", () => {
    render(<ResultPanel result={makeResult()} />);

    expect(
      screen.getByText("AWAITING HUMAN APPROVAL — this fix has not been applied."),
    ).toBeInTheDocument();
    expect(screen.getByText("Fix proposals (awaiting human approval)")).toBeInTheDocument();
  });

  it("shows a neutral message when nothing has been persisted yet", () => {
    render(
      <ResultPanel
        result={makeResult({
          result_available: false,
          hypotheses: [],
          evidence: [],
          verification: [],
          proposals: [],
        })}
      />,
    );

    expect(
      screen.getByText(/No diagnosis has been persisted yet/),
    ).toBeInTheDocument();
  });
});
