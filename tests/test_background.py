from datetime import date, timedelta

import pytest

from app.core.enums import JobStatus, JobType
from app.core.types import utcnow
from app.models import BackgroundJob, Document, DocumentReview, Notification
from app.services import jobs
from tests.conftest import approved_doc, make_doc, ready_doc, submit, upload_version


def _run(db, *types):
    for t in types:
        jobs.enqueue(db, t, f"test:{t}:{utcnow().timestamp()}", {})
    db.commit()
    jobs.drain(db)


def test_version_upload_enqueues_and_runs_jobs(env, db):
    doc = make_doc(env)
    upload_version(env.employee, doc["id"])  # BackgroundTasks drain runs inside TestClient
    types = {j.job_type: j.status for j in db.query(BackgroundJob).all()}
    assert types[JobType.CHECKSUM_VERIFY] == JobStatus.SUCCEEDED
    assert types[JobType.DOCUMENT_INDEXING] == JobStatus.SUCCEEDED
    assert types[JobType.METADATA_EXTRACTION] == JobStatus.SUCCEEDED


def test_expiration_scan_expires_and_notifies(env, db):
    doc = approved_doc(env)
    db.query(Document).filter_by(id=doc["id"]).update({"expiration_date": date.today() - timedelta(days=1)})
    db.commit()
    _run(db, JobType.EXPIRATION_SCAN)
    assert db.get(Document, doc["id"]).status == "EXPIRED"
    assert "DOCUMENT_EXPIRED" in [n["type"] for n in env.employee.get("/api/notifications").json()["items"]]
    assert env.admin.get("/api/documents/expired").json()["total"] == 1
    # expired documents cannot enter a new approval unless explicitly permitted by a manager
    r = submit(env, doc)
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_EXPIRED"
    assert env.manager.post(f"/api/documents/{doc['id']}/submit", json={"allow_expired": True}).status_code == 200


def test_expiring_soon_notification_is_deduplicated(env, db):
    doc = approved_doc(env)
    db.query(Document).filter_by(id=doc["id"]).update({"expiration_date": date.today() + timedelta(days=5)})
    db.commit()
    _run(db, JobType.EXPIRATION_SCAN)
    _run(db, JobType.EXPIRATION_SCAN)
    n = db.query(Notification).filter_by(type="DOCUMENT_EXPIRING", user_id=env.employee.id).count()
    assert n == 1


def test_renew_expired_document(env, db):
    doc = approved_doc(env)
    db.query(Document).filter_by(id=doc["id"]).update({"expiration_date": date.today() - timedelta(days=1)})
    db.commit()
    _run(db, JobType.EXPIRATION_SCAN)
    new = (date.today() + timedelta(days=200)).isoformat()
    assert env.employee.post(f"/api/documents/{doc['id']}/renew", json={"expiration_date": new}).status_code == 403
    past = env.manager.post(f"/api/documents/{doc['id']}/renew", json={"expiration_date": "2000-01-01"})
    assert past.status_code == 422
    r = env.manager.post(f"/api/documents/{doc['id']}/renew", json={"expiration_date": new})
    assert r.status_code == 200 and r.json()["status"] == "APPROVED"


def test_auto_archive_after_expiry_policy(env, db):
    env.admin.post("/api/retention-policies", json={"category_id": env.category, "retention_days": 10,
                                                    "auto_archive_expired": True})
    doc = approved_doc(env)
    db.query(Document).filter_by(id=doc["id"]).update({"expiration_date": date.today() - timedelta(days=1)})
    db.commit()
    _run(db, JobType.EXPIRATION_SCAN, JobType.RETENTION_CHECK)
    assert db.get(Document, doc["id"]).status in ("EXPIRED", "ARCHIVED")
    _run(db, JobType.RETENTION_CHECK)
    assert db.get(Document, doc["id"]).status == "ARCHIVED"


def test_reminders_and_escalation(env, db):
    doc = ready_doc(env)
    submit(env, doc)
    old = utcnow() - timedelta(days=10)
    db.query(DocumentReview).update({"created_at": old, "due_at": old + timedelta(hours=1)})
    db.commit()
    _run(db, JobType.APPROVAL_REMINDERS, JobType.REVIEWER_ESCALATION)
    types = [n["type"] for n in env.rev1.get("/api/notifications").json()["items"]]
    assert "REVIEW_REMINDER" in types and "REVIEWER_ESCALATION" in types
    assert "REVIEWER_ESCALATION" in [n["type"] for n in env.admin.get("/api/notifications").json()["items"]]
    _run(db, JobType.REVIEWER_ESCALATION)  # already escalated -> no duplicate
    assert db.query(Notification).filter_by(type="REVIEWER_ESCALATION", user_id=env.rev1.id).count() == 1


def test_failed_job_is_retried_then_marked_failed(env, db, monkeypatch):
    calls = {"n": 0}

    def boom(_db, _payload):
        calls["n"] += 1
        raise RuntimeError("transient outage")

    monkeypatch.setitem(jobs.HANDLERS, JobType.DOCUMENT_INDEXING, boom)
    job = jobs.enqueue(db, JobType.DOCUMENT_INDEXING, "retry-test", {})
    db.commit()
    job_id = job.id
    for expected_attempt in (1, 2, 3):
        jobs.drain(db)
        row = db.query(BackgroundJob).get(job_id)
        db.refresh(row)
        assert row.attempts == expected_attempt and "transient outage" in row.last_error
        if expected_attempt < 3:
            assert row.status == JobStatus.PENDING and row.run_after > utcnow()  # back-off scheduled
            row.run_after = utcnow() - timedelta(seconds=1)
            db.commit()
    assert row.status == JobStatus.FAILED and calls["n"] == 3
    jobs.drain(db)
    assert calls["n"] == 3  # failed jobs are not picked up again


def test_duplicate_jobs_are_not_created(env, db):
    a = jobs.enqueue(db, JobType.EXPIRATION_SCAN, "same-key", {})
    b = jobs.enqueue(db, JobType.EXPIRATION_SCAN, "same-key", {})
    db.commit()
    assert a.id == b.id
    assert db.query(BackgroundJob).filter_by(dedupe_key="same-key").count() == 1
    jobs.drain(db)
    c = jobs.enqueue(db, JobType.EXPIRATION_SCAN, "same-key", {})  # allowed again once the first finished
    db.commit()
    assert c.id != a.id


def test_job_failure_does_not_roll_back_workflow_state(env, db, monkeypatch):
    def boom(_db, _payload):
        raise RuntimeError("worker crashed")

    monkeypatch.setitem(jobs.HANDLERS, JobType.DOCUMENT_INDEXING, boom)
    monkeypatch.setitem(jobs.HANDLERS, JobType.CHECKSUM_VERIFY, boom)
    doc = ready_doc(env)
    assert submit(env, doc).json()["status"] == "UNDER_REVIEW"  # critical operations stay consistent


def test_checksum_mismatch_is_permanent_failure(env, db):
    from app.models import DocumentFile
    from app.storage.local import LocalStorage
    doc = make_doc(env)
    v = upload_version(env.employee, doc["id"]).json()
    f = db.get(DocumentFile, v["file_id"])
    LocalStorage().path_for(f.storage_key).write_bytes(b"tampered")
    job = jobs.enqueue(db, JobType.CHECKSUM_VERIFY, "tamper", {"file_id": f.id})
    db.commit()
    jobs.drain(db)
    db.refresh(job)
    assert job.status == JobStatus.FAILED and job.attempts == 1 and "checksum mismatch" in job.last_error


def test_jobs_endpoints_admin_only(env):
    assert env.employee.get("/api/jobs").status_code == 403
    assert env.admin.get("/api/jobs").status_code == 200
    r = env.admin.post("/api/jobs/run-scheduled")
    assert r.status_code == 202 and len(r.json()["enqueued_job_ids"]) == 4


def test_notifications_read_flow(env):
    doc = ready_doc(env)
    submit(env, doc)
    items = env.employee.get("/api/notifications?unread_only=true").json()["items"]
    assert items
    assert env.employee.patch(f"/api/notifications/{items[0]['id']}/read").json()["is_read"] is True
    assert env.rev1.patch(f"/api/notifications/{items[0]['id']}/read").status_code == 404  # not theirs
    assert env.employee.patch("/api/notifications/read-all").json()["updated"] >= 0
    assert env.employee.get("/api/notifications?unread_only=true").json()["total"] == 0
