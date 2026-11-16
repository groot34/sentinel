import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ApprovalSnapshotResponse } from "../api/types";
import { makeEvidence, makeHypothesis, makeProposal, makeVerification } from "../test/fixtures";
import { ApprovalSnapshotView } from "./ApprovalSnapshotView";

/** A captured snapshot as the API returns it (mirrors `ApprovalSnapshotResponse`). */
function makeSnapshot(
  overrides: Partial<ApprovalSnapshotResponse> = {},
): ApprovalSnapshotResponse {
  return {
    investigation_id: "inc_01",
    proposal_id: "FIX-001",
    snapshot_available: true,
    decision: "APPROVED",
    reviewer: "dana",
    decided_at: "2026-08-28T15:00:00Z",
    notes: "looks good",
    snapshot_version: 1,
    integrity: {
      algorithm: "sha256",
      hash: "a".repeat(64),
      verified: true,
    },
    snapshot: {
      snapshot_version: 1,
      investigation_id: "inc_01",
      proposal_id: "FIX-001",
      decision: {
        proposal_id: "FIX-001",
        decision: "APPROVED",
        reviewer: "dana",
        timestamp: "2026-08-28T15:00:00Z",
        notes: "looks good",
      },
      proposal: makeProposal({
        patch:
          "--- a/service/app.py\n+++ b/service/app.py\n@@ -22,1 +22,1 @@\n-        ttl = 5\n+        ttl = 3600\n",
      }),
      hypothesis: {
        ...makeHypothesis(),
        verdict: "CONFIRMED",
        confidence: 0.95,
      },
      evidence: [makeEvidence()],
      verification: [makeVerification()],
    },
    ...overrides,
  };
}

describe("ApprovalSnapshotView — captured snapshot", () => {
  it("renders the decision, reviewer and verified integrity status", () => {
    render(<ApprovalSnapshotView snapshot={makeSnapshot()} />);

    expect(screen.getByText("APPROVED")).toBeInTheDocument();
    expect(screen.getByText(/recorded by dana/)).toBeInTheDocument();
    expect(screen.getByText(/integrity verified/)).toBeInTheDocument();
    expect(screen.getByText(/sha256/)).toBeInTheDocument();
  });

  it("renders the proposal diff exactly as captured at decision time", () => {
    render(<ApprovalSnapshotView snapshot={makeSnapshot()} />);

    const patch = screen.getByLabelText("Decision-time patch for FIX-001");
    expect(patch.querySelectorAll(".diff__line--add")).toHaveLength(1);
    expect(patch.querySelectorAll(".diff__line--remove")).toHaveLength(1);
    expect(screen.getByText("Restore the 1-hour TTL.")).toBeInTheDocument();
  });

  it("renders the linked hypothesis with its original verdict and evidence", () => {
    render(<ApprovalSnapshotView snapshot={makeSnapshot()} />);

    // Rendered on the captured hypothesis and on its decision-time verification.
    expect(screen.getAllByText("CONFIRMED").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/confidence 0.95/).length).toBeGreaterThan(0);
    expect(screen.getByText("A misconfigured cache TTL caused a stampede.")).toBeInTheDocument();
    // Appears both as the referenced-evidence chip and in the captured evidence list.
    expect(screen.getAllByText("EV-LOG-001").length).toBeGreaterThan(0);
  });

  it("renders the verification results available at decision time", () => {
    render(<ApprovalSnapshotView snapshot={makeSnapshot()} />);

    expect(screen.getByText("CHK-001")).toBeInTheDocument();
    expect(
      screen.getByText(/Referenced evidence excerpts verified against source files/),
    ).toBeInTheDocument();
  });

  it("frames the captured section as immutable", () => {
    render(<ApprovalSnapshotView snapshot={makeSnapshot()} />);

    expect(
      screen.getByText(/immutable record and does not change/i),
    ).toBeInTheDocument();
  });
});

describe("ApprovalSnapshotView — historical vs current", () => {
  it("shows newer evidence separately and labels it as not part of the decision", () => {
    render(
      <ApprovalSnapshotView
        snapshot={makeSnapshot()}
        currentEvidence={[
          makeEvidence(),
          makeEvidence({
            evidence_id: "EV-LOG-002",
            description: "Newer pool exhaustion signal",
          }),
        ]}
      />,
    );

    expect(
      screen.getByText("Current investigation state (not part of this decision)"),
    ).toBeInTheDocument();
    expect(screen.getByText(/Evidence recorded since this decision \(1\)/)).toBeInTheDocument();
    expect(screen.getByText("EV-LOG-002")).toBeInTheDocument();
    expect(
      screen.getByText(/None of it was available when this decision was recorded/),
    ).toBeInTheDocument();
  });

  it("says so when no further evidence has arrived", () => {
    render(
      <ApprovalSnapshotView
        snapshot={makeSnapshot()}
        currentEvidence={[makeEvidence()]}
      />,
    );

    expect(
      screen.getByText("No additional evidence has been recorded since this decision."),
    ).toBeInTheDocument();
  });

  it("reports a verdict that changed after the decision only in the current block", () => {
    render(
      <ApprovalSnapshotView
        snapshot={makeSnapshot()}
        currentEvidence={[makeEvidence()]}
        currentHypothesis={makeHypothesis({ verdict: "REJECTED" })}
      />,
    );

    expect(screen.getByText(/The linked hypothesis verdict has since changed to/)).toBeInTheDocument();
    expect(screen.getByText("REJECTED")).toBeInTheDocument();
  });

  it("omits the current-state block entirely when no current result is supplied", () => {
    render(<ApprovalSnapshotView snapshot={makeSnapshot()} />);

    expect(
      screen.queryByText("Current investigation state (not part of this decision)"),
    ).toBeNull();
  });
});

describe("ApprovalSnapshotView — legacy approvals", () => {
  it("shows an honest message and never fabricates decision-time context", () => {
    render(
      <ApprovalSnapshotView
        snapshot={makeSnapshot({
          snapshot_available: false,
          decision: "REJECTED",
          reviewer: "dana",
          snapshot_version: null,
          integrity: null,
          snapshot: {},
          message:
            "No historical evidence snapshot was captured for this approval decision.",
        })}
      />,
    );

    expect(
      screen.getByText("No historical evidence snapshot was captured for this approval decision."),
    ).toBeInTheDocument();
    expect(screen.getByText(/was never stored, so it cannot be reconstructed/)).toBeInTheDocument();
    expect(screen.getByText("REJECTED")).toBeInTheDocument();
    // No decision-time evidence, proposal or verification is invented.
    expect(screen.queryByText("EV-LOG-001")).toBeNull();
    expect(screen.queryByLabelText("Decision-time patch for FIX-001")).toBeNull();
    expect(screen.queryByText("CHK-001")).toBeNull();
  });
});

describe("ApprovalSnapshotView — evidence focus", () => {
  it("focuses matching evidence from the historical snapshot", async () => {
    const user = userEvent.setup();
    const onFocusEvidence = vi.fn();
    render(
      <ApprovalSnapshotView
        snapshot={makeSnapshot()}
        onFocusEvidence={onFocusEvidence}
      />,
    );

    await user.click(screen.getByRole("button", { name: /EV-LOG-001/ }));

    expect(onFocusEvidence).toHaveBeenCalledWith("EV-LOG-001");
  });
});
