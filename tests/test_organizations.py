from tests.conftest import make_doc, ready_doc


def test_org_isolation_documents(env):
    doc = make_doc(env)
    r = env.other.get(f"/api/documents/{doc['id']}")
    assert r.status_code == 404 and r.json()["error"]["code"] == "DOCUMENT_NOT_FOUND"
    assert env.other.get("/api/documents").json()["total"] == 0


def test_cannot_read_other_organization(env):
    assert env.other.get(f"/api/organizations/{env.org_id}").status_code == 404
    assert env.other.get(f"/api/organizations/{env.org_id}/members").status_code == 404
    assert env.other.post(f"/api/organizations/{env.org_id}/members",
                          json={"email": "x@x.test", "role": "employee"}).status_code == 404


def test_other_org_header_is_rejected(env):
    r = env.other.get("/api/documents", headers={"X-Organization-ID": str(env.org_id)})
    assert r.status_code == 403 and r.json()["error"]["code"] == "ORGANIZATION_ACCESS_DENIED"


def test_only_admin_manages_members(env):
    r = env.manager.post(f"/api/organizations/{env.org_id}/members", json={"email": "admin@other.test", "role": "employee"})
    assert r.status_code == 403


def test_deactivate_member_preserves_ownership_and_blocks_access(env):
    doc = make_doc(env)
    members = env.admin.get(f"/api/organizations/{env.org_id}/members").json()
    m = next(x for x in members if x["user_id"] == env.employee.id)
    r = env.admin.delete(f"/api/organizations/{env.org_id}/members/{m['id']}")
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert env.employee.get("/api/documents").status_code == 403  # no active organization any more
    got = env.admin.get(f"/api/documents/{doc['id']}").json()
    assert got["owner_id"] == env.employee.id  # ownership preserved


def test_cannot_remove_last_admin(env):
    members = env.admin.get(f"/api/organizations/{env.org_id}/members").json()
    me = next(x for x in members if x["user_id"] == env.admin.id)
    r = env.admin.patch(f"/api/organizations/{env.org_id}/members/{me['id']}", json={"is_active": False})
    assert r.status_code == 400 and r.json()["error"]["code"] == "LAST_ADMIN"


def test_role_permissions_on_configuration(env):
    assert env.employee.post("/api/categories", json={"name": "X"}).status_code == 403
    assert env.manager.post("/api/workflows", json={"name": "w", "category_ids": [env.category], "stages": [
        {"name": "s", "reviewer_ids": [env.rev1.id]}]}).status_code == 403
    assert env.employee.post("/api/retention-policies",
                             json={"category_id": env.category, "retention_days": 5}).status_code == 403


def test_auditor_is_read_only(env):
    doc = ready_doc(env)
    assert env.auditor.get(f"/api/documents/{doc['id']}").status_code == 200
    assert env.auditor.post("/api/documents", json={"title": "t", "category_id": env.category,
                                                    "metadata": {"contract_number": "1"}}).status_code == 403
    assert env.auditor.patch(f"/api/documents/{doc['id']}", json={"title": "x"}).status_code == 403
    assert env.auditor.delete(f"/api/documents/{doc['id']}").status_code == 403
    assert env.auditor.post(f"/api/documents/{doc['id']}/archive").status_code == 403
