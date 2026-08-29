# Product

## Register

Product UI. Repository and launch materials use the brand register when they
need to explain the product before the user is inside a task.

## Users

CMD is for an agent-heavy knowledge worker with too many simultaneous contexts:
a consultant serving several clients, a founder with operating and personal
projects, an investor tracking companies and follow-through, or a creative
operator working across research, writing, communication, and administration.

The reference user is comfortable delegating to Codex, Claude, or ChatGPT but
does not want her work scattered across chat histories. She wants agents to do
more, not another system she must continuously reconcile by hand.

## Product purpose

CMD is a local-first command center for personal agent work. A person records
what needs to happen once. CMD binds that instruction to the right Outcome,
gives an agent bounded context and authority, and brings back a reviewable
result, an exact approval request, or an honest blocker.

The product does not ask the user to do the work and then record a to-do, or to
record a to-do, do the work elsewhere, and return to check a box. Capturing the
work should start the work. Verified results should update its history.

## Brand personality

Calm, exacting, operational, and humane. CMD should feel like a trusted command
desk for someone with high context density. It is confident without pretending
certainty and powerful without erasing human authority.

## Design principles

1. **Record once.** The instruction, its context, the work, and the result stay
   connected. Do not create reconciliation chores for the user.
2. **Outcomes over task theater.** Show what the person is trying to change,
   the next bounded move, and the latest meaningful result.
3. **Agent work stays inspectable.** Progress, artifacts, blockers, and receipts
   belong in the Outcome's history, not only in a chat transcript.
4. **Approval sits at the exact-effect boundary.** Research and drafting may run
   safely; sending, posting, deleting, purchasing, and other consequential
   effects require review of the actual payload.
5. **Fail honestly.** When identity, context, authority, or evidence is weak,
   ask or block instead of guessing.

## Anti-references

- A flat to-do list that still makes the user perform and reconcile every step.
- A generic chat window that forgets which Outcome the work belongs to.
- An autonomous-company or multi-agent org chart that makes activity look like
  progress.
- A team collaboration suite with roles, tenants, shared rooms, and admin work.
- A generic AI dashboard with purple gradients, glass panels, vague metrics,
  and decorative cards.
- Productivity gamification, busywork streaks, or progress bars without a real
  completion condition.
- Marketing abstraction that hides what the current alpha can actually do.

## Current alpha boundary

The alpha proves the local Outcome, agent queue, Work Thread, approval, artifact,
and receipt model with an isolated fictional workspace. It includes public
Codex and Claude worker adapters and synthetic canaries. It does not yet prove
the full JobSpec, broker, connector, and independent-verification architecture
end to end for real accounts. Connector setup remains deliberately disabled by
default and is not yet a polished newcomer path.

CMD is relentlessly single-player. Multiplayer coordination, teams, RBAC, and
autonomous organizations are not release goals.

## Accessibility and inclusion

The interface should meet WCAG 2.2 AA contrast, preserve keyboard navigation,
show visible focus states, avoid relying on color alone, and respect reduced
motion. Dense screens are acceptable when hierarchy, spacing, and labels remain
legible. Product language should explain consequences in plain terms without
hiding technical truth.
