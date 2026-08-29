# Round 02: agent entry and contract honesty

- Date: 2026-08-30
- Working tree: `codex/repo-human-agent-clarity`
- Human score: **32/35** (`+4` from Round 01, `+8` from baseline)
- Agent score: **28/35** (`+4` from Round 01, `+6` from baseline)
- Combined score: **60/70** (`+8` from Round 01, `+14` from baseline)

## Changes evaluated

- Added a repo-local CMD skill, `llms.txt`, expanded `AGENTS.md`, and a clear
  read order and repository map.
- Added `docs/STATUS.md` to separate shipped behavior, executable proof,
  building blocks, and target architecture.
- Added `docs/SAFE-TRIAL.md` with a staged demo, synthetic provider canary, and
  empty private workspace.
- Added public worker input and output schemas.
- Added a deterministic report-blind evaluation packet builder.
- Unified setup registries with the checked-in registries.
- Unified capture database discovery with setup and server state discovery.
- Required completed worker receipts to carry an artifact, conclusion, or
  source evidence.
- Removed references to absent private operator scripts and labeled authorized
  source paths as consent metadata rather than mounted context.

## Fractional CMO

| Dimension | Round 01 | Round 02 | Delta |
|---|---:|---:|---:|
| Five-second clarity | 5 | 5 | 0 |
| Personal relevance | 5 | 5 | 0 |
| Product comprehension | 4 | 5 | +1 |
| Trust and human control | 4 | 4 | 0 |
| First-run confidence | 3 | 4 | +1 |
| Desire to try it now | 4 | 5 | +1 |
| Confidence it can keep complex work organized | 3 | 4 | +1 |

Likely next action: follow the safe trial exactly, including the fictional
workspace, Codex synthetic worker canary, and an empty private workspace with
two nonsensitive Outcomes and no sources or connectors.

Remaining blockers:

1. The real-account JobSpec, lease, context, connector, broker, and verifier
   chain is not complete.
2. The safe trial stops before one genuinely useful private agent job.
3. Dogfood controls and a large Agent Queue still compete with Outcomes in the
   interface.

> "This is the first personal agent-control repository I would try without
> immediately fearing a context leak, but its safest trial ends one step before
> it proves it can help with my actual client work."

## ChatGPT scout

| Dimension | Round 01 | Round 02 | Delta |
|---|---:|---:|---:|
| Solution match and explainability | 4 | 4 | 0 |
| Discovery by an AI agent | 4 | 5 | +1 |
| Install and setup delegability | 3 | 4 | +1 |
| Agent operating contract | 3 | 3 | 0 |
| Permission and safety clarity | 4 | 4 | 0 |
| Machine-readable pathways | 3 | 4 | +1 |
| Confidence recommending a trial | 3 | 4 | +1 |

Verdict: **test cautiously**.

Exact next action: run the isolated fictional demo and inspect all four control
states without approving anything or adding private sources.

Remaining blockers:

1. The complete real-work chain is not wired end to end.
2. Runtime validation still accepts an empty approval payload and empty-string
   source evidence despite the stronger published schema.
3. Setup records sources without mounting them, while doctor does not prove a
   selected worker and live browser together.

> "I can safely demonstrate CMD's control model, but I cannot yet trust it to
> carry real client work from context through independently verified
> completion."

## Round 03 target

Add visual proof and conceptual clarity without disguising alpha status. Pair a
real product screenshot with one precise record-to-work-to-review diagram and
one clearly labeled Arctic Command concept image. Close the two concrete worker
receipt validation gaps found in this round, then rerun the same evaluators.
