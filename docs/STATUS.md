# Public alpha status

This file is the shortest authority for what the public CMD repository proves
today. Code may contain building blocks for a larger architecture. A building
block is not a live end-to-end capability.

## Capability matrix

| Capability | Status | Executable proof | Current limit |
|---|---|---|---|
| Local Outcomes and one level of Next moves | Shipped | `test_public_alpha.py`, fictional demo | Single-player and early UI |
| Work Threads with chronological human and agent turns | Shipped | fictional demo | Synthetic first-run data |
| Reviewable artifacts, blockers, and exact approval previews | Shipped in local model | fictional demo, worker canary | Demo approval does not exercise a real provider effect |
| Private state outside Git | Shipped | onboarding and lifecycle tests | Legacy dogfood `.cmd/` remains supported |
| Codex and Claude public worker adapters | Alpha | `scripts/run_public_worker_canary.py` | Bounded synthetic work only; no polished connector setup |
| Typed worker envelope and receipts | Alpha | worker-contract tests and schemas | Envelope is not a compiled JobSpec |
| Versioned JobSpec, capability, broker, resolver, and verifier libraries | Building blocks shipped | focused runtime tests | Not wired through the public worker as one live chain |
| Authorized source paths | Consent record shipped | onboarding tests | Public worker does not automatically mount or read them |
| Gmail, Calendar, and other live connectors | Not newcomer-ready | none in the public alpha canary | Disabled by default; setup and effect verification incomplete |
| Full real-account execution with independent verification | Not shipped | none | Target architecture |
| Teams, shared workspaces, RBAC, autonomous organizations | Not planned | not applicable | CMD remains single-player |

## What a safe evaluator may conclude

- CMD has a coherent, working local model for Outcomes, agent states, artifacts,
  approval previews, blockers, and history.
- The fictional consultant demo is isolated and cannot touch normal CMD state.
- The public adapters can launch Codex or Claude on one synthetic bounded job
  and validate a typed receipt.
- The project has unusually explicit target contracts for identity, policy,
  approval, and verification.

## What a safe evaluator must not conclude

- That a compiled JobSpec currently governs every public worker run.
- That checked-in Tool entries are connected or granted.
- That recording an authorized source path makes that source available to a
  worker.
- That an approval preview proves a real Gmail or Calendar effect path.
- That a model's `completed` status alone proves successful work.

## Release evidence

Run the core checks from the repository root:

```bash
python3 -m unittest discover -s . -p 'test_*.py'
python3 scripts/audit_public_foundation.py
python3 scripts/run_lifecycle_canary.py
python3 scripts/run_server_security_canary.py
```

The authenticated provider canary is opt-in:

```bash
python3 scripts/run_public_worker_canary.py --provider both
```

Retained canary receipts live under `evals/reports/`.

