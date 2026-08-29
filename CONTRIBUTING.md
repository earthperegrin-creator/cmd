# Contributing

CMD is preparing for a public alpha. Small, focused changes with tests are most
useful.

1. Keep product behavior generic and local-first.
2. Put personal context and live state outside the repository.
3. Preserve exact approval for consequential external effects.
4. Run `python3 -m unittest discover -s . -p 'test_*.py'`.
5. Explain the user-visible outcome and any trust-boundary change in the pull
   request.

Do not include real emails, contacts, companies, credentials, absolute home
paths, private transcripts, or copied user fixtures in tests or screenshots.
