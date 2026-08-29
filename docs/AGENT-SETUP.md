# Agent-native setup

This is the canonical setup contract for Codex and Claude Code. The goal is a
short conversation followed by a deterministic local installation.

## Ask exactly three questions

1. **Sources:** “Where may CMD learn about you? I will read only the paths or
   agent configuration you explicitly grant.” `none` is a valid answer.
2. **Profile confirmation:** After reading only granted sources, propose one
   short sentence describing the person and ask: “What should CMD call you,
   and is this summary right?” Allow one correction and read the final sentence
   back once.
3. **Outcomes:** “What outcomes matter in the next 90 days?” Accept a short
   list. These become the initial root Outcomes.

Do not turn setup into a questionnaire. Categories and generic registries have
safe defaults and remain editable later.

## Initialize

Save the confirmed answers to a temporary JSON file outside the repository:

```json
{
  "first_name": "Nick",
  "summary": "Builds products with coding agents.",
  "authorized_sources": ["~/notes"],
  "outcomes_90_days": ["Ship the first product alpha", "Interview ten users"]
}
```

Then run:

```sh
./cmd setup --profile-json /path/to/confirmed-profile.json --agent codex
```

Use `--agent claude` for Claude Code or `--agent none` when the user wants the
interface without background execution. Fresh connectors remain disabled.

Successful setup must report the private state directory, profile, database,
initial outcome count, and browser URL. If setup fails, report the failed check
and keep the confirmed profile so the user does not need to answer again.

## Verify

Run `./cmd doctor`. Setup is complete when the private state, confirmed profile,
database, and local browser are healthy. The selected agent CLI may be absent
when `--agent none` was chosen.
