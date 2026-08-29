# Working with CMD

CMD is a local-first, single-player command center for personal agent work. A
person records an instruction once; CMD binds it to an Outcome, gives a worker
bounded context and authority, and returns an artifact, approval request, or
honest blocker to the same history.

The current repository is an alpha. Read [`docs/STATUS.md`](docs/STATUS.md)
before describing a capability as shipped.

## Choose the mode first

### Evaluate CMD for a person

1. Read `README.md`, `docs/STATUS.md`, and `docs/SAFE-TRIAL.md`.
2. Run only the fictional demo unless the person explicitly asks for setup.
3. Explain fit, non-fit, shipped behavior, and current limitations separately.
4. Do not inspect personal files, adjacent repositories, credentials, browser
   state, or existing agent configuration.

### Set up CMD

Follow [`docs/AGENT-SETUP.md`](docs/AGENT-SETUP.md) exactly. Ask the three
questions there, confirm one concise readback, then initialize through
`./cmd setup`. Source authorization recorded during setup is consent metadata;
the current public worker does not yet mount those sources automatically.

Never enable a worker or connector merely because a CLI or credential exists.

### Operate with CMD

Read [`docs/AGENT-INTEGRATION.md`](docs/AGENT-INTEGRATION.md) and use the
repo-local [`cmd` skill](.agents/skills/cmd/SKILL.md).

For explicit `$CMD` capture:

- use the canonical capture transaction;
- resolve the database through `CMD_STATE_DIR` or CMD's normal state discovery;
- never write `work_items` directly;
- never invent an Outcome ID or broaden the supplied candidate set; and
- accept success only when the typed receipt has `ok: true` and
  `verification.verified: true`.

### Develop CMD

Read `PRODUCT.md`, then `DESIGN.md` when the change touches a visible surface.
Use the repository map and invariants below. Keep public claims aligned with
executable tests and `docs/STATUS.md`.

## Read order

| Need | Read |
|---|---|
| Product promise and audience | `README.md`, `PRODUCT.md` |
| Shipped versus target behavior | `docs/STATUS.md` |
| Safest first evaluation | `docs/SAFE-TRIAL.md` |
| Three-question setup | `docs/AGENT-SETUP.md` |
| Agent and worker rules | `docs/AGENT-INTEGRATION.md` |
| Logical system boundaries | `docs/ARCHITECTURE.md` |
| Trust and threat model | `SECURITY.md` |
| Public/private separation | `PUBLICATION-BOUNDARY.md` |
| Machine discovery | `llms.txt`, `schemas/`, `registries/v1/` |

## Repository map

| Path | Responsibility |
|---|---|
| `index.html` | Local browser interface |
| `server.py` | Loopback HTTP API and application orchestration |
| `cmd_db.py` | Canonical SQLite task and history state |
| `cmd_app/` | Intake, onboarding, queue, worker, and UI-facing services |
| `cmd_runtime/` | JobSpec, policy, broker, resolver, and verifier building blocks |
| `registries/v1/` | Universal Tool, Skill, and Workflow vocabulary |
| `schemas/` | Versioned machine contracts |
| `scripts/` | Demo, public worker, canaries, and publication tooling |
| `docs/` | Human, agent, architecture, status, and configuration guides |
| `evals/repository/` | Frozen human and agent repository evaluators |
| `publication/` | Explicit public snapshot source and allowlist |

## Product invariants

1. CMD is single-player. Do not add teams, tenants, shared rooms, or RBAC.
2. The task database is canonical. Chat transcripts and Markdown are context,
   not task truth.
3. The human instruction, Outcome, work, artifact, and receipt keep one lineage.
4. Drafting and committing an external effect are different operations.
5. The exact external payload requires approval before send, post, calendar
   write, purchase, deletion, or another consequential effect.
6. Models may propose meaning. Deterministic code owns identity, grants,
   persistence, state transitions, and verification.
7. Ambiguity blocks or asks. It never silently changes the target.
8. Private state belongs outside Git. Tests and demos use isolated state.
9. A target architecture is not a shipped capability. Label the difference.

## Definition of done

Before claiming a release-facing change is complete:

1. run `python3 -m unittest discover -s . -p 'test_*.py'`;
2. run `python3 scripts/audit_public_foundation.py`;
3. build and validate a manifest-bounded snapshot;
4. run the relevant lifecycle, worker, or server-security canary;
5. inspect visible changes in the isolated fictional demo;
6. update `docs/STATUS.md` when the shipped boundary changed; and
7. keep `README.md` and `publication/README.md` identical.

Do not claim a connector, approval, broker, JobSpec, or verifier path is live
because its class, schema, or design document exists. Point to the executable
test or canary that proves the claim.
