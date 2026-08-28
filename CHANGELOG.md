# Changelog

All notable changes and iterative improvements to the Sentinel project will be documented in this file.

## [Unreleased]

### Baseline
- Initial baseline implementation placeholder: Single-call LLM incident root-cause guessing without tool access or verification checks.
- Baseline evaluation runner and schema definitions initialized.

### Advanced Sentinel (Planned / In Progress)
- Multi-agent evidence extraction (Logs Agent, Metrics Agent, Code Agent).
- Hypothesis Engine generating 1–4 falsifiable hypotheses with evidence references.
- Executable Verification Agent classifying hypotheses (`CONFIRMED`, `REJECTED`, `INCONCLUSIVE`).
- Human Approval Gate for fix proposals.
