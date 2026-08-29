# Security policy

CMD is an alpha local-first application. Report suspected vulnerabilities
privately to the repository maintainer before opening a public issue containing
credentials, personal data, or an exploit.

CMD binds to localhost by default. Non-local serving requires an authentication
token. GET and HEAD use explicit route allowlists; the product does not expose
its repository root as a static file tree. Host and mutation Origin validation
remain active even when non-local authentication is configured. Connectors and
background workers are disabled for fresh installations.
External sends, posts, calendar changes, deletion, purchases, and similar
effects require approval of the exact payload.

Never commit `.cmd/`, `.env`, databases, OAuth tokens, private profiles, logs,
or backups. If a secret is committed, revoke it immediately; removing the file
from the latest revision is not sufficient.

Supported alpha versions receive security fixes on a best-effort basis. There
is currently no telemetry and no hosted CMD service.
