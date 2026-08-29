# Round 00: public-alpha baseline

- Date: 2026-08-29
- Commit: `66b28d2baa8f997c978ce5ab508e4634489637ef`
- Human score: **24/35**
- Agent score: **22/35**
- Combined score: **46/70**

## Fractional CMO

She understood CMD as a local-first control center that binds requests to
outcomes, gives agents bounded work, and brings back artifacts, approvals,
blockers, and receipts.

| Dimension | Score | Evidence |
|---|---:|---|
| Five-second clarity | 4 | The positioning and record-once promise communicate the core idea immediately. |
| Personal relevance | 5 | The cross-domain consultant demo closely matches her life. |
| Product comprehension | 3 | The core objects make sense, but internal control-plane vocabulary obscures the basic loop. |
| Trust and human control | 4 | Local state, disabled defaults, exact approvals, honest blockers, and receipts show strong control instincts. |
| First-run confidence | 2 | The demo starts easily, but there is no complete clone-to-first-use walkthrough and the visible demo state is inconsistent. |
| Desire to try it now | 3 | She would explore the fictional workspace but would not yet connect real context. |
| Confidence it can keep complex work organized | 3 | The hierarchy and work threads are compelling, but the demo undermines them with category, link, and worker-state errors. |

Likely next action: run the fictional demo, inspect one artifact and one
approval flow, then wait for a convincing real-agent walkthrough before using
a private workspace.

Top blockers:

1. The demo contradicts its own story through incorrect categories, broken
   outcome links, and a failed worker that should appear active.
2. There is no newcomer path from clone to one useful, safe agent result.
3. The interface presents queue and control-plane detail before teaching the
   simple outcome to agent work to human review loop.

> "CMD is aimed directly at the problem I have, and I trust its instincts, but
> right now I feel invited to dogfood an intelligent control-plane prototype,
> not adopt a dependable operating system for my work."

## ChatGPT scout

The agent explained CMD as a local-first work-order system where a person
records an outcome once, dispatches work to Codex or Claude, and reviews a
typed result, approval request, or blocker.

| Dimension | Score | Evidence |
|---|---:|---|
| Solution match and explainability | 4 | The core model and consultant demo match the stated need, but the human guide mixes current behavior with the target product. |
| Discovery by an AI agent | 4 | Root agent files point to canonical setup and architecture, although some references have drifted. |
| Install and setup delegability | 3 | Setup and doctor are simple, but connector setup is absent and state locations appear inconsistent. |
| Agent operating contract | 2 | The documented contract is strong, but the public worker does not prove the full JobSpec, lease, broker, and verification path. |
| Permission and safety clarity | 4 | Source consent, disabled defaults, loopback serving, and exact approval are explicit. |
| Machine-readable pathways | 3 | Versioned schemas, registries, JSON APIs, and typed receipts exist, but some public contracts drift. |
| Confidence recommending a trial | 2 | Current tests prove a narrow synthetic path, not sustained use with real context and connectors. |

Verdict: **test cautiously**.

Exact next action: run only the isolated fictional demo and inspect its ready,
approval, working, and blocked cases without importing real data or connecting
accounts.

Top blockers:

1. The advertised control contract is stronger than the shipped execution
   path the repository proves.
2. Fresh installations may split task truth and do not mount authorized
   context into the public worker.
3. Documentation, registries, and checked-in scripts show material drift.

> "I can explain CMD's intended control plane more confidently than I can
> prove the shipped worker actually runs inside it."

## Round 01 target

Improve the human front door without hiding alpha limitations: make the core
loop concrete, explain fit and non-fit, provide a complete fictional quickstart,
and separate shipped behavior from architectural direction.
