# Local configuration

CMD is local-first. Product source stays in the Git checkout; private data is
stored separately.

## Private state

Fresh macOS installations default to:

```text
~/Library/Application Support/CMD/state
```

Set `CMD_HOME` to move the whole private CMD home or `CMD_STATE_DIR` to select
one state directory. Existing dogfood checkouts with a `.cmd/` directory keep
using it for backward compatibility; a fresh clone does not create private
state inside the repository.

The state directory contains the local profile, SQLite database, settings,
registries, policies, receipts, logs, and backups. It is never required for the
public source tree to compile or run tests.

## Private context

`CMD_CONTEXT_DIR` reserves one user-approved context directory for future
worker mounting. The current public-alpha worker does not automatically read or
mount it. CMD never discovers sibling folders automatically.

## Lifecycle

```sh
./cmd setup
./cmd start
./cmd stop
./cmd status
./cmd doctor
./cmd backup
./cmd restore /path/to/cmd-backup.tar.gz
./cmd update
./cmd uninstall
```

`uninstall` stops CMD and preserves private data. `uninstall --purge-data` is a
separate explicit destructive operation.

Lifecycle safety guarantees:

- setup makes the private state directory owner-only;
- backup uses SQLite's consistent online-backup operation, excludes transient
  runtime and WAL files, and creates an owner-readable archive;
- update refuses a dirty product checkout, backs up private state, stops a live
  service, fast-forwards Git, runs database migrations, and restarts the same
  workspace;
- starting from a moved checkout safely replaces a stale server only when its
  recorded process and private state both match; and
- uninstall never deletes private data without the explicit `--purge-data`
  flag, which also refuses broad filesystem targets.

Maintainers can verify the full behavior in a disposable installation and local
Git remote:

```sh
python3 scripts/run_lifecycle_canary.py
```

The retained release evidence is written to
`evals/reports/lifecycle-canary-latest.json`.

## Network boundary

CMD binds to `127.0.0.1` by default. Binding to another interface is refused
unless `CMD_AUTH_TOKEN` is configured. The server validates Host and Origin,
serves only the application shell and API, and does not expose repository or
private-state files.

## Environment reference

| Variable | Default | Purpose |
| --- | --- | --- |
| `CMD_HOME` | platform user-data directory | Parent for fresh private CMD state |
| `CMD_STATE_DIR` | `<CMD_HOME>/state` | SQLite, queue ledgers, settings, logs, and receipts |
| `CMD_WEEKLY_LOG` | auto-detected example | One explicit weekly context file |
| `CMD_WEEKLY_LOG_ROOT` | `examples/` | Directory searched for an open weekly log |
| `CMD_CONTEXT_DIR` | unset | Reserved explicit context scope; not mounted by the current public worker |
| `CMD_RESOLVER_REGISTRY_OVERLAY_DIR` | `<state>/resolver-registry/v1` | Optional extension-only local Tool, Skill, and Workflow manifests |
| `CMD_STARTUP_DB` | `<state>/external/startup.db` | Optional structured company/context database |
| `CMD_SWEEP_TOOL` | unset | Optional private email-sweep helper path; no helper is bundled |
| `CMD_USER_EMAIL` | unset | Address used to recognize mail sent directly to the user |
| `CMD_USER_IDENTITY` | unset | Optional display identity passed to email triage |
| `CMD_AUTH_TOKEN` | unset | Required authentication secret for a non-loopback bind |
| `CMD_REGISTRY_RESOLVER_SHADOW` | disabled | Append bounded intake observations for later semantic resolution |
| `CMD_REGISTRY_RESOLVER_MODEL` | `gpt-5.4-mini` | Model used by the observation-only shadow processor |
| `CMD_REGISTRY_SHADOW_QUEUE` | `<state>/resolver-shadow-queue.jsonl` | Optional shadow queue override |
| `CMD_REGISTRY_SHADOW_RESULTS` | `<state>/resolver-shadow-results.jsonl` | Optional shadow result-ledger override |
| `CMD_REGISTRY_SHADOW_REVIEWS` | `<state>/resolver-shadow-reviews.jsonl` | Optional private human-review ledger override |
| `CMD_REGISTRY_SHADOW_COHORT` | `<state>/resolver-shadow-cohort.json` | Optional private holdout-cohort definition override |
| `CMD_LIVE_OUTCOME_SHAPER` | disabled | Use semantic work resolution before dedicated Agent input can create or bind an Outcome |
| `CMD_OUTCOME_SHAPER_MODEL` | `gpt-5.4-mini` | Model used only to propose work binding and concise Outcome shape |
| `CMD_OUTCOME_SHAPER_REASONING_EFFORT` | `medium` | Reasoning effort for the live work-only proposal |

Paths may be absolute or relative to the process working directory. Runtime
state and private context must remain outside version control.

## Workspace identity

The title and two ribbon labels are editable in Settings and stored in the
private runtime settings file. Environment defaults are also available:

- `CMD_WORKSPACE_TITLE`
- `CMD_WORKSPACE_LABEL`
- `CMD_FOCUS_LABEL`

## Agents and connectors

A fresh workspace uses `background_agent: none`. Select Codex or Claude in
Settings only after the corresponding CLI works on the machine. Gmail intake
and semantic triage also start off.

Private skills and playbooks belong under the directory supplied through
`CMD_CONTEXT_DIR`. CMD never guesses a sibling personal workspace for a fresh
installation.

Resolver personalization is also local-first. CMD loads the checked-in
universal registry, then merges any `tools.json`, `skills.json`, or
`workflows.json` found under the private resolver registry directory. The
overlay may add primitives but cannot replace universal IDs, invocations, or
Tool operations; collisions fail visibly during validation.

## Registry resolver shadow

The source contains observation and review building blocks used during private
dogfood evaluation. The public alpha does not ship the historical corpus or
operator scripts that process and freeze those holdouts. Keep the feature off
unless you are extending the public evaluation harness and adding equivalent
checked-in fixtures, commands, and release evidence.

## Live Outcome Shaper

The source includes an experimental semantic proposal stage for Outcome
binding. It is disabled in the public alpha and has no newcomer setup path. A
model may propose `no_capture`, one bounded existing Outcome ID, one newly
named Outcome, or clarification. Deterministic software validates confidence,
title quality, category authority, and candidate IDs; only the verified capture
transaction may write SQLite. A model or validation failure creates no Outcome
and dispatches no worker.

Each validated proposal is atomically retained under the private state
directory before mutation. Request retries reuse the same proposal; a request
ID cannot be rebound to new words.

This flag promotes only work resolution and naming. Workflow selection, Tool
authorization, approvals, provider effects, persistence verification, and
receipts remain separate deterministic control-plane stages.

## Sanitized demo

```bash
python3 scripts/demo_workspace.py serve
```

The command creates isolated `.cmd-demo/` state and opens the normal product
server. Reset it with:

```bash
python3 scripts/demo_workspace.py reset
```
