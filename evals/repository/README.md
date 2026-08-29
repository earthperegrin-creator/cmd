# Repository experience evaluation

This evaluation tests whether CMD is legible and useful to both sides of an
agent-native product:

1. a busy person deciding whether CMD fits her life; and
2. the AI agent she asks to find, assess, and set up a solution.

The two personas and scoring dimensions are frozen. Every repository-facing
improvement round uses the same prompts, starts from the current checkout, and
scores from scratch. Evaluators judge only what exists. They must ignore prior
reports so earlier scores do not become anchors.

## Protocol

1. Record the commit or working-tree state under evaluation.
2. Give each evaluator only its persona file and the current public repository.
3. Ask the evaluator to inspect the newcomer surface named in its persona.
4. Record all seven scores, the total, likely next action, blockers, and candid
   quote without smoothing criticism.
5. Make one coherent improvement pass.
6. Repeat with the same evaluators and rubric.

Build a report-blind packet with:

```bash
python3 scripts/build_repository_eval_packet.py human
python3 scripts/build_repository_eval_packet.py agent
```

The packet records the commit and working-tree state, freezes the relevant
persona, includes the intended newcomer surfaces, and excludes every prior
score report.

A score is evidence, not a release gate. Runtime tests, public-boundary audits,
and security canaries remain separate release requirements.

## Score interpretation

- `0`: absent or actively misleading
- `1`: barely present
- `2`: understandable only with substantial caution or prior context
- `3`: credible but incomplete
- `4`: clear and convincing
- `5`: unusually strong and immediately actionable

## Files

- [`personas/fractional-cmo.md`](personas/fractional-cmo.md): the human buyer and user
- [`personas/chatgpt-scout.md`](personas/chatgpt-scout.md): the agent searching on her behalf
- [`reports/`](reports/): one immutable report per round
