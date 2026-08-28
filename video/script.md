# Sentinel Solution Video Script (5-Minute Maximum)

## Video Overview
- **Target Duration**: 4:30 – 5:00 minutes
- **Core Message**: *Sentinel does not stop at a plausible root-cause guess. It gathers isolated evidence, generates falsifiable hypotheses, executes invariant checks, and only reports a root cause when verified.*

---

## Script Breakdown

### Act 1: The Problem & The Danger of "Plausible" AI (0:00 - 0:50)
- **Visual**: Terminal showing fragmented logs, Grafana spike, and git diff.
- **Narrator**:
  > *"When production breaks at 2 AM, on-call engineers are inundated with logs, metric spikes, and recent git commits. If you feed this incident bundle into standard ChatGPT or Claude, it gives you a smooth, persuasive diagnosis in seconds. But here is the catch: plausible is not verified. A single-shot LLM often picks the most obvious red herring, hallucinations follow, and teams execute the wrong rollback."*

### Act 2: Introducing Sentinel & The Architecture (0:50 - 1:40)
- **Visual**: Clean animated architecture diagram showing Orchestrator -> Specialist Agents -> Hypothesis Engine -> Verification Agent -> Fix Proposal Gate.
- **Narrator**:
  > *"Meet Sentinel: an evidence-backed incident investigator designed specifically to eliminate plausible-but-wrong diagnoses. Rather than guessing, Sentinel isolates evidence items from logs, metrics, and code, generates competing falsifiable hypotheses, and writes executable checks to verify them against reality."*

### Act 3: Live Incident Walkthrough (1:40 - 3:30)
- **Visual**: Terminal recording / UI walkthrough of an incident bundle investigation.
- **Narrator**:
  > *"Let's watch Sentinel in action on a real failure scenario: a connection pool exhaustion incident.
  > 1. Specialist agents collect raw evidence items: `EV-LOG-001` (timeout spikes) and `EV-CODE-002` (unclosed database connection in new retry middleware).
  > 2. The Hypothesis Engine generates two competing hypotheses: DB CPU saturation vs. Connection Leak in retry loop.
  > 3. The Verification Agent executes an invariant check against metric streams. Hypothesis 1 is REJECTED because DB CPU was at 12%. Hypothesis 2 is CONFIRMED because active pool connections never decremented.
  > 4. Sentinel compiles the verified report with cited evidence IDs and drafts a patch + regression test, held at a human approval gate."*

### Act 4: Comparative Evaluation & Baseline Benchmark (3:30 - 4:20)
- **Visual**: Evaluation benchmark comparison table (Baseline vs. Sentinel).
- **Narrator**:
  > *"We evaluated Sentinel and a single-call baseline across 10 synthetic backend incident bundles. While the baseline suffered from plausible red herrings on complex incidents, Sentinel achieved 100% verification rigor with zero ungrounded diagnoses."*

### Act 5: Conclusion & Reproduction (4:20 - 5:00)
- **Visual**: Repository link, REPRODUCE.md, and concluding takeaway slide.
- **Narrator**:
  > *"Sentinel is fully reproducible, lightweight, and built with verification at its core. Production reliability demands evidence, not guesses. Thank you."*
