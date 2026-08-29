# Publication boundary

CMD has one generic product codebase and a separate private user layer.

The public product contains application and runtime code, schemas, sanitized
fixtures, generic setup and security documentation, tests, and release
artifacts. The private layer contains the person's profile, outcomes, local
database, credentials, private skills, connector configuration, logs, and
backups.

Private context is mounted only through `CMD_CONTEXT_DIR`. Fresh state is stored
outside the checkout. The product must boot, demonstrate its core loop, and run
tests without private context.

The current dogfood repository and its history are not the public artifact. A
reviewed clean snapshot will become the canonical public repository.
