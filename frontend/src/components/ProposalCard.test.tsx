import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { ApprovalRecord, SubmitApprovalResponse } from "../api/types";
import { makeProposal } from "../test/fixtures";
import { proposalView } from "../lib/proposals";
import { ProposalCard, DEFAULT_REVIEWER, type ProposalDecisionInput } from "./ProposalCard";

function approvalResponse(
  overrides: Partial<SubmitApprovalResponse> = {},
): SubmitApprovalResponse {
  return {
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
    ...overrides,
  };
}

function renderCard({
  approvals = [] as ApprovalRecord[],
  proposal = makeProposal(),
  onDecide,
  onFocusEvidence,
}: {
  approvals?: ApprovalRecord[];
  proposal?: ReturnType<typeof makeProposal>;
  onDecide?: (input: ProposalDecisionInput) => Promise<SubmitApprovalResponse>;
  onFocusEvidence?: (id: string) => void;
} = {}) {
  return render(
    <ul>
      <ProposalCard
        view={proposalView(proposal, approvals)}
        onDecide={onDecide}
        onFocusEvidence={onFocusEvidence}
      />
    </ul>,
  );
}

describe("ProposalCard review content", () => {
  it("renders the rationale, hypothesis link, evidence and proposed file changes", () => {
    renderCard({
      proposal: makeProposal({
        patch: null,
        changes: [
          { file: "service/app.py", description: "Restore TTL", start_line: 22, end_line: 22 },
          { file: "service/cache.py", description: "Warm cache", start_line: 10, end_line: 12 },
        ],
      }),
    });

    expect(screen.getByText("Restore the 1-hour TTL.")).toBeInTheDocument();
    expect(screen.getByText("The shortened TTL caused the stampede.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "HYP-001" })).toHaveAttribute(
      "href",
      "#hypothesis-HYP-001",
    );
    expect(screen.getByText(":22")).toBeInTheDocument();
    expect(screen.getByText(":10-12")).toBeInTheDocument();
  });

  it("renders the stored patch as a diff when one exists", () => {
    renderCard({
      proposal: makeProposal({
        patch: "--- a/service/app.py\n+++ b/service/app.py\n@@ -22,1 +22,1 @@\n-        ttl = 5\n+        ttl = 3600\n",
      }),
    });

    const patch = screen.getByLabelText("Proposed patch for FIX-001");
    expect(patch.querySelectorAll(".diff__line--add")).toHaveLength(1);
    expect(patch.querySelectorAll(".diff__line--remove")).toHaveLength(1);
    expect(patch.querySelectorAll(".diff__line--hunk")).toHaveLength(1);
    expect(patch.querySelector(".diff__line--add")?.textContent).toBe(
      "+        ttl = 3600\n",
    );
  });

  it("says so instead of inventing a diff when only file references exist", () => {
    renderCard({ proposal: makeProposal({ patch: null }) });

    expect(screen.queryByLabelText("Proposed patch for FIX-001")).toBeNull();
    expect(
      screen.getByText(/Only file references are stored for this proposal/),
    ).toBeInTheDocument();
  });

  it("keeps the backend's mandatory human approval notice", () => {
    renderCard();

    expect(
      screen.getByText("AWAITING HUMAN APPROVAL — this fix has not been applied."),
    ).toBeInTheDocument();
  });

  it("focuses matching evidence when a supporting-evidence chip is activated", async () => {
    const user = userEvent.setup();
    const onFocusEvidence = vi.fn();
    renderCard({
      proposal: makeProposal({ evidence_ids: ["EV-LOG-001"] }),
      onFocusEvidence,
    });

    await user.click(screen.getByRole("button", { name: /EV-LOG-001/ }));

    expect(onFocusEvidence).toHaveBeenCalledWith("EV-LOG-001");
  });
});

describe("ProposalCard approval workflow", () => {
  it("is read-only with no Approve/Reject actions when no handler is supplied", () => {
    renderCard();

    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.getByText("No decision has been recorded yet.")).toBeInTheDocument();
  });

  it("requires confirmation before recording an approval", async () => {
    const user = userEvent.setup();
    const onDecide = vi.fn().mockResolvedValue(approvalResponse());
    renderCard({ onDecide });

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(onDecide).not.toHaveBeenCalled();
    expect(
      screen.getByText(/no code is modified, committed, pushed or deployed/i),
    ).toBeInTheDocument();

    const reviewer = screen.getByLabelText("Reviewer");
    expect(reviewer).toHaveValue(DEFAULT_REVIEWER);

    await user.clear(reviewer);
    await user.type(reviewer, "dana");
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));

    expect(onDecide).toHaveBeenCalledTimes(1);
    expect(onDecide).toHaveBeenCalledWith({
      proposalId: "FIX-001",
      decision: "approved",
      reviewer: "dana",
      notes: undefined,
    });
  });

  it("shows the persisted result returned by the backend", async () => {
    const user = userEvent.setup();
    const onDecide = vi.fn().mockResolvedValue(approvalResponse());
    renderCard({ onDecide });

    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(
        "Decision recorded: APPROVED by dana.",
      ),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "HUMAN REVIEW RECORDED — fix patch has not been applied.",
    );
  });

  it("submits a rejection with the reviewer's notes", async () => {
    const user = userEvent.setup();
    const onDecide = vi.fn().mockResolvedValue(
      approvalResponse({
        status: "REJECTED",
        approval: {
          proposal_id: "FIX-001",
          status: "REJECTED",
          decision: "rejected",
          approved_by: "dana",
          timestamp: "2026-08-28T15:00:00Z",
          notes: "too risky",
        },
      }),
    );
    renderCard({ onDecide });

    await user.click(screen.getByRole("button", { name: "Reject" }));
    const reviewer = screen.getByLabelText("Reviewer");
    await user.clear(reviewer);
    await user.type(reviewer, "dana");
    await user.type(screen.getByLabelText(/Notes/), "too risky");
    await user.click(screen.getByRole("button", { name: "Confirm rejection" }));

    expect(onDecide).toHaveBeenCalledWith({
      proposalId: "FIX-001",
      decision: "rejected",
      reviewer: "dana",
      notes: "too risky",
    });
  });

  it("blocks a submission with no reviewer identity", async () => {
    const user = userEvent.setup();
    const onDecide = vi.fn();
    renderCard({ onDecide });

    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.clear(screen.getByLabelText("Reviewer"));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));

    expect(onDecide).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Enter a reviewer identity before submitting.",
    );
  });

  it("disables the confirm action while a decision is in flight", async () => {
    const user = userEvent.setup();
    let resolve!: (value: SubmitApprovalResponse) => void;
    const pending = new Promise<SubmitApprovalResponse>((r) => {
      resolve = r;
    });
    const onDecide = vi.fn().mockReturnValue(pending);
    renderCard({ onDecide });

    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));

    expect(onDecide).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Submitting…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();

    resolve(approvalResponse());
    await waitFor(() => expect(screen.getByRole("status")).toBeInTheDocument());
  });

  it("surfaces an actionable error when the backend rejects the operation", async () => {
    const user = userEvent.setup();
    const onDecide = vi
      .fn()
      .mockRejectedValue(
        new ApiError(
          409,
          "proposal_already_reviewed",
          "Proposal 'FIX-001' has already been reviewed.",
        ),
      );
    renderCard({ onDecide });

    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.click(screen.getByRole("button", { name: "Confirm approval" }));

    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Proposal 'FIX-001' has already been reviewed.",
      ),
    );
  });

  it("disables the actions once a real decision is recorded", () => {
    renderCard({
      onDecide: vi.fn(),
      approvals: [
        {
          proposal_id: "FIX-001",
          status: "REJECTED",
          decision: "rejected",
          approved_by: "dana",
          timestamp: "2026-08-28T15:00:00Z",
          notes: "Reviewed and rejected.",
        },
      ],
    });

    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.getByText("decision recorded")).toBeInTheDocument();
    expect(
      screen.getByText(/This proposal has already been reviewed/),
    ).toBeInTheDocument();
  });

  it("still allows a decision to override the automated gate record", () => {
    renderCard({
      onDecide: vi.fn(),
      approvals: [
        {
          proposal_id: "FIX-001",
          status: "REJECTED",
          decision: "rejected",
          approved_by: "human",
          timestamp: "2026-08-28T15:00:00Z",
          notes: "Defaulting to rejected: no interactive reviewer.",
        },
      ],
    });

    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(
      screen.getByText(/Recorded by the non-interactive pipeline gate/),
    ).toBeInTheDocument();
  });
});
