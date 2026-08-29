---
name: CMD Classic
description: The shipped visual system for CMD's local command workspace.
version: 1.0.0
status: active
---

# CMD Classic

## Overview

**Creative North Star: The Precision Field Desk.** CMD should feel like a calm,
well-made operations desk for someone carrying many simultaneous contexts. It
is dense because the work is dense, but hierarchy, status, and authority stay
legible. The interface is single-player, local-first, and operational. It does
not perform intelligence through decorative complexity.

The shipped Classic alpha uses a light technical-grid surface with flat white
panels, clipped-corner records, dark rules, and restrained status color. Arctic
Command may be used for explanatory concept art, but it is not the source of
truth for the current product tokens.

## Colors

| Token | Value | Use |
|---|---:|---|
| Paper | `#f5f7fa` | Main workspace and technical grid |
| Paper 2 | `#eef1f5` | Secondary workspace bands |
| Panel | `#ffffff` | Primary records and surfaces |
| Panel 2 | `#f7f8fa` | Recessed and selected regions |
| Ink | `#17191e` | Primary text, rules, decisive controls |
| Ink 2 | `#3e434c` | Secondary text |
| Muted | `#747b86` | Metadata and supporting labels |
| Line | `#e0e3e8` | Quiet dividers |
| Line 2 | `#c9ced6` | Structural boundaries |
| Signal | `#d96a19` | Approval and human-attention boundary |
| Signal soft | `#fff1e6` | Approval background |
| Blue | `#3159c9` | Active work, links, and current selection |
| Danger | `#b33b2f` | Blocked, stale, and destructive states |
| Violet | `#7356a8` | Writing category only |

Color must carry a stable meaning and must be paired with a label, icon, or
shape. Orange never means generic excitement. Red never means mere priority.

## Typography

- **Saira Condensed, 600** for Outcome titles, panel headings, queue cards, and
  other compact operational headlines.
- **Archivo, 400 to 700** for interface copy, descriptions, controls, and
  longer reading.
- **JetBrains Mono, 400 to 700** for status labels, IDs, timestamps, receipts,
  structured tokens, and compact system metadata.

Use sentence case for human language. Reserve uppercase mono labels for terse
machine states. Keep body copy at a comfortable line height even when the
surrounding layout is dense.

## Elevation

The default system is flat. Structure comes from one-pixel rules, tonal shifts,
spacing, and clipped corners rather than stacked shadows. Cards do not float.
Hover states change border or background color without moving the object.

The Work Thread side panel may use one restrained directional shadow because it
physically overlays the workspace. No other routine component should create a
new elevation tier.

## Components

- **Outcome record:** white or quiet-gray surface, one-pixel border, narrow
  category spine, clear title, real completion condition, latest result, and
  one level of Next moves.
- **Agent receipt:** state label plus summary, artifact, blocker, heartbeat, or
  exact proposed operation. A completed state needs evidence.
- **Approval preview:** signal-orange boundary that shows capability, risk,
  execution mode, and exact payload before a consequential effect.
- **Work Thread:** chronological human and agent turns attached to one Outcome,
  with handoff controls kept visually separate from the transcript.
- **Primary action:** dark ink fill. Use one per local decision region.
- **Status treatment:** working is blue, approval is orange, blocked is red,
  complete is ink. Always include plain-language text.
- **Repository visual:** diagrams should explain record once, bounded work,
  review, and exact-effect approval. Product screenshots must be labeled as
  actual; skin explorations must be labeled as concept art.

All interactive controls need visible keyboard focus, at least a 44-pixel touch
target where practical, and a reduced-motion equivalent.

## Do’s and Don’ts

**Do** keep the instruction, Outcome, work state, artifact, and approval lineage
visually connected. Show what happened, what evidence exists, and what decision
the person is making. Prefer concrete fictional examples over abstract feature
claims. Make failure and missing context explicit.

**Don’t** turn CMD into a flat to-do list, a generic chat shell, an autonomous
company org chart, or a team administration suite. Avoid purple-gradient AI
cliches, glass panels, decorative metric cards, vague activity, productivity
streaks, and progress bars without an observable completion condition. Never
use marketing abstraction to imply that an alpha capability is already wired
end to end.
