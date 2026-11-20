"""llens api-token: a short-lived Neon Data API JWT for the operator, from the password in Keychain.
A fake transport stands in for Neon Auth: no network, SYNTHETIC values only."""

import pytest
from scrape_harness.cli import EXIT_NETWORK, main

from listing_lens import api_token, site

PASSWORD = "synthetic-password-never-printed"
JWT = "eyJ.synthetic.jwt"
AUTH = "https://auth.example.invalid/db/auth"


class Fake:
    def __init__(self, sign_in=200, session=200, jwt=JWT):
        self.sign_in, self.session, self.jwt, self.calls = sign_in, session, jwt, []

    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        if url.endswith("/sign-in/email"):
            return self.sign_in, {}
        if url.endswith("/get-session"):
            return self.session, ({"set-auth-jwt": self.jwt} if self.jwt and self.session == 200 else {})
        return 404, {}


@pytest.fixture
def api(monkeypatch):
    cfg = {"auth_url": AUTH, "email": "operator@example.invalid"}
    monkeypatch.setattr(site, "get", lambda table, key: cfg[key] if table == "api" else pytest.fail(table))
    monkeypatch.setenv("LLENS_API_PASSWORD", PASSWORD)
    fake = Fake()
    monkeypatch.setattr(api_token, "transport", lambda: fake)
    return fake


def run(capsys, *extra):
    capsys.readouterr()
    code = main(["api-token", *extra])
    out, err = capsys.readouterr()
    return code, out, err


def test_prints_the_jwt_and_never_the_password(api, capsys):
    code, out, err = run(capsys, "--approve-network")
    assert code == 0 and out.strip() == JWT
    assert PASSWORD not in out + err
    (m1, u1, b1), (m2, u2, _) = api.calls
    assert (m1, u1) == ("POST", AUTH + "/sign-in/email") and b1["password"] == PASSWORD
    assert (m2, u2) == ("GET", AUTH + "/get-session")


def test_copy_puts_it_on_the_clipboard_not_stdout(api, capsys, monkeypatch):
    got = []
    monkeypatch.setattr(api_token, "to_clipboard", got.append)
    code, out, err = run(capsys, "--approve-network", "--copy")
    assert code == 0 and got == [JWT] and JWT not in out + err


def test_without_approve_network_nothing_is_sent(api, capsys):
    with pytest.raises(SystemExit) as e:  # the harness's need_network refusal
        run(capsys)
    assert e.value.code == EXIT_NETWORK and api.calls == []


def test_without_the_password_nothing_is_sent(api, capsys, monkeypatch):
    monkeypatch.delenv("LLENS_API_PASSWORD")
    code, _, err = run(capsys, "--approve-network")
    assert code == 2 and "secret exec LLENS_API_PASSWORD=" in err and api.calls == []


@pytest.mark.parametrize("kw", [{"sign_in": 401}, {"session": 401}, {"jwt": None}])
def test_a_failed_sign_in_is_exit_11_with_the_status_only(api, capsys, kw):
    for k, v in kw.items():
        setattr(api, k, v)
    code, out, err = run(capsys, "--approve-network")
    assert code == 11 and out == "" and PASSWORD not in err
