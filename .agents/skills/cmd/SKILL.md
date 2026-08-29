---
name: cmd
description: Evaluate, preview, set up, or operate the CMD local-first command center for personal AI-agent work. Use when a person asks whether CMD fits, asks to install or configure CMD, wants a safe fictional trial, or explicitly invokes $CMD capture.
---

# CMD

CMD lets one person record work once, dispatch bounded agent work, and review
the resulting artifact, exact approval request, or blocker inside the same
Outcome history.

## Select one mode

### Evaluate

1. Read `README.md`, `docs/STATUS.md`, and `docs/SAFE-TRIAL.md`.
2. Run the isolated fictional demo only.
3. Report who CMD fits, what the alpha proves, what remains target architecture,
   and the safest next step.
4. Do not inspect personal files, import data, or enable a connector.

### Set up

1. Follow `docs/AGENT-SETUP.md` exactly.
2. Ask only its three onboarding questions.
3. Read only explicitly granted sources.
4. Confirm the profile and 90-day Outcomes before initialization.
5. Run `./cmd setup`, then `./cmd doctor`.
6. Report the private state location and every disabled worker or connector.

Source authorization is recorded consent. The current public worker does not
automatically mount or read those sources.

### Capture with `$CMD`

1. Treat `$CMD`, `cmd capture`, `capture cmd`, and `$cmd-capture` as explicit
   entry into CMD's bounded resolver.
2. Pass the marker-stripped development and only UI-supplied, focused, or
   task-bound candidate Outcome IDs through `scripts/task_capture.py`.
3. Never invent IDs, supply broad unrelated candidates, or write SQLite
   directly.
4. Propagate `needs_clarification` and `no_capture` honestly.
5. Accept mutation success only from a receipt with `ok: true` and
   `verification.verified: true`.

Example:

```bash
python3 scripts/task_capture.py --request-json '<canonical request JSON>'
```

The script uses CMD's normal private-state discovery unless `--db` is supplied.

### Develop

Follow `AGENTS.md`. Read `PRODUCT.md` before product changes and `DESIGN.md`
before visible interface changes. Keep `docs/STATUS.md` aligned with executable
proof.

## Safety boundary

- Research, analysis, synthesis, and reviewable drafting may be low risk when
  their sources are authorized.
- A registry entry describes a possible operation. It is not a live connection
  or capability grant.
- Sending, posting, calendar writes, purchases, deletions, and destructive
  changes require approval of the exact payload.
- A worker saying `completed` is insufficient without an artifact, conclusion,
  source evidence, or independently readable receipt.
- Missing identity, context, authority, or evidence produces a blocker, not a
  guess.

## Current alpha truth

The public alpha proves the local Outcome, Work Thread, queue, demo approval,
typed worker receipt, lifecycle, and loopback-security model. Its public worker
uses a bounded alpha envelope. The full compiled JobSpec, lease, broker,
connector, and independent-verifier path is not yet wired end to end for real
accounts. Read `docs/STATUS.md` for the current matrix.

