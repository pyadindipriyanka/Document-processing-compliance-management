import threading

import pytest

from tests.conftest import (IS_POSTGRES, approve_all, approved_doc, make_doc, pending, ready_doc, submit,
                            upload_version)


def _my_review(actor, doc):
    return next(i for i in pending(actor) if i["document_id"] == doc["id"])


def test_workflow_validation_and_activation_rules(env):
    bad = env.admin.post("/api/workflows", json={"name": "bad", "category_ids": [env.category],
                                                 "stages": [{"name": "s"}]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_WORKFLOW"
    auditor_stage = env.admin.post("/api/workflows", json={"name": "bad2", "category_ids": [env.category], "stages": [
        {"name": "s", "reviewer_ids": [env.auditor.id]}]})
    assert auditor_stage.status_code == 422  # auditors are read-only and cannot approve
    other = env.admin.post("/api/workflows", json={"name": "second", "category_ids": [env.category], "stages": [
        {"name": "s", "reviewer_ids": [env.rev1.id]}]}).json()
    r = env.admin.post(f"/api/workflows/{other['id']}/activate")
    assert r.status_code == 409 and r.json()["error"]["code"] == "WORKFLOW_CONFLICT"
    assert env.admin.post(f"/api/workflows/{env.workflow}/deactivate").json()["is_active"] is False
    assert env.admin.post(f"/api/workflows/{other['id']}/activate").status_code == 200
    assert len(env.manager.get("/api/workflows").json()) == 2


def test_full_multi_stage_approval(env):
    doc = ready_doc(env)
    r = submit(env, doc)
    assert r.status_code == 200 and r.json()["status"] == "UNDER_REVIEW"
    assert pending(env.rev2) == []  # stage 2 not active yet (sequential)
    r1 = _my_review(env.rev1, doc)
    assert env.rev2.post(f"/api/reviews/{r1['id']}/approve").status_code == 403  # not the assigned reviewer
    assert env.rev1.post(f"/api/reviews/{r1['id']}/approve", json={"comment": "fine"}).status_code == 200
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["status"] == "UNDER_REVIEW"
    r2 = _my_review(env.rev2, doc)
    assert env.rev2.post(f"/api/reviews/{r2['id']}/approve").status_code == 200
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["status"] == "APPROVED"
    history = env.admin.get(f"/api/documents/{doc['id']}/approval-history").json()
    decisions = [h["decision"] for h in history]
    assert decisions[0] == "SUBMITTED" and decisions[-1] == "FINAL_APPROVED" and decisions.count("APPROVED") == 2
    assert history[-1]["metadata_snapshot"]["metadata"]["contract_number"] == "C-1001"
    notes = env.employee.get("/api/notifications").json()["items"]
    assert "APPROVAL_COMPLETED" in [n["type"] for n in notes]


def test_duplicate_decision_rejected(env):
    doc = ready_doc(env)
    submit(env, doc)
    r1 = _my_review(env.rev1, doc)
    assert env.rev1.post(f"/api/reviews/{r1['id']}/approve").status_code == 200
    again = env.rev1.post(f"/api/reviews/{r1['id']}/approve")
    assert again.status_code == 409 and again.json()["error"]["code"] == "REVIEW_ALREADY_COMPLETED"
    assert env.rev1.post(f"/api/reviews/{r1['id']}/reject", json={"comment": "x"}).status_code == 409


def test_reject_and_request_changes_need_comment(env):
    doc = ready_doc(env)
    submit(env, doc)
    r1 = _my_review(env.rev1, doc)
    for action in ("reject", "request-changes"):
        r = env.rev1.post(f"/api/reviews/{r1['id']}/{action}", json={"comment": "  "})
        assert r.status_code == 422 and r.json()["error"]["code"] == "COMMENT_REQUIRED"
        assert env.rev1.post(f"/api/reviews/{r1['id']}/{action}").status_code == 422


def test_reject_then_resubmit(env):
    doc = ready_doc(env)
    submit(env, doc)
    r1 = _my_review(env.rev1, doc)
    assert env.rev1.post(f"/api/reviews/{r1['id']}/reject", json={"comment": "Wrong vendor"}).status_code == 200
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["status"] == "REJECTED"
    assert "DOCUMENT_REJECTED" in [n["type"] for n in env.employee.get("/api/notifications").json()["items"]]
    assert upload_version(env.employee, doc["id"], description="fixed").status_code == 201
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["status"] == "DRAFT"
    assert submit(env, doc).status_code == 200
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["submission_no"] == 2
    assert len(pending(env.rev1)) == 1


def test_request_changes_returns_to_draft_and_can_resubmit(env):
    doc = ready_doc(env)
    submit(env, doc)
    r1 = _my_review(env.rev1, doc)
    assert env.rev1.post(f"/api/reviews/{r1['id']}/request-changes", json={"comment": "add annex"}).status_code == 200
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["status"] == "DRAFT"
    assert submit(env, doc).status_code == 200


def test_invalid_transitions(env):
    doc = ready_doc(env)
    assert submit(env, doc).status_code == 200
    again = submit(env, doc)
    assert again.status_code == 409 and again.json()["error"]["code"] == "INVALID_WORKFLOW_TRANSITION"
    arch = env.employee.post(f"/api/documents/{doc['id']}/archive")
    assert arch.status_code == 409 and arch.json()["error"]["code"] == "INVALID_WORKFLOW_TRANSITION"
    assert env.employee.delete(f"/api/documents/{doc['id']}").status_code == 409
    assert env.employee.patch(f"/api/documents/{doc['id']}", json={"title": "x"}).json()["error"]["code"] == "DOCUMENT_NOT_EDITABLE"


def test_submit_preconditions(env):
    doc = make_doc(env)
    r = submit(env, doc)
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_DOCUMENT_VERSION"
    cat = env.admin.post("/api/categories", json={"name": "NoFlow"}).json()["id"]
    d2 = env.employee.post("/api/documents", json={"title": "x", "category_id": cat}).json()
    upload_version(env.employee, d2["id"])
    r = submit(env, d2)
    assert r.status_code == 409 and r.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"
    assert submit(env, doc, env.employee2).status_code == 403


def test_approval_invalidated_by_material_change_but_not_minor(env):
    doc = approved_doc(env)
    assert upload_version(env.employee, doc["id"], PDF_PLUS(1), material=False).status_code == 201
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["status"] == "APPROVED"
    assert upload_version(env.employee, doc["id"], PDF_PLUS(2), material=True).status_code == 201
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["status"] == "DRAFT"
    history = env.admin.get(f"/api/documents/{doc['id']}/approval-history").json()
    assert "INVALIDATED" in [h["decision"] for h in history]
    assert [h["version_number"] for h in history if h["decision"] == "FINAL_APPROVED"] == [1]  # tied to v1
    assert [h["version_number"] for h in history if h["decision"] == "INVALIDATED"] == [2]  # v2 was current
    assert submit(env, doc).status_code == 200  # re-approval required
    assert len([h for h in env.admin.get(f"/api/documents/{doc['id']}/approval-history").json()
                if h["decision"] == "SUBMITTED"]) == 2  # earlier approval history preserved


def PDF_PLUS(n):
    return b"%PDF-1.4\n" + bytes([65 + n]) * 20 + b"\n%%EOF"


def test_material_change_during_review_cancels_pending_reviews(env):
    doc = ready_doc(env)
    submit(env, doc)
    assert len(pending(env.rev1)) == 1
    upload_version(env.employee, doc["id"], PDF_PLUS(3))
    assert pending(env.rev1) == []
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["status"] == "DRAFT"


def test_owner_is_excluded_from_reviewing_own_document(env):
    # make rev1 the only stage-1 reviewer AND a document owner via manager role switch
    members = env.admin.get(f"/api/organizations/{env.org_id}/members").json()
    m = next(x for x in members if x["user_id"] == env.rev1.id)
    env.admin.patch(f"/api/organizations/{env.org_id}/members/{m['id']}", json={"role": "document_manager"})
    doc = ready_doc(env, who=env.rev1)
    r = submit(env, doc, env.rev1)
    assert r.status_code == 409 and r.json()["error"]["code"] == "WORKFLOW_NOT_CONFIGURED"


def test_reassign(env):
    doc = ready_doc(env)
    submit(env, doc)
    r1 = _my_review(env.rev1, doc)
    assert env.rev1.post(f"/api/reviews/{r1['id']}/reassign", json={"new_reviewer_id": env.rev2.id}).status_code == 403
    assert env.admin.post(f"/api/reviews/{r1['id']}/reassign", json={"new_reviewer_id": env.employee.id}).status_code == 422
    assert env.admin.post(f"/api/reviews/{r1['id']}/reassign", json={"new_reviewer_id": env.rev2.id,
                                                                     "reason": "vacation"}).status_code == 200
    assert env.rev1.post(f"/api/reviews/{r1['id']}/approve").status_code == 409
    assert len(pending(env.rev2)) == 1


def test_pending_listing_scopes(env):
    doc = ready_doc(env)
    submit(env, doc)
    assert len(pending(env.rev1)) == 1 and pending(env.employee) == []
    assert env.employee.get("/api/reviews/pending?all=true").json()["total"] == 0  # ignored for plain employees
    assert env.admin.get("/api/reviews/pending?all=true").json()["total"] == 1
    review = pending(env.rev1)[0]
    assert env.rev1.get(f"/api/reviews/{review['id']}").status_code == 200
    assert env.other.get(f"/api/reviews/{review['id']}").status_code == 404


def test_auditor_cannot_decide(env):
    doc = ready_doc(env)
    submit(env, doc)
    review = _my_review(env.rev1, doc)
    assert env.auditor.post(f"/api/reviews/{review['id']}/approve").status_code == 403


def test_minimum_approvals_stage(env):
    wf = env.admin.post("/api/workflows", json={"name": "quorum", "category_ids": [], "stages": []})
    assert wf.status_code == 422
    env.admin.post(f"/api/workflows/{env.workflow}/deactivate")
    created = env.admin.post("/api/workflows", json={"name": "two-of-two", "category_ids": [env.category], "stages": [
        {"name": "Both", "reviewer_ids": [env.rev1.id, env.rev2.id], "require_all": True}]}).json()
    env.admin.post(f"/api/workflows/{created['id']}/activate")
    doc = ready_doc(env)
    submit(env, doc)
    env.rev1.post(f"/api/reviews/{_my_review(env.rev1, doc)['id']}/approve")
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["status"] == "UNDER_REVIEW"  # still incomplete
    env.rev2.post(f"/api/reviews/{_my_review(env.rev2, doc)['id']}/approve")
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["status"] == "APPROVED"


def test_workflow_edit_does_not_affect_in_flight_documents(env):
    doc = ready_doc(env)
    submit(env, doc)
    r = env.admin.patch(f"/api/workflows/{env.workflow}", json={"stages": [
        {"name": "Only one", "reviewer_ids": [env.rev2.id]}]})
    assert r.status_code == 200 and r.json()["version"] == 2
    env.rev1.post(f"/api/reviews/{_my_review(env.rev1, doc)['id']}/approve")
    assert len(pending(env.rev2)) == 1  # in-flight document still follows its frozen two-stage snapshot


@pytest.mark.skipif(not IS_POSTGRES, reason="needs PostgreSQL row locking (set TEST_DATABASE_URL)")
def test_concurrent_decisions_on_same_review(env):
    doc = ready_doc(env)
    submit(env, doc)
    review = _my_review(env.rev1, doc)
    results = []
    threads = [threading.Thread(target=lambda: results.append(
        env.rev1.post(f"/api/reviews/{review['id']}/approve").status_code)) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [200] + [409] * 5
