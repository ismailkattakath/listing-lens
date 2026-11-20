"""`llens grafana <subcommand>`: the private boards' commands in the one CLI (it was `llens-grafana`).

ONE parser definition (`add_subcommands`) serves both spellings, so `llens-grafana <sub> --help` and
`llens grafana <sub> --help` differ only in the prog name, and both reach the same handler:

- `check`        the hard rules over every built board (offline);
- `build [uid]`  the generated board JSON (offline);
- `tf plan|apply` the OpenTofu stack (tf.py); `plan` and `apply` are short for `tf plan` and `tf apply`;
- `perms`        the folder + datasource RBAC grants (apply.py);
- `ds-password`  the datasource password, from LLENS_PG_DSN (apply.py);
- `bootstrap`    one-time, operator: the boards' service account and its Keychain token (apply.py).

The networked ones (everything that calls Grafana's API) are `paid` in pipeline.toml, so the guard blocks
them unless the operator approved. `llens grafana` also gates them in code (`--approve-network`, as `db
check`). The old `llens-grafana` keeps its old gate, `secret exec` in the flake apps, and no new flag: it
prints a one-line deprecation note to stderr and runs the same handler. It goes after one release.

Nothing at import time touches the boards or the site file: `register` runs on every `llens` call,
including `cli spec` in a sandbox with no site file, so each handler imports what it needs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from scrape_harness.cli import approve_network_arg, need_network

LEGACY = "llens-grafana"
NEW = "llens grafana"
SUBCOMMANDS = ("check", "build", "tf", "plan", "apply", "perms", "ds-password", "bootstrap")
# The old parser looked for these words ANYWHERE in argv, in this order (apply.py before the fold).
LEGACY_WORDS = ("--check", "check", "bootstrap", "ds-password", "perms")
METAVAR = "{" + ",".join(SUBCOMMANDS) + "}"
USAGE = f"usage: {LEGACY} --check | build [uid] | tf plan|apply | perms | ds-password | bootstrap"


class _GrafanaApi:
    network = True  # need_network's plugin: Grafana's HTTP API


def _clean() -> tuple[list[dict], int | None]:
    """The hard rules over every built board: (boards, 2) and the violations when one is broken."""
    from . import boards, model

    built = boards.build_all()
    errs = [e for b in built for e in model.check(b)]
    if errs:
        print(f"{LEGACY}: refusing, hard-rule violations:\n  " + "\n  ".join(errs), file=sys.stderr)
        return built, 2
    return built, None


def _gate(args: argparse.Namespace, what: str) -> None:
    if args.gated:  # `llens grafana` only: the old spelling's gate stays `secret exec`, as before the fold
        need_network(args, _GrafanaApi(), f"grafana {what}")


def _api(args: argparse.Namespace, what: str):
    """The checks every API subcommand ran first, in the old order: hard rules, the gate, then the env.
    Returns (Grafana client, None) or (None, exit code)."""
    from . import apply

    if (rc := _clean()[1]) is not None:
        return None, rc
    _gate(args, what)
    url, token = os.environ.get("LLENS_GRAFANA_URL"), os.environ.get("GRAFANA_TOKEN")
    if not url or not token:
        print(f"{LEGACY}: LLENS_GRAFANA_URL and GRAFANA_TOKEN are required", file=sys.stderr)
        return None, 2
    return apply.Grafana(url, token), None


# ── handlers ─────────────────────────────────────────────────────────────────────────────────────


def cmd_check(_: argparse.Namespace) -> int:
    built, rc = _clean()
    if rc is not None:
        return rc
    print(f"{len(built)} boards clean")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    from . import boards

    for b in boards.build_all():
        if args.uid is None or b["uid"] == args.uid:
            print(json.dumps(b, indent=1))
    return 0


def cmd_tf(args: argparse.Namespace) -> int:
    from . import tf

    _gate(args, "tf")
    return tf.main([args.tf_action])  # tf.main runs its own hard-rule check, lock and state guards


def cmd_perms(args: argparse.Namespace) -> int:
    from . import apply

    g, rc = _api(args, "perms")
    if g is None:
        return rc
    apply.folder(g)
    apply.datasource_grants(g)
    return 0


def cmd_ds_password(args: argparse.Namespace) -> int:
    from . import apply

    g, rc = _api(args, "ds-password")
    if g is None:
        return rc
    dsn = os.environ.get("LLENS_PG_DSN")
    if not dsn:
        print(f"{LEGACY} ds-password: LLENS_PG_DSN is required", file=sys.stderr)
        return 2
    apply.ds_password(g, dsn)
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    from . import apply

    g, rc = _api(args, "bootstrap")
    if g is None:
        return rc
    apply.bootstrap(g, os.environ.get("LLENS_GRAFANA_TOKEN_ITEM", "llens:grafana:token"))
    return 0


# ── the ONE parser definition ────────────────────────────────────────────────────────────────────

TF_HELP = "the OpenTofu stack (folder, datasource, boards); state under DATA tofu/ (networked)"


def _tf_common(sp: argparse.ArgumentParser, gated: bool) -> None:
    approve_network_arg(sp)
    sp.set_defaults(fn=cmd_tf, gated=gated)


def add_subcommands(gs: argparse._SubParsersAction, gated: bool) -> None:  # noqa: SLF001
    """Every grafana subcommand, for both spellings: `gated` is True under `llens grafana` only."""
    c = gs.add_parser("check", help="the hard rules over every built board (offline)")
    c.set_defaults(fn=cmd_check, gated=gated)

    b = gs.add_parser("build", help="print the generated board JSON, all boards or one (offline)")
    b.add_argument("uid", nargs="?", help="only the board with this uid")
    b.set_defaults(fn=cmd_build, gated=gated)

    t = gs.add_parser("tf", help=TF_HELP)
    t.add_argument("tf_action", choices=("plan", "apply"), help="plan, then apply that exact plan")
    _tf_common(t, gated)
    for action in ("plan", "apply"):
        a = gs.add_parser(action, help=f"short for `tf {action}`: {TF_HELP}")
        _tf_common(a, gated)
        a.set_defaults(tf_action=action)

    p = gs.add_parser("perms", help="assert the folder + datasource RBAC grants (networked)")
    approve_network_arg(p)
    p.set_defaults(fn=cmd_perms, gated=gated)

    d = gs.add_parser(
        "ds-password", help="set the datasource password from LLENS_PG_DSN; never printed (networked)"
    )
    approve_network_arg(d)
    d.set_defaults(fn=cmd_ds_password, gated=gated)

    s = gs.add_parser(
        "bootstrap",
        help="one-time, operator: the boards' service account; its token into the Keychain (networked)",
    )
    approve_network_arg(s)
    s.set_defaults(fn=cmd_bootstrap, gated=gated)


def register(sub) -> None:
    """[plugins].commands entry (via commands.register): the `llens grafana` group."""
    g = sub.add_parser("grafana", help="the private Grafana boards: check, build, tf plan|apply, perms, ...")
    add_subcommands(g.add_subparsers(dest="grafana_command", required=True, metavar=METAVAR), True)


def legacy_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=LEGACY, description=f"deprecated: the same as `{NEW}`")
    add_subcommands(p.add_subparsers(dest="grafana_command", required=True, metavar=METAVAR), False)
    return p


# ── the old spelling ─────────────────────────────────────────────────────────────────────────────


def legacy_argv(args: list[str]) -> list[str]:
    """The old grammar onto the new: `tf ...` and `build ...` as they were; otherwise the word `apply` is
    dropped (it was a no-op word) and the first of LEGACY_WORDS found anywhere wins, as it did."""
    if args[:1] in (["tf"], ["build"]):
        return list(args)
    rest = [a for a in args if a != "apply"]
    helps = [a for a in rest if a in ("-h", "--help")]
    for w in LEGACY_WORDS:
        if w in rest:
            return [w.lstrip("-"), *helps]
    return helps  # no old word: top-level help, else the old usage error (never `tf` or `plan` by accident)


def legacy_main(args: list[str], note: bool = True) -> int:
    argv = legacy_argv(args)
    if note:
        words = " ".join(a for a in argv if not a.startswith("-")) or "<subcommand>"
        print(
            f"{LEGACY} is deprecated: use `{NEW} {words}` (same subcommands and arguments)", file=sys.stderr
        )
    if not argv:  # no old word: the old usage error
        print(USAGE, file=sys.stderr)
        return 2
    ns = legacy_parser().parse_args(argv)
    return ns.fn(ns)


def legacy() -> None:
    """The `llens-grafana` console script (pyproject), kept for one release."""
    raise SystemExit(legacy_main(sys.argv[1:]))
