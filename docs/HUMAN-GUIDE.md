# What CMD Is

CMD is a personal command center for getting work done with AI.

It connects a human's tasks, local context, files, services, and AI agents in
one controlled execution system. The human can say what needs to happen in
natural language. CMD turns that request into a specific, reviewable job,
decides what the job is allowed to do, lets an agent perform the work, and
records what actually happened.

The important idea is that CMD does not treat an AI response as the same thing
as completed work. A draft is different from a sent email. A proposed database
change is different from a verified database change. CMD keeps those steps
separate.

## What CMD Is Not

CMD is not a team chat application, a general-purpose autonomous company, or a
multi-user collaboration platform. It is deliberately single-player:

- one person's work context;
- one local source of task truth;
- agents acting on that person's behalf;
- explicit limits around external effects; and
- a record of the request, the decision, the work, and the result.

The agents may be different models or runtimes, but they operate inside the
same personal control surface.

## The Simplest Mental Model

Think of CMD as a work order system for a human and their agents.

```text
Human request
    -> understand the requested operation
    -> identify the exact context
    -> compile a precise job contract
    -> check permissions and safety
    -> let an agent do the work
    -> verify the result
    -> record a receipt
```

The agent is responsible for reasoning and using tools. CMD is responsible for
making the job explicit, constraining effects, preserving state, and making
the result inspectable.

## One Example: Drafting an Email

Suppose the human says:

> Draft a follow-up to Alex at Acme about the pricing question from our last
> call.

CMD needs to solve several different problems. They should not all be left to
one model prompt.

1. **Understand the operation.** This is an email draft, not an email send.
2. **Resolve the context.** Which Alex? Which Acme? Which call? Which pricing
   question? CMD should use the available people, company, relationship, task,
   and source records to identify the intended objects.
3. **Surface uncertainty.** If two people named Alex work at Acme, or the call
   cannot be identified, the job should ask or block instead of guessing.
4. **Create the job contract.** The request becomes a `JobSpec` stating the
   operation, exact target, allowed capability, inputs, acceptance criteria,
   and approval requirement.
5. **Apply policy.** Drafting may be allowed automatically. Sending would be a
   different capability and would normally require a separate approval.
6. **Run the work.** A Codex or Claude worker reads the approved context and
   creates the draft.
7. **Verify and record.** CMD records the draft, its source context, the job
   status, and a receipt. The human can see what was produced and why.

This separation is the point of the product. The model can be helpful without
being the final authority over identity, permission, or completion.

## The Logical Components

### 1. Human interface and native agent sessions

The human can work through the CMD interface or through a native agent session
such as Codex or Claude. These are ways to express intent and inspect work.

They are not the authoritative task database and they do not get to redefine
CMD's safety rules. A native agent session is a powerful reasoning surface; CMD
is the controlled execution surface around it.

### 2. Action intake

CMD captures the request as an action associated with a task or work thread.
This preserves what the human actually asked for, rather than keeping only the
agent's final prose.

An action can be a request to draft an email, create a calendar event, update a
record, search the web, write a local file, or create a task.

### 3. Operation classification

CMD classifies the requested operation. In plain language, it asks: *what kind
of effect is being requested?*

Examples include `gmail.draft`, `gmail.send`, `calendar.create`,
`database.update`, `local.write`, and `task.create`.

Classification is not permission. Saying that an action is an email send does
not mean the system has approved an email send.

### 4. Context resolution

Context resolution answers: *what exact thing does this request refer to?*

It may resolve:

- a company from a name, domain, or memo;
- a person from a name, email address, or relationship;
- a task or work thread;
- a source document, policy, or prior decision; and
- the database or service record that is the canonical target.

The planned pure resolver is read-only. It does not send mail, edit records, or
start a worker. It returns a structured result containing the candidate
identity, the locator used, the evidence and provenance, and any ambiguity,
missing data, conflict, or freshness problem.

This is the layer that prevents a worker from acting on whichever "Alex" or
whichever memo happened to appear first in a prompt.

### 5. The `JobSpec` contract

`JobSpec` is the authoritative execution contract. It is the compiled answer
to: *what exactly is this job allowed and expected to do?*

It can include:

- the operation;
- the exact context and target selectors;
- the allowed capability;
- inputs and source references;
- acceptance criteria;
- the verifier to use;
- the approval state;
- retry and timeout rules; and
- links to prior or revised specifications.

The worker does not receive a vague instruction and decide the boundaries for
itself. It receives a job with explicit boundaries.

### 6. Capability registry and policy

The capability registry is the list of effects CMD knows how to authorize. A
capability describes an action such as drafting an email, sending an email, or
writing a file.

Policy decides whether that capability is allowed for this job, whether an
approval is required, and what selectors or conditions must be present. A model
cannot grant itself a capability merely by asking for it.

### 7. Approval and human correction

Some work can be prepared automatically but should not take effect without the
human. CMD can pause a job for approval, show the proposed effect, and accept a
correction.

For example, drafting an email and sending an email are separate operations.
If the human corrects the recipient or target, CMD should produce a revised
job linked to the earlier one rather than silently changing history.

### 8. State ledger

The state ledger records where a job is in its lifecycle. Typical states are
pending, queued, running, awaiting approval, completed, failed, blocked, or
cancelled.

This state belongs to CMD's local database, not to an agent's memory or to a
chat transcript. The ledger makes work resumable and lets the interface show
what needs attention.

### 9. Worker and shared lifecycle

A worker is the process that performs a `JobSpec` using an agent runtime. CMD
currently supports thin Codex and Claude adapters behind a shared worker
lifecycle.

The adapter chooses how to start and communicate with a model. The shared
lifecycle handles common concerns such as loading the contract, writing logs,
tracking exit status, capturing artifacts, and reporting completion.

This keeps the product's rules independent from any one model provider.

### 10. Broker and resident services

External services such as Gmail, Calendar, or a database should be reached
through a controlled broker or resident service. The worker requests an allowed
operation; it does not receive a permanent unrestricted service credential.

The broker is where CMD can enforce the operation, target, and authorization
again at the point where an external effect would happen.

### 11. Provider adapters

Provider adapters translate CMD's internal operation into the API or local
command needed by a specific service. A Gmail adapter knows Gmail's API. A
calendar adapter knows the calendar API. The rest of CMD should not need to
know every provider's details.

### 12. Verification, artifacts, and receipts

The agent's claim that it finished is not proof that it finished. A verifier
checks the result against the acceptance criteria.

CMD then stores the useful evidence: the output artifact, provider response,
validation result, timestamps, and relevant source references. This is the
receipt a human can inspect later.

### 13. Replay and evaluation

CMD can replay the classification, context, policy, and compilation stages
without calling providers or changing live state. This allows us to test
whether a new rule would queue, approve, or block real examples before turning
it on.

Replay is how CMD improves safely: evaluate the control decision first, then
enable execution once the behavior is understood.

## Why These Pieces Are Separate

The separation protects against different classes of failure:

- **Wrong operation:** classification catches that a draft was mistaken for a
  send.
- **Wrong target:** context resolution catches the wrong person, company, or
  record.
- **Too much authority:** capabilities and policy limit the effect.
- **Unclear expectations:** `JobSpec` defines what success means.
- **Unrecoverable execution:** the state ledger and worker lifecycle make jobs
  resumable and inspectable.
- **False completion:** verification and receipts check the result.
- **Unsafe changes:** replay tests decisions without live side effects.

Putting all of this into one prompt or one autonomous loop makes it difficult
to tell which part failed and difficult to correct it without changing
everything else.

## What Happens When Something Is Ambiguous?

CMD should prefer a clear question or a blocked job over a confident guess when
the ambiguity could change the external effect.

Examples:

- two people match the name;
- a company has changed its domain;
- a source memo conflicts with a canonical database record;
- the target record cannot be found;
- the requested operation exceeds the granted capability; or
- the source context has changed since the job was compiled.

The human can correct the context or revise the job. That correction becomes
part of the job history.

## What Is Local and What Leaves the Machine?

CMD is designed around local control. Task state, job state, routing decisions,
logs, and receipts belong to the local system. An external model or service is
used only where the selected workflow requires it.

The exact data sent to a model or provider depends on the adapter and job. The
system should make that boundary visible rather than treating every tool call
as an invisible side effect.

## Current Product Status

The local Outcome, Work Thread, queue, artifact, blocker, and approval-preview
model is shipped in the fictional alpha workspace. Codex and Claude public
adapters can run one synthetic bounded job and return a typed receipt.

The full architecture described in this guide is not yet one live execution
chain. The public worker does not currently receive a compiled `JobSpec`,
capability lease, brokered connector, or independent verifier. Authorized
source paths are recorded consent but are not automatically mounted into the
worker. Live connector setup is not newcomer-ready.

Read the maintained [`public alpha status`](STATUS.md) for a capability-by-
capability matrix and executable proof. When this guide and that matrix differ,
the status matrix governs claims about what is shipped.

## Short Glossary

- **Action:** The captured request or proposed effect.
- **Context:** The people, companies, tasks, records, documents, and policies
  that give an action meaning.
- **ContextRef:** A structured reference to one resolved context object,
  including its locator and provenance.
- **Capability:** A named effect CMD knows how to authorize.
- **JobSpec:** The complete execution contract for one job.
- **Worker:** An agent-backed process that performs a `JobSpec`.
- **Broker:** The controlled boundary between a worker and an external service.
- **Artifact:** A file, draft, record, or other output produced by a job.
- **Receipt:** Evidence that records what the system attempted and what was
  verified.
- **Replay:** A no-side-effect evaluation of the control decision for an action.
