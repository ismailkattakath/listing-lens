"""`llens api-token`: a short-lived JWT for the Neon Data API (export-db@26's gated schema api).

Signs in to Neon Auth (Managed Better Auth) as the operator and prints the session's JWT (~15 min), or
copies it with --copy. Where to sign in comes from the site file's [api] table (auth_url, email: DATA,
never the repo). The password reaches it only as $LLENS_API_PASSWORD, set by
`secret exec LLENS_API_PASSWORD=<keychain item> -- llens api-token --approve-network`. The password is never
printed. A failure reports the HTTP status only."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

from scrape_harness.cli import approve_network_arg, need_network

from . import site

PASSWORD_ENV = "LLENS_API_PASSWORD"
EXIT_CONFIG, EXIT_AUTH = 2, 11
ORIGIN = "http://localhost:3000"  # Neon Auth checks Origin; localhost is allowed on the branch


class AuthNetwork:
    """Neon Auth is a network service of our own database project."""

    network = True


def transport():
    """(method, url, body) -> (status, headers), with one cookie jar for the whole sign-in."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(url, method=method, headers={"Origin": ORIGIN})
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with opener.open(req, timeout=30) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            return e.code, {}

    return call


def sign_in(call, auth_url: str, email: str, password: str) -> tuple[str | None, str]:
    """The operator's Neon Auth sign-in -> (the session JWT, "") or (None, why: the HTTP status only).
    ONE path for `api-token` and `db api-check`; `call` is transport() (one cookie jar for both steps)."""
    status, _ = call("POST", auth_url + "/sign-in/email", {"email": email, "password": password})
    if status != 200:
        return None, f"sign-in refused (HTTP {status})"
    status, headers = call("GET", auth_url + "/get-session")
    jwt = headers.get("set-auth-jwt")
    if status != 200 or not jwt:
        return None, f"no JWT in the session (HTTP {status})"
    return jwt, ""


def to_clipboard(value: str) -> None:
    subprocess.run(["pbcopy"], input=value.encode(), check=True)


def cmd_api_token(args: argparse.Namespace) -> int:
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        sys.stderr.write(
            f"api-token: {PASSWORD_ENV} is not set; run under "
            f"`secret exec {PASSWORD_ENV}=<keychain item> -- llens api-token --approve-network`\n"
        )
        return EXIT_CONFIG
    auth_url, email = site.get("api", "auth_url").rstrip("/"), site.get("api", "email")
    need_network(args, AuthNetwork(), "api-token")
    jwt, why = sign_in(transport(), auth_url, email, password)
    if not jwt:
        sys.stderr.write(f"api-token: {why}\n")
        return EXIT_AUTH
    if args.copy:
        to_clipboard(jwt)
        sys.stderr.write("api-token: JWT copied to the clipboard (valid ~15 min)\n")
    else:
        print(jwt)
    return 0


HELP = "a short-lived Neon Data API JWT for the operator (Neon Auth sign-in)"


def token_args(t) -> None:
    """api-token's arguments: ONE definition, for `api-token` and its alias `db token`."""
    approve_network_arg(t)
    t.add_argument("--copy", action="store_true", help="copy the JWT to the clipboard instead of printing it")
    t.set_defaults(fn=cmd_api_token)


def register(sub) -> None:
    token_args(sub.add_parser("api-token", help=HELP))
