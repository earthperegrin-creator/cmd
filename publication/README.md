# CMD

**Google Tasks for the agent age.**

Record what needs to be done once. CMD sends agents to do the work and brings
back reviewable results.

CMD is a local-first workspace for directing work across multiple outcomes and
agent runs. It binds each instruction to an outcome, gives an agent bounded
context, and returns a reviewable result or an honest blocker.

The product is designed around three separations:

- source code is public-capable while profiles, tasks, receipts, and credentials
  remain in private local state;
- agents reason and produce artifacts while CMD owns identity, policy, approval,
  and lifecycle state; and
- preparing an action is different from committing an external effect.

## Try the fictional workspace

CMD requires Python 3.11 or newer and uses only the standard library for its
local core.

```bash
python3 scripts/demo_workspace.py serve
```

Open `http://127.0.0.1:8765/`. The demo follows Maya Chen, a fictional
independent consultant managing healthcare, climate, software, and
practice-building outcomes. Its companies, messages, addresses, and artifacts
are synthetic, and its state is isolated in `.cmd-demo/`.

Reset the demo with:

```bash
python3 scripts/demo_workspace.py reset
```

## Set up a private workspace

```bash
./cmd setup
./cmd doctor
```

Setup asks for authorized context sources, a confirmed profile, and the
outcomes that matter during the next 90 days. Fresh workers and connectors are
disabled. Private state defaults to the operating system's user-data directory,
outside the Git checkout.

See [agent-native setup](docs/AGENT-SETUP.md), [configuration](docs/CONFIGURATION.md),
and the [human guide](docs/HUMAN-GUIDE.md).

## Validate the product boundary

```bash
python3 scripts/audit_public_foundation.py
python3 -m unittest test_public_alpha.py
python3 scripts/run_lifecycle_canary.py
python3 scripts/run_server_security_canary.py
```

Maintainers can build and independently validate the reviewed public snapshot:

```bash
python3 scripts/build_public_snapshot.py /tmp/cmd-public
python3 scripts/validate_public_snapshot.py /tmp/cmd-public
```

The manifest is an allowlist. Files outside it cannot enter the snapshot by
accident. Building a snapshot does not publish it.

Provider conformance is a separate opt-in because it requires authenticated
local Codex and Claude installations:

```bash
python3 scripts/validate_public_snapshot.py /tmp/cmd-public --with-providers
```

## Security

CMD binds to loopback by default, refuses a non-loopback bind without an
authentication token, validates Host and Origin, and serves only the application
shell and intended API routes. External effects remain approval-gated. See
[SECURITY.md](SECURITY.md) for the trust model and vulnerability reporting.

## License

CMD is licensed under the [Apache License 2.0](LICENSE).
