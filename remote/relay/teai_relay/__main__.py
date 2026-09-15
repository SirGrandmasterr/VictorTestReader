"""Command line: ``python -m teai_relay`` (serve), ``keygen`` or ``keys list|add|revoke``."""

import os
import sys

from .auth import KeyStore, KeyStoreError, generate_key
from .config import DEFAULT_KEYS_FILE, ConfigError, RelayConfig, parse_keys

KEY_WARNING = (
    "Keep this key secret: it is shown only once and cannot be recovered, only revoked.\n"
    "A running relay does not re-read the file; add keys through the admin API "
    "(POST /admin/keys) while it runs, or restart it."
)

USAGE = (
    "Usage:\n"
    "  python -m teai_relay                       start the relay (configured via environment)\n"
    "  python -m teai_relay keygen [n]            print n new random API keys\n"
    "  python -m teai_relay keys list             list key names in RELAY_KEYS_FILE and the environment\n"
    "  python -m teai_relay keys add <name> [--agent]   create a key in RELAY_KEYS_FILE (client unless --agent)\n"
    "  python -m teai_relay keys revoke <name>    revoke a key (file keys are removed, env keys blocked)\n"
)


def _store_from_environ(environ):
    """The key store the ``keys`` command edits: RELAY_KEYS_FILE plus the environment keys for listing."""
    path = environ.get("RELAY_KEYS_FILE", "").strip() or DEFAULT_KEYS_FILE
    try:
        agent_keys = parse_keys(environ.get("RELAY_AGENT_KEYS"), "agent")
        client_keys = parse_keys(environ.get("RELAY_CLIENT_KEYS"), "client")
    except ConfigError as exc:
        print("Warning: {0}; ignoring the environment keys".format(exc), file=sys.stderr)
        agent_keys, client_keys = {}, {}
    store = KeyStore(agent_keys, client_keys, path)
    if store.load_error:
        print("Warning: {0}".format(store.load_error), file=sys.stderr)
    return store


def keys_command(argv, environ=None, out=None):
    """Implement ``keys list|add|revoke``; returns the exit status."""
    environ = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    action = argv[0] if argv else "list"
    store = _store_from_environ(environ)
    if action == "list":
        rows = store.list()
        if not rows:
            print("(no keys)", file=out)
        for row in rows:
            print("{kind:<7}{name:<24}{source:<6}{created}".format(**row), file=out)
        return 0
    if action == "add":
        if len(argv) < 2:
            print(USAGE, file=sys.stderr)
            return 2
        kind = "agent" if "--agent" in argv[2:] else "client"
        try:
            key = store.add(argv[1], kind)
        except KeyStoreError as exc:
            print("Error: {0}".format(exc), file=sys.stderr)
            return 1
        except OSError as exc:
            print("Error: the keys file could not be written: {0}".format(exc), file=sys.stderr)
            return 1
        print(key, file=out)
        print(KEY_WARNING, file=sys.stderr)
        return 0
    if action == "revoke":
        if len(argv) < 2:
            print(USAGE, file=sys.stderr)
            return 2
        try:
            kind = store.revoke(argv[1])
        except KeyStoreError as exc:
            print("Error: {0}".format(exc), file=sys.stderr)
            return 1
        except OSError as exc:
            print("Error: the keys file could not be written: {0}".format(exc), file=sys.stderr)
            return 1
        if kind is None:
            print("Error: no key named '{0}'".format(argv[1]), file=sys.stderr)
            return 1
        print("revoked {0} key '{1}'".format(kind, argv[1]), file=out)
        return 0
    print(USAGE, file=sys.stderr)
    return 2


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("keygen", "key"):
        count = int(argv[1]) if len(argv) > 1 else 1
        for _ in range(max(1, count)):
            print(generate_key())
        return 0
    if argv and argv[0] == "keys":
        return keys_command(argv[1:])
    if argv and argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    try:
        config = RelayConfig.from_environ()
    except ConfigError as exc:
        print("Configuration error: {0}".format(exc), file=sys.stderr)
        return 2
    from .server import run

    try:
        run(config)
    except ConfigError as exc:
        print("Configuration error: {0}".format(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
