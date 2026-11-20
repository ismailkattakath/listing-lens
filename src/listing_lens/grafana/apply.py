"""What stays OUTSIDE the OpenTofu stack (tf.py owns the folder, the dashboards and the datasource):
- `perms`        folder + datasource RBAC grants, asserted to be exactly what is intended;
- `ds-password`  (re)set the datasource password on the EXISTING datasource — the stack never holds it;
- `bootstrap`    one-time: the li service account (org-admin token), its token into the Keychain.
Plus `check` (the hard rules), `build [uid]` (the generated JSON) and `tf plan|apply`. The parser and
the handlers live in cli.py: `llens grafana <subcommand>` (the old `llens-grafana` is deprecated).

Everything personal arrives through the environment, never the repo:
- LLENS_GRAFANA_URL   the stack URL (plain env);
- GRAFANA_TOKEN    a stack service-account token (secret exec, from the Keychain);
- LLENS_PG_DSN        the read-only Postgres URI (secret exec, ds-password only). Its password goes only
                   into the request BODY (urllib, never argv), and nothing here prints it.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from . import model


class Grafana:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self._h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def req(self, method: str, path: str, body=None, ok=(200,)):
        data = None if body is None else json.dumps(body).encode()
        r = urllib.request.Request(self.url + path, data=data, method=method, headers=self._h)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        if status not in ok:
            # the body is Grafana's error message; it never echoes secureJsonData
            raise SystemExit(
                f"llens-grafana: {method} {path} -> {status}: {raw[:300].decode(errors='replace')}"
            )
        return status, (json.loads(raw) if raw else None)


def folder(g: Grafana) -> None:
    uid = model.FOLDER_UID  # the folder itself is the tofu stack's; here only its grants
    # Viewer/Editor lose their default grants (""), Admins keep the folder, and so does the li SA
    # itself (or it would lock itself out). The RBAC endpoint, not the legacy one: legacy
    # /api/folders/{uid}/permissions 500s on a service-account grant (measured 2026-11-08).
    _, me = g.req("GET", "/api/user")
    me = me or {}
    base = f"/api/access-control/folders/{uid}"
    for role, perm in (("Viewer", ""), ("Editor", ""), ("Admin", "Admin")):
        g.req("POST", f"{base}/builtInRoles/{role}", {"permission": perm})
    # a service-account token reports id 0; its real user id is in uid "service-account:<id>"
    my_id = int(str(me.get("uid", "")).rpartition(":")[2] or 0) or me["id"]
    g.req("POST", f"{base}/users/{my_id}", {"permission": "Admin"})
    _, perms = g.req("GET", f"/api/access-control/folders/{uid}")
    grants = sorted(
        f"{p.get('builtInRole') or p.get('userLogin') or p.get('teamUid') or '?'}:{p.get('permission')}"
        for p in perms or []
        if not p.get("isInherited")
    )
    expect = sorted(["Admin:Admin", f"{me['login']}:Admin"])
    if grants != expect:
        raise SystemExit(f"llens-grafana: folder {uid} grants are {grants}, expected {expect}")
    print(f"folder {uid}: {', '.join(grants)} — nothing else")


def direct_host(host: str) -> str:
    """Neon's pooled endpoint (PgBouncer, transaction mode) ignores per-role settings, so the
    role's search_path=li never applies there (measured 2026-11-08: pooled gives "$user", public;
    direct gives li). Grafana opens at most 2 connections, far under the direct limit."""
    first, dot, rest = host.partition(".")
    return first.removesuffix("-pooler") + dot + rest


def datasource_grants(g: Grafana) -> None:
    uid = model.DS_UID  # the datasource itself is the tofu stack's; here only its grants
    # Viewer/Editor lose the default Query grant; the li SA keeps Admin on its own datasource
    _, me = g.req("GET", "/api/user")
    my_id = int(str((me or {}).get("uid", "")).rpartition(":")[2] or 0)
    base = f"/api/access-control/datasources/{uid}"
    g.req("POST", f"{base}/users/{my_id}", {"permission": "Admin"})
    for role in ("Viewer", "Editor"):
        g.req("POST", f"{base}/builtInRoles/{role}", {"permission": ""})
    _, perms = g.req("GET", base)
    grants = sorted(
        f"{p.get('builtInRole') or p.get('userLogin')}:{p.get('permission')}" for p in perms or []
    )
    print(f"datasource {uid} grants: {', '.join(grants)}")
    _, health = g.req("GET", f"/api/datasources/uid/{uid}/health", ok=(200, 400, 500))
    print(f"datasource {uid}: {(health or {}).get('status')} {(health or {}).get('message', '')[:120]}")


def ds_password(g: Grafana, dsn: str) -> None:
    """Set ONLY the password on the existing datasource: GET its model, PUT it back with
    secureJsonData. The tofu stack ignores secure_json_data_encoded, so this is the one writer."""
    uid = model.DS_UID
    _, cur = g.req("GET", f"/api/datasources/uid/{uid}")
    body = {k: v for k, v in (cur or {}).items() if k not in ("id", "version", "secureJsonFields")}
    body["secureJsonData"] = {"password": urllib.parse.unquote(urllib.parse.urlsplit(dsn).password or "")}
    g.req("PUT", f"/api/datasources/uid/{uid}", body)
    print(f"datasource {uid}: password set (value not shown)")
    datasource_grants(g)


SA_NAME = os.environ.get(
    "LLENS_GRAFANA_SA_NAME", "llens-boards"
)  # the stack's service account (live name bound outside the repo)
SA_FIXED_ROLES = ["fixed:datasources:creator", "fixed:folders:creator"]


def bootstrap(g: Grafana, keychain_item: str) -> None:
    """ONE-TIME, operator, with an org-admin token: the li service account
    with the least privilege apply needs — basic role None, create-only on datasources and root
    folders (a creator owns only what it creates, so the stack's other datasources stay out of
    reach), and Admin on the li folder alone. A fresh token goes straight into the Keychain via
    stdin; it is never printed."""
    _, found = g.req("GET", f"/api/serviceaccounts/search?query={SA_NAME}")
    sa = next((a for a in (found or {}).get("serviceAccounts", []) if a["name"] == SA_NAME), None)
    if sa is None:
        _, sa = g.req("POST", "/api/serviceaccounts", {"name": SA_NAME, "role": "None"}, ok=(200, 201))
    sa_id = sa["id"]
    _, roles = g.req("GET", "/api/access-control/roles")
    uids = {r["name"]: r["uid"] for r in roles or []}
    for name in SA_FIXED_ROLES:
        g.req("POST", f"/api/access-control/users/{sa_id}/roles", {"roleUid": uids[name]}, ok=(200, 409))
    status, _ = g.req("GET", f"/api/folders/{model.FOLDER_UID}", ok=(200, 404))
    if status == 404:
        g.req("POST", "/api/folders", {"uid": model.FOLDER_UID, "title": model.FOLDER_TITLE})
    g.req(
        "POST",
        f"/api/access-control/folders/{model.FOLDER_UID}/users/{sa_id}",
        {"permission": "Admin"},
    )
    _, tok = g.req("POST", f"/api/serviceaccounts/{sa_id}/tokens", {"name": f"{SA_NAME}-apply"})
    subprocess.run(
        ["secret", "set", "--no-export", keychain_item],
        input=(tok or {})["key"] + "\n",
        text=True,
        check=True,
    )
    print(
        f"bootstrap: SA {SA_NAME} (id {sa_id}, role None + {', '.join(SA_FIXED_ROLES)}, Admin on folder "
        f"{model.FOLDER_UID}); token stored in Keychain item {keychain_item}"
    )


def main(argv: list[str]) -> int:
    """The old in-process entry (argv without the program name): the same handlers as `llens grafana`."""
    from .cli import legacy_main

    return legacy_main(argv, note=False)


def cli() -> None:
    """The `llens-grafana` console script: deprecated for `llens grafana` (grafana/cli.py), one release."""
    from .cli import legacy

    legacy()
