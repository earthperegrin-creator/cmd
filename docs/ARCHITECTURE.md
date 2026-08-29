# CMD System Map

This document is the compact logical map of CMD. It is written for a human who
wants to understand the system and for an agent or maintainer that needs to
know where a decision belongs.

## End-to-End Flow

```text
human or native agent
        |
        v
captured action and task context
        |
        v
work admission and outcome binding
        |
        v
workflow, skill, and tool resolution
        |
        v
context resolution and preflight
        |
        v
JobSpec compilation
        |
        v
capability and policy decision
        |
        +---- blocked or awaiting approval
        |
        v
local job state and queue
        |
        v
worker using Codex or Claude
        |
        v
broker or resident service
        |
        v
provider adapter and external effect
        |
        v
verification, artifact, and receipt
```

The same control stages can be replayed without the worker or provider. That
is the evaluation path used to test changes before live execution.

## Component Boundaries

| Component | Owns | Does not own |
| --- | --- | --- |
| Interface | Display, capture, and human correction | Canonical task truth or authorization |
| Native agent session | Reasoning, tool use, and proposing work | Capability grants or completion proof |
| Action intake | The original request and its task link | The final execution decision |
| Work resolver | No-capture, existing outcome, new outcome, or clarification | Persisting an unverified mutation |
| Execution resolver | Registered Workflow, Skills, Tool operations, prohibitions, and acceptance | Inventing primitives or granting authority |
| Context resolver | Exact identity, provenance, and ambiguity result | Workflow selection, external writes, or worker startup |
| `JobSpec` compiler | The concrete execution contract | Provider implementation details |
| Capability policy | Allow, block, or request approval | Model reasoning |
| State ledger | Job lifecycle and history | Guessing what a job meant |
| Worker | Performing the approved contract | Expanding the contract |
| Broker | Enforcing the effect at the service boundary | Deciding the human's intent |
| Provider adapter | Translating an allowed effect to a service | General workflow policy |
| Verifier | Checking acceptance criteria | Treating a model claim as proof |
| Replay engine | Testing decisions without side effects | Executing live work |

These boundaries are intentionally repetitive. A safety boundary that exists
only in a prompt is not a reliable system boundary.

## Registry-First Resolution

The product resolver works from the versioned manifests in `registries/v1/`.
Natural language remains the main input, and explicit invocations such as
`$email` are deterministic fast paths. The model selects only from bounded
candidates and returns `schemas/resolution-plan.v1.json`. Deterministic software
then checks outcome candidates, Workflow steps, Skill contracts, Tool
connections and grants, prohibitions, approval gates, and acceptance criteria.

The low-level `capabilities/v1/registry.json` remains the authorization ledger.
Tools, Skills, and Workflows make intent understandable; capabilities make
effects enforceable.

## Live Outcome Shaper

The dedicated browser Agent composer uses a narrow semantic stage before an
action can enter the queue:

```text
raw instruction
        -> deterministic bounded Outcome retrieval
        -> read-only model proposal
        -> deterministic proposal validation
        -> durable request-keyed proposal journal
        -> verified Outcome bind/create transaction
        -> action append and worker eligibility
```

The model proposes meaning: whether the request is durable, whether it belongs
to one supplied Outcome, a useful new title, the finish condition, and the next
move. It receives at most 12 active candidate IDs and cannot inspect CMD,
invoke Tools, or write state.

Software controls reality: it validates the closed schema, confidence,
candidate identity, explicit human category, title bounds, field consistency,
and current Outcome status. Before mutation, it atomically journals the exact
validated proposal under a hash of the browser request ID. A crash retry must
reuse that proposal; the same request ID with different instructions fails
closed. Software then either performs the existing idempotent SQLite
transaction and readback, records an unbound action, or fails closed with
clarification. Worker startup, execution resolution, Tool grants, approval,
provider commits, and completion verification happen later and are not
authority granted by the work-resolution model.

This is intentionally not one general-purpose agent call. Interpretation and
execution are separated so a clever model can absorb human phrasing without
being trusted to mutate or authorize the system it is interpreting.

## The Pure Context Resolver Boundary

The context resolver is a read-only function later in the control path:

```text
request + available local context
        -> candidates and evidence
        -> resolved ContextRefs, or ambiguity/missing/conflict
```

It should be:

- deterministic for the same inputs and context snapshot;
- provider-free;
- free of queue, database-write, and external side effects;
- explicit about confidence, evidence, provenance, and freshness; and
- consumable by the existing `JobSpec` compiler.

It is not the work or execution resolver. It does not choose whether Codex or
Claude should run, create a new queue, or select a Tool operation. Its job is
narrow: resolve the nouns and sources that make the already bounded operation
safe.

## State and Evidence

The local runtime has several kinds of state. They serve different purposes:

- **Task state:** what the human is trying to accomplish.
- **Job state:** what CMD has compiled and what lifecycle state it is in.
- **Action history:** what was requested or proposed.
- **Source context:** documents, records, and policies consulted by the job.
- **Artifacts:** outputs produced by execution.
- **Receipts:** verification and provider evidence about the outcome.

The system should not collapse these into one chat transcript. A transcript is
useful context, but it is not a reliable execution ledger.

## Resolver Stress Gate

Run the checked-in pure replay corpus from the repository root:

```bash
python3 scripts/stress_context_resolver.py
```

The gate is fixed at 1,000 labeled cases and runs each case twice. It does not
start a worker, invoke a model, call a provider, create a queue entry, or write
live state. It asserts the expected validated or blocked result, checks the
expected `ContextRef` or denial code, compares both replay records for exact
determinism, and checks the zero-effect counters. The local catalog paths can
be overridden with `CMD_STARTUP_DB` and `CMD_PEOPLE_INDEX` or the matching CLI
flags.

The 2026-08-04 run passed 2,000 replays: 500 validated, 500 blocked, zero model
tokens, zero provider calls, and zero writes. It took about 101 seconds because
the current resolver uses a deliberately simple linear scan over the catalog.
That runtime is acceptable for the current single-player dogfood slice; an
indexed catalog is the next performance refinement if the catalog grows.

## Important Invariants

1. `JobSpec` is the authoritative execution contract.
2. The model cannot grant itself a capability.
3. A worker cannot silently change the target or operation.
4. Draft, propose, and send/write are distinct effects.
5. Ambiguous identity blocks or asks; it does not silently guess.
6. External provider success is still checked against acceptance criteria.
7. Revisions produce traceable job history.
8. Replay does not call providers or mutate live state.
9. CMD remains single-player; contracts govern software actors, not a team
   collaboration model.

## Current and Planned

| Area | Current shape | Next refinement |
| --- | --- | --- |
| Task and local runtime | Local CMD state and task-linked actions | Keep task truth separate from execution history |
| Contract | Versioned `JobSpec` with schema tests | Compile resolved context into every relevant job |
| Capabilities | Registry, policy, and approval gates | Expand exact selectors and acceptance criteria |
| Workers | Thin Codex and Claude adapters with shared lifecycle | Keep provider differences out of control logic |
| External services | Controlled service and provider bridges | Tighten target checks at the effect boundary |
| Context | Pure resolver plus read-only company/people catalog loader, explicit task/source references, and target checks | Add CMD task/source/policy snapshots and versioned catalog inputs |
| Testing | Focused runtime tests, shadow replay, and a 1,000-case pure resolver gate | Add historical labeled actions and versioned catalog snapshots |
| Product surface | Single-player browser interface plus native agent sessions | Explain the model clearly to humans and agents |

## Where to Read the Code

The product-facing explanation is in [What CMD Is](HUMAN-GUIDE.md). The
implementation-level references are:

- [`cmd_runtime/jobspec.py`](../cmd_runtime/jobspec.py) for the execution
  contract and context reference structures;
- [`cmd_runtime/control.py`](../cmd_runtime/control.py) for control decisions
  and replay;
- [`capabilities/v1/registry.json`](../capabilities/v1/registry.json) for
  registered effects;
- [`schemas/job-spec.v1.json`](../schemas/job-spec.v1.json) for the contract
  schema; and
- [`AGENT-INTEGRATION.md`](AGENT-INTEGRATION.md) for worker and runtime adapter
  detail.
