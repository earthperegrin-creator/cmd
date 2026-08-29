# Setting up CMD

When the user says “Set up CMD for me,” follow
[`docs/AGENT-SETUP.md`](docs/AGENT-SETUP.md). Do not inspect personal files,
adjacent repositories, credentials, or existing agent configuration until the
user grants those sources in question 1.

Ask exactly three onboarding questions. Confirm one concise readback, accept a
correction if needed, then initialize CMD through `./cmd setup`. Never enable a
connector or external write merely because credentials are present. External
sends, posts, calendar writes, deletion, purchases, and other consequential
effects require approval of the exact payload.

For development, run `python3 -m unittest discover -s . -p 'test_*.py'` before
claiming a release-facing change is complete. Private state belongs outside the
repository; use `CMD_STATE_DIR` for test and user installations.
