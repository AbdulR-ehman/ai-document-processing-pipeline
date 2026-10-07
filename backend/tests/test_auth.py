"""Authentication: registration policy, login, tokens, lockout."""

from __future__ import annotations

import base64
import json

import pytest

from conftest import ADMIN_EMAIL, BASE, build_client, register_account


def test_admin_email_registration_yields_a_normal_user():
    """Registering the ADMIN_EMAIL address through the API never grants a role."""
    with build_client() as fresh:
        account = register_account(fresh, ADMIN_EMAIL, "TryToBeAdmin1")
        assert account.role == "user"
        # ... and it therefore has no admin-only powers.
        assert fresh.get(f"{BASE}/documents?scope=all", headers=account.headers).status_code == 403


def test_admin_email_registration_is_case_insensitive_and_still_a_user():
    with build_client() as fresh:
        account = register_account(fresh, ADMIN_EMAIL.upper(), "TryToBeAdmin2")
        assert account.role == "user"


def test_seeded_admin_is_the_only_admin(client, admin, user):
    assert admin.role == "admin"
    assert user.role == "user"
    early = register_account(client, "early-bird@example.com", "Passw0rdTest3")
    assert early.role == "user"


def test_registering_first_user_gives_no_admin(client):
    """A fresh deployment with no seed step has no administrator at all."""
    account = register_account(client, "first@example.com", "Passw0rdTest1")
    assert account.role == "user"
    body = client.get(f"{BASE}/documents?scope=all", headers=account.headers)
    assert body.status_code == 403


def test_seed_admin_creates_promotes_and_updates(client):
    """``scripts/seed_admin.py`` covers the bootstrap paths."""
    from app.db import get_session_factory
    from app.services.admin import ensure_admin

    session = get_session_factory()()

    user, action = ensure_admin(session, "owner@example.com", "Str0ngPassw0rd")
    assert (user.role, action) == ("admin", "created")

    plain = register_account(client, "plain@example.com", "Passw0rdTest1")
    promoted, action = ensure_admin(session, "plain@example.com", "Passw0rdTest1")
    assert (promoted.role, action) == ("admin", "promoted")

    unchanged, action = ensure_admin(session, "owner@example.com", "Str0ngPassw0rd")
    assert action == "already_admin"

    rotated, action = ensure_admin(session, "owner@example.com", "N3wPassw0rd!")
    assert action == "password_updated"
    assert plain.role == "user"
    session.close()


def test_seed_admin_validates_input(client):
    from app.db import get_session_factory
    from app.services.admin import AdminSetupError, ensure_admin

    session = get_session_factory()()
    with pytest.raises(AdminSetupError):
        ensure_admin(session, "not-an-email", "Passw0rdTest1")
    with pytest.raises(AdminSetupError):
        ensure_admin(session, "weak@example.com", "short")
    session.close()


def test_register_ignores_role_from_client(client):
    response = client.post(
        f"{BASE}/auth/register",
        json={"email": "sneaky@example.com", "password": "Passw0rdTest1", "role": "admin"},
    )
    assert response.status_code == 422  # extra fields are forbidden


def test_register_rejects_duplicate_email(client):
    register_account(client, "dupe@example.com", "Passw0rdTest1")
    response = client.post(
        f"{BASE}/auth/register",
        json={"email": "DUPE@example.com", "password": "Passw0rdTest2"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"


def test_register_rejects_weak_passwords(client):
    cases = ["short1", "alllettersonly", "1234567890"]
    for password in cases:
        response = client.post(
            f"{BASE}/auth/register",
            json={"email": f"weak{cases.index(password)}@example.com", "password": password},
        )
        assert response.status_code == 422, password
        assert response.json()["error"]["code"] == "weak_password"


def test_register_rejects_bad_email(client):
    for email in ["nope", "a@b", "@example.com", "spaces here@example.com"]:
        response = client.post(
            f"{BASE}/auth/register", json={"email": email, "password": "Passw0rdTest1"}
        )
        assert response.status_code == 422, email


def test_password_never_leaks_in_responses(client):
    response = client.post(
        f"{BASE}/auth/register",
        json={"email": "leak@example.com", "password": "S3cretPassw0rd"},
    )
    assert response.status_code == 201
    assert "S3cretPassw0rd" not in response.text
    assert "password_hash" not in response.text


def test_login_success_and_failure(client):
    register_account(client, "login@example.com", "Passw0rdTest1")
    bad = client.post(
        f"{BASE}/auth/login",
        json={"email": "login@example.com", "password": "WrongPass99"},
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_credentials"

    good = client.post(
        f"{BASE}/auth/login",
        json={"email": "login@example.com", "password": "Passw0rdTest1"},
    )
    assert good.status_code == 200
    assert good.json()["access_token"]
    assert good.json()["token_type"] == "bearer"
    assert good.json()["expires_in"] > 0


def test_unknown_email_gives_same_error_as_wrong_password(client):
    register_account(client, "known@example.com", "Passw0rdTest1")
    unknown = client.post(
        f"{BASE}/auth/login",
        json={"email": "unknown@example.com", "password": "Whatever123"},
    )
    wrong = client.post(
        f"{BASE}/auth/login",
        json={"email": "known@example.com", "password": "Whatever123"},
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]


def test_me_returns_own_account(client, admin):
    account = register_account(client, "me@example.com", "Passw0rdTest1")
    body = client.get(f"{BASE}/auth/me", headers=account.headers).json()
    assert body["email"] == "me@example.com"
    assert body["role"] == "user"
    assert client.get(f"{BASE}/auth/me", headers=admin.headers).json()["role"] == "admin"
    assert "password" not in client.get(f"{BASE}/auth/me", headers=account.headers).text


def test_me_requires_authentication(client):
    response = client.get(f"{BASE}/auth/me")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def _b64(data: dict) -> str:
    raw = json.dumps(data).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_tampered_token_is_rejected(client):
    account = register_account(client, "tamper@example.com", "Passw0rdTest1")
    header, payload, signature = account.token.split(".")
    flipped = ("A" if signature[-1] != "A" else "B")
    response = client.get(
        f"{BASE}/auth/me",
        headers={"Authorization": f"Bearer {header}.{payload}.{flipped}"},
    )
    assert response.status_code == 401


def test_alg_none_token_is_rejected(client):
    forged = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64({'sub': '1', 'iat': 1, 'exp': 4102444800, 'jti': 'x', 'typ': 'access'})}."
    response = client.get(
        f"{BASE}/auth/me", headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401


def test_token_signed_with_wrong_key_is_rejected(client):
    import jwt

    register_account(client, "victim@example.com", "Passw0rdTest1")
    forged = jwt.encode(
        {"sub": "1", "iat": 1, "exp": 4102444800, "jti": "x", "typ": "access"},
        "a-completely-different-secret-key",
        algorithm="HS256",
    )
    response = client.get(
        f"{BASE}/auth/me", headers={"Authorization": f"Bearer {forged}"}
    )
    assert response.status_code == 401


def test_logout_is_recorded_but_stateless(client):
    account = register_account(client, "out@example.com", "Passw0rdTest1")
    assert client.post(f"{BASE}/auth/logout", headers=account.headers).status_code == 204
    # Stateless design: the short-lived token still verifies until it expires.
    assert client.get(f"{BASE}/auth/me", headers=account.headers).status_code == 200
    audit = client.get(f"{BASE}/audit?action=logout", headers=account.headers).json()
    assert audit["total"] == 1


def test_login_lockout_after_configured_failures(client):
    register_account(client, "lock@example.com", "Passw0rdTest1")
    for _ in range(5):
        response = client.post(
            f"{BASE}/auth/login",
            json={"email": "lock@example.com", "password": "WrongPass99"},
        )
        assert response.status_code == 401
    locked = client.post(
        f"{BASE}/auth/login",
        json={"email": "lock@example.com", "password": "Passw0rdTest1"},
    )
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "account_locked"
    assert int(locked.headers["retry-after"]) > 0


def test_login_rate_limit_from_settings():
    with build_client({"RATE_LIMIT_LOGIN": "3/minute"}) as client:
        register_account(client, "rl@example.com", "Passw0rdTest1")
        codes = [
            client.post(
                f"{BASE}/auth/login",
                json={"email": "rl@example.com", "password": "Passw0rdTest1"},
            ).status_code
            for _ in range(4)
        ]
        assert codes[:3] == [200, 200, 200]
        assert codes[3] == 429


def test_audit_entries_never_contain_passwords(client):
    account = register_account(client, "audited@example.com", "S3cretPassw0rd")
    client.post(
        f"{BASE}/auth/login",
        json={"email": "audited@example.com", "password": "S3cretPassw0rd"},
    )
    body = client.get(f"{BASE}/audit", headers=account.headers).text
    assert "S3cretPassw0rd" not in body
    assert "password" not in body.lower()
