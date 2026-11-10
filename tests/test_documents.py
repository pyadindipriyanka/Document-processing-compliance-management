from datetime import date, datetime, timedelta, timezone

from tests.conftest import PDF, make_doc, ready_doc, upload_version


def test_create_document_and_unique_numbers(env):
    a, b = make_doc(env), make_doc(env)
    assert a["status"] == "DRAFT" and a["document_number"] != b["document_number"]
    assert a["owner_id"] == env.employee.id and a["metadata"]["contract_number"] == "C-1001"


def test_missing_required_metadata_rejected(env):
    r = env.employee.post("/api/documents", json={"title": "t", "category_id": env.category, "metadata": {}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "REQUIRED_METADATA_MISSING"


def test_invalid_metadata_type_and_unknown_field(env):
    r = env.employee.post("/api/documents", json={"title": "t", "category_id": env.category, "metadata": {
        "contract_number": "1", "expiration_date": "not-a-date"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_METADATA"
    r = env.employee.post("/api/documents", json={"title": "t", "category_id": env.category, "metadata": {
        "contract_number": "1", "nope": "x"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_METADATA"


def test_patch_metadata_validates_and_syncs_expiration(env):
    doc = make_doc(env)
    r = env.employee.patch(f"/api/documents/{doc['id']}/metadata", json={"metadata": {"vendor": None}})
    assert r.status_code == 200
    bad = env.employee.patch(f"/api/documents/{doc['id']}/metadata", json={"metadata": {"contract_number": None}})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "REQUIRED_METADATA_MISSING"
    exp = (date.today() + timedelta(days=90)).isoformat()
    env.employee.patch(f"/api/documents/{doc['id']}/metadata", json={"metadata": {"expiration_date": exp}})
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["expiration_date"] == exp


def test_update_and_forbidden_update(env):
    doc = make_doc(env)
    assert env.employee.patch(f"/api/documents/{doc['id']}", json={"title": "New"}).json()["title"] == "New"
    r = env.employee2.patch(f"/api/documents/{doc['id']}", json={"title": "Hack"})
    assert r.status_code in (403,)  # draft of someone else is invisible/forbidden


def test_archive_and_restore(env):
    doc = ready_doc(env)
    r = env.employee.post(f"/api/documents/{doc['id']}/archive")
    assert r.status_code == 200 and r.json()["status"] == "ARCHIVED"
    assert env.employee.post(f"/api/documents/{doc['id']}/archive").status_code == 409
    assert env.employee.post(f"/api/documents/{doc['id']}/submit").json()["error"]["code"] == "DOCUMENT_ARCHIVED"
    r = env.employee.post(f"/api/documents/{doc['id']}/restore")
    assert r.status_code == 200 and r.json()["status"] == "DRAFT"
    assert env.employee.post(f"/api/documents/{doc['id']}/restore").status_code == 409


def test_delete_rules(env):
    doc = make_doc(env)
    assert env.employee2.delete(f"/api/documents/{doc['id']}").status_code == 403
    assert env.employee.delete(f"/api/documents/{doc['id']}").status_code == 204
    assert env.employee.get(f"/api/documents/{doc['id']}").status_code == 404


def test_retention_blocks_deletion(env):
    r = env.admin.post("/api/retention-policies", json={"category_id": env.category, "retention_days": 365})
    assert r.status_code == 201
    doc = make_doc(env)
    d = env.employee.delete(f"/api/documents/{doc['id']}")
    assert d.status_code == 409 and d.json()["error"]["code"] == "DOCUMENT_RETENTION_ACTIVE"
    pid = r.json()["id"]
    assert env.admin.patch(f"/api/retention-policies/{pid}", json={"prohibit_deletion": False}).status_code == 200
    assert env.employee.delete(f"/api/documents/{doc['id']}").status_code == 204


def test_draft_hidden_from_other_employees_but_visible_to_manager(env):
    doc = make_doc(env)
    assert env.employee2.get(f"/api/documents/{doc['id']}").status_code == 403
    assert env.manager.get(f"/api/documents/{doc['id']}").status_code == 200
    assert env.employee2.get("/api/documents").json()["total"] == 0


def test_access_levels(env):
    doc = ready_doc(env, access_level="PRIVATE")
    assert env.manager.get(f"/api/documents/{doc['id']}").status_code == 403
    assert env.auditor.get(f"/api/documents/{doc['id']}").status_code == 403
    assert env.admin.get(f"/api/documents/{doc['id']}").status_code == 200
    r = env.employee.patch(f"/api/documents/{doc['id']}", json={"allowed_user_ids": [env.manager.id]})
    assert r.status_code == 200
    assert env.manager.get(f"/api/documents/{doc['id']}").status_code == 200


def test_search_filters_sorting_pagination(env):
    for i in range(3):
        make_doc(env, title=f"Contract alpha {i}")
    make_doc(env, title="Invoice beta", metadata={"contract_number": "Z-9", "vendor": "Initech"})
    r = env.admin.get("/api/documents", params={"search": "alpha", "page_size": 2, "sort_by": "title",
                                                "sort_order": "asc"}).json()
    assert r["total"] == 3 and len(r["items"]) == 2 and r["pages"] == 2
    assert r["items"][0]["title"] == "Contract alpha 0"
    assert env.admin.get("/api/documents", params={"search": "initech"}).json()["total"] == 1  # metadata is indexed
    assert env.admin.get("/api/documents", params={"status": "draft"}).json()["total"] == 4
    assert env.admin.get("/api/documents", params={"status": "under_review"}).json()["total"] == 0
    assert env.admin.get("/api/documents", params={"owner_id": env.employee2.id}).json()["total"] == 0
    today = datetime.now(timezone.utc).date().isoformat()
    assert env.admin.get("/api/documents", params={"from_date": today, "to_date": today}).json()["total"] == 4
    assert env.admin.get("/api/documents", params={"status": "bogus"}).status_code == 422


def test_expiring_and_expired_endpoints(env, db):
    from app.models import Document
    soon = make_doc(env, expiration_date=(date.today() + timedelta(days=10)).isoformat())
    db.query(Document).filter_by(id=soon["id"]).update({"status": "APPROVED"})
    db.commit()
    assert env.admin.get("/api/documents/expiring?days=30").json()["total"] == 1
    assert env.admin.get("/api/documents/expiring?days=5").json()["total"] == 0
    assert env.admin.get("/api/documents/expired").json()["total"] == 0


def test_download_requires_version(env):
    doc = make_doc(env)
    assert env.employee.get(f"/api/documents/{doc['id']}/download").status_code == 404
    upload_version(env.employee, doc["id"])
    r = env.employee.get(f"/api/documents/{doc['id']}/download")
    assert r.status_code == 200 and r.content == PDF
    assert r.headers["x-content-type-options"] == "nosniff"
