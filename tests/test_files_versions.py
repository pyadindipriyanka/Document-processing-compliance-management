import io
import threading
import zipfile

import pytest

from app.core.config import get_settings
from tests.conftest import IS_POSTGRES, PDF, make_doc, ready_doc, upload_version

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_upload_attachment_ignores_client_name_and_mime(env):
    doc = make_doc(env)
    r = env.employee.post(f"/api/documents/{doc['id']}/files",
                          files={"file": ("../../evil.exe", PDF, "image/gif")})
    assert r.status_code == 201
    body = r.json()
    assert body["content_type"] == "application/pdf"  # sniffed from content, not trusted from client
    assert body["original_filename"] == "evil.exe"  # path components stripped
    assert len(body["checksum_sha256"]) == 64


@pytest.mark.parametrize("name,content", [("a.pdf", b"MZ\x90\x00\x03\x00\x00\x00binary"), ("b.zip", b"PK\x03\x04junk"),
                                          ("c.pdf", b"\x00\x01\x02\x03\x04")])
def test_disallowed_content_rejected(env, name, content):
    doc = make_doc(env)
    r = env.employee.post(f"/api/documents/{doc['id']}/files", files={"file": (name, content, "application/pdf")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "FILE_TYPE_NOT_ALLOWED"
    assert env.employee.get(f"/api/documents/{doc['id']}/files").json() == []  # no inconsistent DB record


def test_office_zip_with_docx_structure_is_accepted(env):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w/>")
        zf.writestr("[Content_Types].xml", "<Types/>")
    doc = make_doc(env)
    r = env.employee.post(f"/api/documents/{doc['id']}/files", files={"file": ("x.bin", buf.getvalue(), "text/plain")})
    assert r.status_code == 201 and r.json()["content_type"].endswith("wordprocessingml.document")


def test_file_too_large_and_empty(env, monkeypatch):
    doc = make_doc(env)
    assert env.employee.post(f"/api/documents/{doc['id']}/files",
                             files={"file": ("e.pdf", b"", "application/pdf")}).json()["error"]["code"] == "FILE_EMPTY"
    monkeypatch.setattr(get_settings(), "MAX_UPLOAD_SIZE_MB", 0)
    r = env.employee.post(f"/api/documents/{doc['id']}/files", files={"file": ("e.pdf", PDF, "application/pdf")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_unauthorized_file_download(env):
    doc = make_doc(env, access_level="PRIVATE")
    f = env.employee.post(f"/api/documents/{doc['id']}/files", files={"file": ("a.pdf", PDF, "application/pdf")}).json()
    assert env.employee.get(f"/api/files/{f['id']}/download").content == PDF
    r = env.manager.get(f"/api/files/{f['id']}/download")
    assert r.status_code == 403 and r.json()["error"]["code"] == "DOCUMENT_ACCESS_DENIED"
    assert env.other.get(f"/api/files/{f['id']}/download").status_code == 404
    assert env.rev1.get(f"/api/files/{f['id']}/download").status_code == 403


def test_delete_attachment_but_not_version_file(env):
    doc = make_doc(env)
    f = env.employee.post(f"/api/documents/{doc['id']}/files", files={"file": ("a.pdf", PDF, "application/pdf")}).json()
    assert env.employee2.delete(f"/api/files/{f['id']}").status_code == 403
    assert env.employee.delete(f"/api/files/{f['id']}").status_code == 204
    assert env.employee.get(f"/api/files/{f['id']}/download").status_code == 404
    v = upload_version(env.employee, doc["id"]).json()
    r = env.employee.delete(f"/api/files/{v['file_id']}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "FILE_IN_USE"


def test_version_numbers_are_sequential_and_restore_creates_new_version(env):
    doc = make_doc(env)
    v1 = upload_version(env.employee, doc["id"], description="first").json()
    v2 = upload_version(env.employee, doc["id"], PDF + b"more", description="second").json()
    assert (v1["version_number"], v2["version_number"]) == (1, 2)
    assert v1["checksum_sha256"] != v2["checksum_sha256"]
    r = env.employee.post(f"/api/documents/{doc['id']}/versions/{v1['id']}/restore")
    assert r.status_code == 201
    v3 = r.json()
    assert v3["version_number"] == 3 and v3["restored_from_version_id"] == v1["id"]
    assert v3["checksum_sha256"] == v1["checksum_sha256"]
    versions = env.employee.get(f"/api/documents/{doc['id']}/versions").json()
    assert [v["version_number"] for v in versions] == [1, 2, 3]
    assert env.employee.get(f"/api/documents/{doc['id']}").json()["current_version"] == 3
    assert env.employee.get(f"/api/documents/{doc['id']}/versions/999999").status_code == 404


def test_invalid_upload_does_not_consume_version_number(env):
    doc = make_doc(env)
    bad = upload_version(env.employee, doc["id"], b"\x00\x00\x00")
    assert bad.status_code == 400
    assert upload_version(env.employee, doc["id"]).json()["version_number"] == 1


def test_version_upload_permissions(env):
    doc = ready_doc(env)
    assert upload_version(env.employee2, doc["id"]).status_code == 403
    assert upload_version(env.auditor, doc["id"]).status_code == 403
    assert upload_version(env.other, doc["id"]).status_code == 404


def test_cannot_version_archived_document(env):
    doc = ready_doc(env)
    env.employee.post(f"/api/documents/{doc['id']}/archive")
    r = upload_version(env.employee, doc["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "DOCUMENT_ARCHIVED"


@pytest.mark.skipif(not IS_POSTGRES, reason="needs PostgreSQL row locking (set TEST_DATABASE_URL)")
def test_concurrent_version_uploads_never_conflict(env):
    doc = make_doc(env)
    results = []

    def worker(i):
        results.append(upload_version(env.employee, doc["id"], PDF + bytes([i]), description=f"t{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    numbers = sorted(r.json()["version_number"] for r in results)
    assert numbers == list(range(1, 9))
