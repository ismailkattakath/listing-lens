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

from scrape_harness.cli import need_network

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
    call = transport()
    status, _ = call("POST", auth_url + "/sign-in/email", {"email": email, "password": password})
    if status != 200:
        sys.stderr.write(f"api-token: sign-in refused (HTTP {status})\n")
        return EXIT_AUTH
    status, headers = call("GET", auth_url + "/get-session")
    jwt = headers.get("set-auth-jwt")
    if status != 200 or not jwt:
        sys.stderr.write(f"api-token: no JWT in the session (HTTP {status})\n")
        return EXIT_AUTH
    if args.copy:
        to_clipboard(jwt)
        sys.stderr.write("api-token: JWT copied to the clipboard (valid ~15 min)\n")
    else:
        print(jwt)
    return 0


def register(sub) -> None:
    t = sub.add_parser(
        "api-token", help="a short-lived Neon Data API JWT for the operator (Neon Auth sign-in)"
    )
    t.add_argument("--approve-network", action="store_true")
    t.add_argument("--copy", action="store_true", help="copy the JWT to the clipboard instead of printing it")
    t.set_defaults(fn=cmd_api_token)
