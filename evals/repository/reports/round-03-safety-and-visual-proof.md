# Round 03: safety and visual proof

- Date: 2026-08-30
- Working tree: `codex/repo-human-agent-clarity`
- Human score: **32/35** (`0` from Round 02, `+8` from baseline)
- Agent score: **28/35** (`0` from Round 02, `+6` from baseline)
- Combined score: **60/70** (`0` from Round 02, `+14` from baseline)

## Changes evaluated

- Replaced the abstract text loop with a precise record, bounded work, and
  review diagram.
- Added a real screenshot of the shipped Classic alpha using fictional data.
- Added Arctic Command concept art to explain the product philosophy, with an
  explicit label that it is not shipped UI.
- Added `DESIGN.md` and machine-readable design tokens for the current Classic
  interface.
- Required meaningful source evidence rather than a merely nonempty list in a
  completed worker receipt.
- Required capability, execute mode, risk level, and a nonempty payload for an
  approval request.
- Made `cmd doctor` fail when the local server is stopped or the selected worker
  CLI is unavailable.
- Expanded tests and lifecycle evidence for the tighter receipt and doctor
  contracts.

## Fractional CMO

| Dimension | Round 02 | Round 03 | Delta |
|---|---:|---:|---:|
| Five-second clarity | 5 | 5 | 0 |
| Personal relevance | 5 | 5 | 0 |
| Product comprehension | 5 | 5 | 0 |
| Trust and human control | 4 | 4 | 0 |
| First-run confidence | 4 | 4 | 0 |
| Desire to try it now | 5 | 5 | 0 |
| Confidence it can keep complex work organized | 4 | 4 | 0 |

Likely next action: run the fictional workspace and Codex synthetic canary,
then create an empty private workspace with two nonsensitive Outcomes, no
authorized sources, and no connectors.

Remaining blockers:

1. The real-work path still lacks mounted sources, a live full JobSpec and
   capability chain, newcomer-ready connectors, and independent verification.
2. The safe trial stops before one useful private job produces a verified
   artifact from authorized context.
3. The Classic interface still exposes system-facing controls beside the
   person's Outcomes, while Arctic Command remains concept art.

> "I finally understand both the product and why I want it, but the
> control-room vision is still one proof ahead of reality: I need to see a real
> client-safe job go from authorized context to verified result."

## ChatGPT scout

| Dimension | Round 02 | Round 03 | Delta |
|---|---:|---:|---:|
| Solution match and explainability | 4 | 4 | 0 |
| Discovery by an AI agent | 5 | 5 | 0 |
| Install and setup delegability | 4 | 4 | 0 |
| Agent operating contract | 3 | 3 | 0 |
| Permission and safety clarity | 4 | 4 | 0 |
| Machine-readable pathways | 4 | 4 | 0 |
| Confidence recommending a trial | 4 | 4 | 0 |

Verdict: **test cautiously**.

Exact next action: run the isolated fictional demo and inspect all four control
states without approving anything or adding private sources.

Remaining blockers:

1. The complete real-context and independently verified execution chain is not
   wired end to end.
2. Receipt validation proves nonempty structure, not operation-specific payload
   correctness or a substantively meaningful conclusion.
3. Outside the isolated demo, safe-trial limits remain operator instructions
   rather than technically enforced product modes.

> "I trust CMD to demonstrate how agent work should be organized, but not yet
> to prove that real client work was completed correctly or that an approved
> external payload was actually valid."

## What the plateau means

The repository no longer loses points because a newcomer cannot understand the
idea, find the agent entry path, or identify the safe trial. The remaining
ceiling is product evidence. More repository polish would not earn the missing
points honestly. The next material evaluation round should begin only after one
capability-aware, authorized-context job can produce an independently verified
result without exposing a real client account.
