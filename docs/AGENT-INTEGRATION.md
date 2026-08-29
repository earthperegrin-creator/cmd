# Agent Integration

This is the short reference for agents operating with CMD.

This document separates the **public-alpha worker contract** from the **target
control-plane architecture**. The current public worker receives the bounded
envelope in [`worker-envelope.v1.json`](../schemas/worker-envelope.v1.json) and
returns [`worker-result.v1.json`](../schemas/worker-result.v1.json). It does not
yet receive a compiled `JobSpec`, capability lease, brokered connector, or
independent verifier. See [`STATUS.md`](STATUS.md) before describing a
capability as live.

## The Division of Responsibility

An agent provides reasoning, planning, drafting, tool selection, and execution
within an approved job. CMD provides identity resolution, contract creation,
policy, lifecycle state, effect boundaries, verification, and history.

The agent is not the control plane. It is a worker inside the control plane.

## Deterministic CMD Capture

`$CMD` is the canonical explicit capture skill; `cmd capture`, `capture cmd`, and `$cmd-capture` are aliases. Pass the marker-stripped development, at most three recent user-authored turns, and only selected/focused/task-bound/UI-supplied candidate item IDs through:

```bash
python3 scripts/task_capture.py --request-json '<canonical request JSON>'
```

The model may resolve among those candidates and summarize the development. It must not invent an ID, write `work_items` directly, or claim that a status flip preserved context. A result is successful only when the typed receipt reports `verification.verified=true`; otherwise propagate `needs_clarification` or block. Dedicated Task creation may keep using the positional CLI, which is a compatibility adapter to the same transaction.

`$CMD` activates interpretation, not mandatory mutation. If the text is an
acknowledgement, reaction, retraction, or conversational repair, propagate the
verified `no_capture` receipt and do not create an outcome to satisfy the
marker.

When resolver observation is enabled, this canonical transaction is also the
shared observation boundary. A `$CMD` request from Codex, Claude, ChatGPT, the
web interface, or an explicitly promoted inbox item enters the same Resolver
Center corpus. Unmarked conversation and unpromoted notes do not.

## Registry-First Execution Resolution

Before JobSpec compilation, resolve execution against the checked-in
`registries/v1/` Tool, Skill, and Workflow manifests and return
`schemas/resolution-plan.v1.json`. Explicit Skill invocation strongly
constrains selection, but it does not bypass outcome binding, Tool grants,
approval, or verification. Reject or clarify any plan that invents a primitive,
drops a declared prohibition, changes Workflow steps, or lacks independently
readable acceptance criteria.

The canonical plan is compiled into JobSpec only after work binding is
verified. JobSpec carries both granted capabilities and explicit forbidden
capabilities, so a constraint such as `gmail.send` remains visible through the
worker handoff even though no send grant exists.

Resolver callers also name `available_inputs`: bounded Skill input identifiers
whose content or context references are already present. Phrases such as
"supplied notes" or "the selected file" are not proof that an artifact exists;
the resolver clarifies unless the corresponding input is explicitly available.

## What a full control-plane agent should receive

The target execution path may provide:

- the human request and linked task;
- the compiled `JobSpec`;
- resolved context references and their provenance;
- the inputs explicitly allowed by the contract;
- the selected Workflow and Skills, Tool grants, and approval state; and
- instructions for acceptance criteria and reporting artifacts.

The current public-alpha worker receives actions, a confirmed profile, local
policy, and registry descriptions. Registry descriptions are vocabulary, not
proof of a live connection or grant. The worker must block when the concrete
input or authority is absent.

## What an Agent Must Do

1. Read the `JobSpec` before acting.
2. Use only the capabilities and selectors granted by the contract.
3. Treat unresolved, conflicting, or stale context as a reason to stop and
   report, not as an invitation to guess.
4. Produce the requested artifact and the evidence needed by the verifier.
5. Report failure honestly, including partial work and the last safe point.
6. Leave external effects to the approved broker or resident service.

## What an Agent Must Not Do

- change a recipient, company, record, or operation because another candidate
  seems more likely;
- turn a draft request into a send or write;
- grant itself a capability;
- treat a successful API call as proof that the intended result exists;
- bypass approval because the action appears harmless; or
- hide a tool call, failure, or change from the job record.

## Native Sessions and Headless Workers

CMD supports two related ways of working:

- A **native session** is an interactive Codex or Claude conversation where the
  human and agent reason together. CMD provides the task and control context.
- A **headless worker** is a dispatched process that executes a compiled job
  without requiring a live conversation for every step.

Both should use the same contract, capability definitions, and evidence rules.
The difference is interaction style, not authority.

## The Reasoning Loop

The reasoning loop is mostly performed by the model runtime:

```text
read contract and context
    -> plan the next safe step
    -> call an allowed tool
    -> inspect the result
    -> revise the plan
    -> repeat until the acceptance criteria are met or the job must stop
```

CMD enables this loop by supplying bounded tools, persistent job state, and a
way to report artifacts and receipts. CMD does not need to reproduce the model
provider's internal reasoning. Its responsibility is to constrain the loop and
to judge the externally visible result.

## Context References

A `ContextRef` should make a noun in the human request operationally precise.
It should carry enough information to answer:

- what object was selected;
- how it was located;
- which source supported the selection;
- when that source was observed; and
- whether the result is ambiguous, conflicting, or stale.

For example, the string `Acme` is not a sufficient target. A useful reference
would identify the canonical company record, the matching domain or database
key, and the source evidence used to make that match.

## Reporting Back

At completion, an agent should report:

- what it attempted;
- which contract and context it used;
- what artifacts it created;
- which external effects actually occurred;
- what the verifier confirmed; and
- any remaining uncertainty or follow-up.

The report is evidence for CMD's receipt, not a replacement for verification.

## Agent-Facing Authority

For exact field names, allowed state transitions, schema versions, and adapter
behavior, use the runtime code and schema as authority:

- [`schemas/job-spec.v1.json`](../schemas/job-spec.v1.json)
- [`schemas/worker-envelope.v1.json`](../schemas/worker-envelope.v1.json)
- [`schemas/worker-result.v1.json`](../schemas/worker-result.v1.json)
- [`cmd_runtime/jobspec.py`](../cmd_runtime/jobspec.py)
- [`cmd_runtime/control.py`](../cmd_runtime/control.py)
- [`ARCHITECTURE.md`](ARCHITECTURE.md)

This document explains the design intent. It does not override the executable
contract.

## Public Runtime Adapter Contract

CMD's public worker boundary is provider-neutral. Codex and Claude Code are the
first adapters, but another runtime can be added without changing CMD's work,
approval, or completion semantics.

This is the executable alpha contract. It is deliberately narrower than the
target `JobSpec` architecture described above.

### Boundary

The resident queue owns dispatch selection, liveness, retries, validation, and
persistence. An adapter receives one dispatch, invokes one agent runtime, and
returns a typed result. The model is never allowed to select a different
Outcome, broaden its tools, approve its own external operation, or write
directly to CMD's ledgers.

The adapter process receives:

- `--dispatch-id <id>` for exactly one existing dispatch;
- `CMD_STATE_DIR`, pointing at the user's private CMD state; and
- an optional provider model name.

It may create working files only inside `<CMD_STATE_DIR>/workspace`. Credentials
remain runtime-owned and must not be copied into CMD state or product source.

### Input envelope

Build the bounded input with
`cmd_app.worker_contract.dispatch_input(state_dir, dispatch_id)`. The envelope
contains only:

- dispatch identity and mode;
- pending actions already bound to their Outcomes;
- confirmed profile fragments;
- local policy; and
- registered Tools, Skills, and Workflows.

Do not add filesystem discovery, global memory, unrelated project instructions,
or unregistered connectors to this envelope.

### Required output

The runtime returns exactly one JSON object. One receipt is required for every
pending action and no other action ID is accepted.

```json
{
  "schema_version": 1,
  "dispatch_id": "dispatch-id",
  "receipts": [
    {
      "action_id": "action-id",
      "status": "completed",
      "summary": "One-sentence result",
      "conclusion": "Decision-relevant result",
      "artifact": {},
      "needs_human_review": false,
      "sources": [],
      "model": "provider-model"
    }
  ]
}
```

Allowed statuses are `completed`, `awaiting_approval`, `blocked`, and `failed`.
`completed` requires a non-empty artifact, decision-relevant conclusion, or
source evidence in addition to the summary.
`awaiting_approval` also requires a complete `proposed_operation` object with
the capability, execution mode, risk level, and exact payload. The adapter must
not execute that operation.

Pass raw runtime output to
`cmd_app.worker_contract.parse_worker_output(...)`. CMD accepts an exact JSON
object or one unambiguous fenced JSON object; malformed, partial, duplicate, or
unknown receipts fail closed. Only the parent adapter appends validated receipts
with `append_receipts(...)`.

### Process behavior

A conforming adapter:

1. creates the bounded envelope and generic worker prompt;
2. launches the provider in the private CMD workspace with unrelated settings,
   plugins, connectors, browser control, and network tools disabled where the
   runtime supports it;
3. uses `scripts.worker_lifecycle.run_worker(...)` for heartbeats, timeout, and
   honest launch-failure receipts;
4. validates the provider's final output; and
5. exits `0` only after all receipts validate and persist.

Nonzero launch, timeout, or validation failures must become terminal failure
receipts. An adapter may never report completion merely because the provider
process exited successfully.

### Adding a runtime

Add the provider launch arguments to `scripts/public_worker.py`, preserving the
same envelope, prompt, parser, lifecycle, workspace confinement, and exit-code
behavior. Then add the provider to the canary runner.

Run the conformance check with fictional local data:

```text
python3 scripts/run_public_worker_canary.py --provider both
```

The canary must prove process success, exactly one completed typed receipt, the
expected evidence-backed recommendation, a reviewable artifact or conclusion,
and no proposed external operation. Keep the resulting report at
`evals/reports/public-worker-canary-latest.json` as release evidence.
