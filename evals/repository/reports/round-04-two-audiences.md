# Round 04: two audiences, two languages

- Date: 2026-08-30
- Working tree: `codex/readme-human-agent-split`
- Human score: **31/35** (`-1` from Round 03, `+7` from baseline)
- Agent score: **30/35** (`+2` from Round 03, `+8` from baseline)
- Combined score: **61/70** (`+1` from Round 03, `+15` from baseline)

## Changes evaluated

- Rewrote the opening in the creator's conversational voice around the real
  problem of scattered, misnamed, and context-compacted agent chats.
- Separated the repository entrance into `For you, the human` and `For your
  agent`.
- Added a versioned machine protocol with explicit evaluation and setup phases,
  authority, permission boundaries, and an embedded JSON Schema for the result.
- Made demo execution opt-in and declared its state directory, loopback server,
  readiness signal, timeout, stop, cleanup, and read-only fallback behavior.
- Aligned `AGENTS.md` and the repo-local CMD skill with the protocol's permission
  gate.
- Collapsed the machine protocol in GitHub's human-facing rendering while
  keeping the complete contract in README source.
- Replaced the placeholder clone command with the canonical public repository
  URL and retained the private-coupling audit for every other occurrence of the
  creator handle.

The first agent pass exposed phase, lifecycle, and serialization ambiguities.
Protocol v1.2 resolved them before the final report-blind evaluation below.

## Fractional CMO

| Dimension | Round 03 | Round 04 | Delta |
|---|---:|---:|---:|
| Five-second clarity | 5 | 5 | 0 |
| Personal relevance | 5 | 5 | 0 |
| Product comprehension | 5 | 4 | -1 |
| Trust and human control | 4 | 4 | 0 |
| First-run confidence | 4 | 5 | +1 |
| Desire to try it now | 5 | 5 | 0 |
| Confidence it can keep complex work organized | 4 | 3 | -1 |

Likely next action: authorize Codex to run the fictional demo, delete its
synthetic state afterward, then run the synthetic Codex canary. If both feel
coherent, create an empty workspace with three nonsensitive Outcomes and no
client sources or connectors.

Remaining blockers:

1. The public alpha stops before sensitive sources, polished connectors, and
   independently verified real-account effects.
2. One level of Next moves may be insufficient for a deeply nested,
   multi-client workload.
3. The evidence is still synthetic and does not yet prove coherence over weeks
   of interrupted work.

> "The collapse fixes the README: I can stay in the human story and let my
> agent open the machinery."

## ChatGPT scout

| Dimension | Round 03 | Round 04 | Delta |
|---|---:|---:|---:|
| Solution match and explainability | 4 | 4 | 0 |
| Discovery by an AI agent | 5 | 5 | 0 |
| Install and setup delegability | 4 | 4 | 0 |
| Agent operating contract | 3 | 4 | +1 |
| Permission and safety clarity | 4 | 5 | +1 |
| Machine-readable pathways | 4 | 4 | 0 |
| Confidence recommending a trial | 4 | 4 | 0 |

Verdict: **test cautiously**.

Exact next action: ask permission for the isolated fictional demo, disclose its
local effects, enforce the 20-second readiness rule, inspect the four documented
states, stop and confirm exit, then remove the synthetic state as requested.
Do not proceed to setup or client data without a separate authorization.

Remaining blockers:

1. The complete real-account execution and independent-verification chain is
   not wired end to end.
2. Authorized source paths are consent records rather than mounted worker
   context.
3. Live connectors are disabled and not newcomer-ready.

The final agent evaluation reported: **publication blockers: none**.

> "I would trust CMD to organize a fictional week and expose its boundaries;
> I would not yet trust it with five startups' live client systems."

## What the score movement means

The human score traded one point of immediate product simplicity for a safer,
more explicit first run. The machine contract gained two points by becoming
authoritative and executable instead of merely suggestive. The remaining
missing points are product evidence, hierarchy depth, and real-account
verification. They should not be manufactured through more README polish.
