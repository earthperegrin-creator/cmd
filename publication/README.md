# CMD

## Record it once. Let an agent work. Review what came back.

CMD is Google Tasks for the agent age: a local-first command center where a
task is not merely stored. It is bound to an Outcome, handed to an agent with
clear limits, and returned as a result you can inspect.

If your life contains five client workspaces, a few advisory relationships,
three side projects, family logistics, and several AI agents, CMD is designed
for the part that usually breaks: keeping the work, context, decisions, and
results connected.

```text
You record one instruction
        ↓
CMD binds it to the right Outcome and gives an agent bounded context
        ↓
You receive a result, an exact approval request, or an honest blocker
```

CMD does not ask you to maintain a second story about your work. You should not
have to write a to-do, do it somewhere else, then return and tell the to-do app
what happened.

## A concrete example

Maya, the fictional fractional CMO in the demo, records:

> Compare the three launch regions for Alder Health. Recommend one, show the
> evidence and tradeoff, and do not send anything.

CMD keeps that instruction inside the Alder Health Outcome. An agent does the
bounded research and returns a recommendation with its reasoning. If Maya later
asks to email the client, CMD can prepare the exact message, but sending remains
a separate approval.

That distinction is the product: useful autonomy inside clear boundaries.

## Who CMD is for

- People already using Codex, Claude Code, or another capable agent every day.
- Consultants, founders, investors, researchers, writers, and operators whose
  work crosses projects and domains.
- People who want agents to perform low-risk work automatically and bring back
  decisions, drafts, artifacts, or evidence.
- People who prefer local task truth and explicit control over external effects.

CMD is not a team project manager, a replacement for every source application,
or an autonomous company in a box. It is a single-player control surface for
one person and the agents working on that person's behalf.

## Try the fictional workspace in about a minute

CMD requires Python 3.11 or newer. The local core uses only the standard
library.

```bash
git clone <repository-url>
cd cmd
python3 scripts/demo_workspace.py serve
```

Open `http://127.0.0.1:8765/`.

The demo contains only synthetic data. Maya Chen is a fictional fractional CMO
managing startup clients, a consulting practice, a writing project, and family
plans. Her workspace shows four important states:

- an agent result ready for review;
- an outbound email waiting for exact approval;
- a worker currently in progress; and
- an honest blocker caused by missing source material.

Every company, person, metric, address, URL, and artifact is fictional. Demo
state stays isolated in `.cmd-demo/` and cannot touch a normal CMD workspace.

Reset it at any time:

```bash
python3 scripts/demo_workspace.py reset
```

## Ask your agent to assess it first

Paste this into Codex, Claude Code, or ChatGPT:

```text
Evaluate the CMD repository I shared as a way to keep my work organized while
AI agents do more of it. Read the README and AGENTS.md, clone it into a new
folder, and run only the fictional demo. Do not inspect my personal files,
import my data, enable connectors, or make external changes. Show me what CMD
does today, what remains alpha, and whether it fits my workflow.
```

## Set up a private workspace

The agent-native setup asks only three questions: which sources CMD may read,
how to describe you, and which Outcomes matter in the next 90 days. It must not
inspect a path you did not grant or enable a connector merely because credentials
exist.

```bash
./cmd setup
./cmd doctor
```

Private profiles, tasks, receipts, and credentials live outside the Git
checkout by default. Fresh workers and connectors are disabled. See
[agent-native setup](docs/AGENT-SETUP.md) or the [human guide](docs/HUMAN-GUIDE.md).

## What the public alpha proves today

The repository currently ships:

- a local browser workspace with Outcomes, one level of Next moves, agent queue
  states, Work Threads, artifacts, and approval previews;
- private local state backed by SQLite;
- an isolated, resettable fictional consultant demo;
- public Codex and Claude worker adapters that return typed receipts;
- versioned schemas and registries for work, capabilities, and resolution; and
- lifecycle, public-boundary, worker, and server-security canaries.

The full architecture is ahead of the shipped execution path. In particular,
the public worker does not yet prove the complete JobSpec, capability lease,
broker, connector, and independent-verification chain against real accounts.
Connectors remain disabled by default, and their setup is not yet a polished
alpha workflow. Treat CMD as an inspectable early product, not finished
infrastructure.

## The control model

CMD separates responsibilities deliberately:

- **The agent** reasons, researches, drafts, uses allowed tools, and produces
  artifacts.
- **CMD** owns Outcome identity, job boundaries, lifecycle state, approval,
  verification, and history.
- **The human** decides when an exact external effect should occur.

A prepared action is not the same as a committed effect. A model saying “done”
is not the same as a verified result. When context or authority is weak, CMD is
designed to ask or block instead of guessing.

## Pick the shortest useful document

| You are... | Start here |
|---|---|
| Trying to understand the product | [Human guide](docs/HUMAN-GUIDE.md) |
| An agent setting CMD up | [Agent setup](docs/AGENT-SETUP.md) |
| An agent operating with CMD | [Agent integration](docs/AGENT-INTEGRATION.md) |
| Reviewing the system boundary | [Architecture](docs/ARCHITECTURE.md) |
| Checking local configuration | [Configuration](docs/CONFIGURATION.md) |
| Auditing risk and trust | [Security](SECURITY.md) |

## Validate the public boundary

```bash
python3 scripts/audit_public_foundation.py
python3 -m unittest test_public_alpha.py
python3 scripts/run_lifecycle_canary.py
python3 scripts/run_server_security_canary.py
```

Maintainers can also build and independently inspect the manifest-bounded
public snapshot. Building a snapshot does not publish it.

```bash
python3 scripts/build_public_snapshot.py /tmp/cmd-public
python3 scripts/validate_public_snapshot.py /tmp/cmd-public
```

## License

CMD is licensed under the [Apache License 2.0](LICENSE).
