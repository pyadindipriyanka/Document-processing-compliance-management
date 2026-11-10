import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="docplatform-test-")
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_tmp}/test.db"
os.environ["STORAGE_DIR"] = os.path.join(_tmp, "storage")
os.environ["SECRET_KEY"] = "test-secret-key-test-secret-key-1234"
os.environ["BCRYPT_ROUNDS"] = "4"
os.environ["SUPER_ADMIN_EMAIL"] = "root@platform.test"
os.environ["EMAIL_ENABLED"] = "false"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.models  # noqa: E402,F401
from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PASSWORD = "Passw0rd1"
IS_POSTGRES = engine.dialect.name == "postgresql"


@pytest.fixture(autouse=True)
def _fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()


class Actor:
    def __init__(self, client, email, user_id, tokens):
        self.client, self.email, self.id, self.tokens = client, email, user_id, tokens

    @property
    def h(self):
        return {"Authorization": f"Bearer {self.tokens['access_token']}"}

    def get(self, url, **kw):
        return self.client.get(url, headers=self.h, **kw)

    def post(self, url, **kw):
        return self.client.post(url, headers=self.h, **kw)

    def patch(self, url, **kw):
        return self.client.patch(url, headers=self.h, **kw)

    def delete(self, url, **kw):
        return self.client.delete(url, headers=self.h, **kw)


def signup(client, email) -> Actor:
    r = client.post("/api/auth/register", json={"email": email, "password": PASSWORD, "full_name": email.split("@")[0]})
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return Actor(client, email, uid, r.json())


class Env:
    pass


@pytest.fixture
def env(client):
    e = Env()
    e.client = client
    e.admin = signup(client, "admin@acme.test")
    org = e.admin.post("/api/organizations", json={"name": "Acme"}).json()
    e.org_id = org["id"]
    for name, role in [("manager", "document_manager"), ("employee", "employee"), ("employee2", "employee"),
                       ("rev1", "reviewer"), ("rev2", "reviewer"), ("auditor", "auditor")]:
        actor = signup(client, f"{name}@acme.test")
        setattr(e, name, actor)
        r = e.admin.post(f"/api/organizations/{e.org_id}/members", json={"email": actor.email, "role": role})
        assert r.status_code == 201, r.text
    e.other = signup(client, "admin@other.test")
    e.other_org = e.other.post("/api/organizations", json={"name": "Other Corp"}).json()["id"]

    cat = e.admin.post("/api/categories", json={"name": "Contracts", "metadata_definitions": [
        {"key": "contract_number", "required": True},
        {"key": "expiration_date", "field_type": "date"},
        {"key": "vendor"}]})
    assert cat.status_code == 201, cat.text
    e.category = cat.json()["id"]
    wf = e.admin.post("/api/workflows", json={"name": "Contract approval", "category_ids": [e.category], "stages": [
        {"name": "Department review", "reviewer_ids": [e.rev1.id]},
        {"name": "Compliance review", "reviewer_ids": [e.rev2.id]}]})
    assert wf.status_code == 201, wf.text
    e.workflow = wf.json()["id"]
    assert e.admin.post(f"/api/workflows/{e.workflow}/activate").status_code == 200
    return e


def make_doc(env, who=None, **over):
    who = who or env.employee
    body = {"title": "Master Services Agreement", "category_id": env.category,
            "metadata": {"contract_number": "C-1001", "vendor": "Globex"}}
    body.update(over)
    r = who.post("/api/documents", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def upload_version(who, doc_id, content=PDF, name="contract.pdf", description="v", material=True):
    return who.post(f"/api/documents/{doc_id}/versions", files={"file": (name, content, "application/pdf")},
                    data={"change_description": description, "is_material": str(material).lower()})


def ready_doc(env, who=None, **over):
    who = who or env.employee
    doc = make_doc(env, who, **over)
    assert upload_version(who, doc["id"]).status_code == 201
    return doc


def submit(env, doc, who=None):
    return (who or env.employee).post(f"/api/documents/{doc['id']}/submit")


def pending(actor):
    return actor.get("/api/reviews/pending").json()["items"]


def approve_all(env, doc):
    for rev in (env.rev1, env.rev2):
        items = [i for i in pending(rev) if i["document_id"] == doc["id"]]
        assert items, "expected a pending review"
        assert rev.post(f"/api/reviews/{items[0]['id']}/approve", json={"comment": "ok"}).status_code == 200


def approved_doc(env, **over):
    doc = ready_doc(env, **over)
    assert submit(env, doc).status_code == 200
    approve_all(env, doc)
    return doc
