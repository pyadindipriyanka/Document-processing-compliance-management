import pytest

from app.models import AuditLog, User
from tests.conftest import PASSWORD, signup


def test_register_login_me(client):
    a = signup(client, "a@x.test")
    me = a.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["email"] == "a@x.test"


def test_register_rejects_weak_password_and_duplicates(client):
    r = client.post("/api/auth/register", json={"email": "w@x.test", "password": "short", "full_name": "W"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    signup(client, "dup@x.test")
    r = client.post("/api/auth/register", json={"email": "DUP@x.test", "password": PASSWORD, "full_name": "D"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"


def test_protected_endpoint_requires_token(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401 and r.json() == {"success": False, "error": {
        "code": "NOT_AUTHENTICATED", "message": "Authentication required."}}
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_wrong_password_is_audited(client, db):
    signup(client, "u@x.test")
    r = client.post("/api/auth/login", json={"email": "u@x.test", "password": "WrongPass1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "INVALID_CREDENTIALS"
    assert db.query(AuditLog).filter_by(action="LOGIN_FAILED").count() == 1


def test_inactive_user_cannot_authenticate(client, db):
    signup(client, "gone@x.test")
    db.query(User).filter_by(email="gone@x.test").update({"is_active": False})
    db.commit()
    r = client.post("/api/auth/login", json={"email": "gone@x.test", "password": PASSWORD})
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCOUNT_INACTIVE"


def test_refresh_rotation_and_reuse_detection(client):
    a = signup(client, "r@x.test")
    old = a.tokens["refresh_token"]
    r = client.post("/api/auth/refresh", json={"refresh_token": old})
    assert r.status_code == 200
    new = r.json()
    reuse = client.post("/api/auth/refresh", json={"refresh_token": old})
    assert reuse.status_code == 401 and reuse.json()["error"]["code"] == "REFRESH_TOKEN_REUSED"
    # the whole session family is now revoked, including the newest tokens
    assert client.post("/api/auth/refresh", json={"refresh_token": new["refresh_token"]}).status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {new['access_token']}"}).status_code == 401


def test_access_token_cannot_be_used_as_refresh(client):
    a = signup(client, "t@x.test")
    r = client.post("/api/auth/refresh", json={"refresh_token": a.tokens["access_token"]})
    assert r.status_code == 401


def test_logout_revokes_session(client):
    a = signup(client, "l@x.test")
    assert a.post("/api/auth/logout").status_code == 200
    assert a.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": a.tokens["refresh_token"]}).status_code == 401


def test_change_password_revokes_sessions(client):
    a = signup(client, "c@x.test")
    bad = a.post("/api/auth/change-password", json={"current_password": "nope", "new_password": "NewPassw0rd"})
    assert bad.status_code == 400
    ok = a.post("/api/auth/change-password", json={"current_password": PASSWORD, "new_password": "NewPassw0rd"})
    assert ok.status_code == 200
    assert a.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/login", json={"email": "c@x.test", "password": "NewPassw0rd"}).status_code == 200


def test_audit_logs_are_immutable(env, db):
    row = db.query(AuditLog).first()
    row.action = "TAMPERED"
    with pytest.raises(Exception):
        db.commit()
    db.rollback()


def test_audit_log_endpoint_roles_and_no_secrets(env):
    assert env.employee.get("/api/audit-logs").status_code == 403
    assert env.auditor.get("/api/audit-logs").status_code == 200
    body = env.admin.get("/api/audit-logs?page_size=200").text
    assert PASSWORD not in body and "hashed_password" not in body
