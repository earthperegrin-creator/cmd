# The safest path from curiosity to a real trial

CMD is alpha software. Use three increasing levels of trust instead of handing
it private client context on the first run.

## Level 1: inspect the fictional workspace

```bash
python3 scripts/demo_workspace.py serve
```

Inspect four cases:

1. Alder Health: a reviewable recommendation artifact.
2. Brightfield Climate: the exact email payload waiting for approval.
3. Juniper Desk: a fresh worker heartbeat.
4. Maya's consulting practice: an honest missing-source blocker.

Do not approve the fictional email. The point is to understand the boundary,
not to simulate success.

## Level 2: verify your local agent adapter with synthetic work

If Codex or Claude Code is already installed, run its public worker canary:

```bash
python3 scripts/run_public_worker_canary.py --provider codex
```

Use `--provider claude` for Claude Code. The canary creates disposable state,
supplies fictional context, asks for one read-only recommendation, validates
exactly one typed receipt, and checks that no external operation was proposed.
It does not read a private CMD workspace or connect an account.

## Level 3: create an empty private workspace

Ask an agent to follow `docs/AGENT-SETUP.md`, choose `none` for authorized
sources, and keep the background agent set to `none`. Add two or three
nonsensitive Outcomes and inspect how they feel in the interface.

At this point you have tested:

- whether the Outcome model fits your work;
- whether the fictional control flow makes sense;
- whether your selected agent can satisfy CMD's typed synthetic receipt; and
- whether the private local interface is useful without granting data access.

## Stop before live client work

The current public alpha does not yet provide a newcomer-ready connector path
or prove the full JobSpec, broker, provider effect, and independent-verification
chain against real accounts. Do not add sensitive client sources or enable a
consequential connector merely because the demo and canary pass.
