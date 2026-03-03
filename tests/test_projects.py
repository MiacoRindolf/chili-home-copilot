"""Tests for Project Space feature: CRUD, file handling, conversation assignment, RAG."""
import io
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from app.models import Project, ProjectFile, Conversation, User, Device
from app.services import project_file_service as pfs
from app.pairing import DEVICE_COOKIE_NAME


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_paired(db):
    """Create a paired user+device and return (user, token)."""
    user = User(name="ProjUser")
    db.add(user)
    db.commit()
    db.refresh(user)
    token = "proj-test-tok-abc"
    db.add(Device(token=token, user_id=user.id, label="test", client_ip_last="127.0.0.1"))
    db.commit()
    return user, token


# ── File validation tests ────────────────────────────────────────────────────

class TestFileValidation:
    def test_valid_text_file(self):
        assert pfs.validate_file("readme.txt", 100) is None

    def test_valid_pdf(self):
        assert pfs.validate_file("doc.pdf", 5000) is None

    def test_valid_code_file(self):
        assert pfs.validate_file("main.py", 200) is None

    def test_valid_image(self):
        assert pfs.validate_file("photo.png", 1024) is None

    def test_unsupported_extension(self):
        err = pfs.validate_file("archive.zip", 100)
        assert err is not None
        assert "Unsupported" in err

    def test_file_too_large(self):
        err = pfs.validate_file("big.txt", 15 * 1024 * 1024)
        assert err is not None
        assert "too large" in err

    def test_exact_max_size(self):
        assert pfs.validate_file("ok.txt", 10 * 1024 * 1024) is None


# ── Text extraction tests ───────────────────────────────────────────────────

class TestTextExtraction:
    def test_extract_text_file(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("Hello world", encoding="utf-8")
        result = pfs.extract_text(f, "text/plain")
        assert result == "Hello world"

    def test_extract_code_file(self, tmp_path):
        f = tmp_path / "test.py"
        f.write_text("print('hi')", encoding="utf-8")
        result = pfs.extract_text(f, "text/x-python")
        assert "print" in result

    def test_extract_unknown_extension(self, tmp_path):
        f = tmp_path / "test.xyz"
        f.write_text("data", encoding="utf-8")
        result = pfs.extract_text(f, "application/octet-stream")
        assert result == ""

    @patch("app.services.project_file_service._extract_pdf")
    def test_extract_pdf_delegated(self, mock_pdf, tmp_path):
        mock_pdf.return_value = "PDF content"
        f = tmp_path / "test.pdf"
        f.write_bytes(b"%PDF-fake")
        result = pfs.extract_text(f, "application/pdf")
        assert result == "PDF content"

    def test_extract_csv(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("a,b,c\n1,2,3", encoding="utf-8")
        result = pfs.extract_text(f, "text/csv")
        assert "a,b,c" in result


# ── Project CRUD API tests ──────────────────────────────────────────────────

class TestProjectCRUD:
    def test_list_projects_guest(self, client):
        resp = client.get("/api/projects")
        assert resp.status_code == 200
        assert resp.json()["projects"] == []

    def test_create_project(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/projects", data={"name": "Test Proj", "description": "desc", "color": "#ef4444"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Proj"
        assert data["description"] == "desc"
        assert data["color"] == "#ef4444"
        assert data["id"] > 0

    def test_create_project_guest_forbidden(self, client):
        resp = client.post("/api/projects", data={"name": "X"})
        assert resp.status_code == 403

    def test_list_projects_paired(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        client.post("/api/projects", data={"name": "P1"})
        client.post("/api/projects", data={"name": "P2"})
        resp = client.get("/api/projects")
        data = resp.json()
        assert len(data["projects"]) == 2
        names = {p["name"] for p in data["projects"]}
        assert "P1" in names
        assert "P2" in names

    def test_update_project(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/projects", data={"name": "Old"})
        pid = resp.json()["id"]
        resp2 = client.put(f"/api/projects/{pid}", data={"name": "New", "color": "#10b981"})
        assert resp2.status_code == 200
        assert resp2.json()["name"] == "New"
        assert resp2.json()["color"] == "#10b981"

    def test_update_nonexistent(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.put("/api/projects/9999", data={"name": "X"})
        assert resp.status_code == 404

    def test_delete_project(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/projects", data={"name": "Del"})
        pid = resp.json()["id"]

        with patch("app.services.project_file_service.remove_project_collection"):
            resp2 = client.delete(f"/api/projects/{pid}")
        assert resp2.status_code == 200
        assert resp2.json()["ok"] is True

        resp3 = client.get("/api/projects")
        assert len(resp3.json()["projects"]) == 0

    def test_delete_unlinks_conversations(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/projects", data={"name": "P"})
        pid = resp.json()["id"]

        convo = Conversation(convo_key=f"user:{user.id}", title="Chat", project_id=pid)
        db.add(convo)
        db.commit()
        db.refresh(convo)

        with patch("app.services.project_file_service.remove_project_collection"):
            client.delete(f"/api/projects/{pid}")

        db.refresh(convo)
        assert convo.project_id is None


# ── Conversation Assignment tests ────────────────────────────────────────────

class TestConversationAssignment:
    def test_assign_conversation(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/projects", data={"name": "P"})
        pid = resp.json()["id"]

        convo = Conversation(convo_key=f"user:{user.id}", title="Chat")
        db.add(convo)
        db.commit()
        db.refresh(convo)

        resp2 = client.post(f"/api/projects/{pid}/conversations/{convo.id}")
        assert resp2.status_code == 200
        db.refresh(convo)
        assert convo.project_id == pid

    def test_unassign_conversation(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/projects", data={"name": "P"})
        pid = resp.json()["id"]

        convo = Conversation(convo_key=f"user:{user.id}", title="Chat", project_id=pid)
        db.add(convo)
        db.commit()
        db.refresh(convo)

        resp2 = client.delete(f"/api/projects/{pid}/conversations/{convo.id}")
        assert resp2.status_code == 200
        db.refresh(convo)
        assert convo.project_id is None

    def test_assign_wrong_project(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        convo = Conversation(convo_key=f"user:{user.id}", title="Chat")
        db.add(convo)
        db.commit()
        db.refresh(convo)

        resp = client.post(f"/api/projects/9999/conversations/{convo.id}")
        assert resp.status_code == 404


# ── Conversations API includes project_id ────────────────────────────────────

class TestConversationProjectField:
    def test_conversations_include_project_id(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)

        proj = Project(user_id=user.id, name="Test")
        db.add(proj)
        db.commit()
        db.refresh(proj)

        convo = Conversation(convo_key=f"user:{user.id}", title="In Proj", project_id=proj.id)
        db.add(convo)
        db.commit()

        resp = client.get("/api/conversations")
        convos = resp.json()["conversations"]
        found = [c for c in convos if c["title"] == "In Proj"]
        assert len(found) == 1
        assert found[0]["project_id"] == proj.id


# ── File upload/delete API tests ─────────────────────────────────────────────

class TestFileUploadAPI:
    @patch("app.services.project_file_service.ingest_file")
    def test_upload_file(self, mock_ingest, db, client):
        mock_ingest.return_value = {"ok": True, "chunks": 3}
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/projects", data={"name": "P"})
        pid = resp.json()["id"]

        file_content = b"Hello, this is a test file."
        resp2 = client.post(
            f"/api/projects/{pid}/files",
            files=[("files", ("test.txt", io.BytesIO(file_content), "text/plain"))],
        )
        assert resp2.status_code == 200
        results = resp2.json()["results"]
        assert len(results) == 1
        assert results[0]["ok"] is True
        assert results[0]["name"] == "test.txt"

    def test_upload_invalid_type(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/projects", data={"name": "P"})
        pid = resp.json()["id"]

        resp2 = client.post(
            f"/api/projects/{pid}/files",
            files=[("files", ("bad.exe", io.BytesIO(b"MZ"), "application/octet-stream"))],
        )
        assert resp2.status_code == 200
        results = resp2.json()["results"]
        assert results[0]["ok"] is False
        assert "Unsupported" in results[0]["error"]

    def test_list_files(self, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/projects", data={"name": "P"})
        pid = resp.json()["id"]

        pf = ProjectFile(project_id=pid, original_name="test.txt", stored_name="abc.txt", content_type="text/plain", file_size=100)
        db.add(pf)
        db.commit()

        resp2 = client.get(f"/api/projects/{pid}/files")
        assert resp2.status_code == 200
        files = resp2.json()["files"]
        assert len(files) == 1
        assert files[0]["original_name"] == "test.txt"

    @patch("app.services.project_file_service.remove_file")
    def test_delete_file(self, mock_remove, db, client):
        user, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/projects", data={"name": "P"})
        pid = resp.json()["id"]

        pf = ProjectFile(project_id=pid, original_name="del.txt", stored_name="del.txt", content_type="text/plain", file_size=50)
        db.add(pf)
        db.commit()
        db.refresh(pf)

        resp2 = client.delete(f"/api/projects/{pid}/files/{pf.id}")
        assert resp2.status_code == 200
        mock_remove.assert_called_once()


# ── Model tests ──────────────────────────────────────────────────────────────

class TestModels:
    def test_project_creation(self, db):
        user = User(name="ModelUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        proj = Project(user_id=user.id, name="MyProj", color="#10b981")
        db.add(proj)
        db.commit()
        db.refresh(proj)

        assert proj.id is not None
        assert proj.name == "MyProj"
        assert proj.color == "#10b981"

    def test_project_file_relationship(self, db):
        user = User(name="RelUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        proj = Project(user_id=user.id, name="P")
        db.add(proj)
        db.commit()
        db.refresh(proj)

        pf = ProjectFile(project_id=proj.id, original_name="f.txt", stored_name="s.txt", content_type="text/plain")
        db.add(pf)
        db.commit()

        db.refresh(proj)
        assert len(proj.files) == 1
        assert proj.files[0].original_name == "f.txt"

    def test_conversation_project_link(self, db):
        user = User(name="LinkUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        proj = Project(user_id=user.id, name="P")
        db.add(proj)
        db.commit()
        db.refresh(proj)

        convo = Conversation(convo_key=f"user:{user.id}", title="Chat", project_id=proj.id)
        db.add(convo)
        db.commit()
        db.refresh(convo)

        assert convo.project_id == proj.id
        assert convo.project.name == "P"

    def test_cascade_delete_files(self, db):
        user = User(name="CascadeUser")
        db.add(user)
        db.commit()
        db.refresh(user)

        proj = Project(user_id=user.id, name="P")
        db.add(proj)
        db.commit()
        db.refresh(proj)

        pf = ProjectFile(project_id=proj.id, original_name="f.txt", stored_name="s.txt", content_type="text/plain")
        db.add(pf)
        db.commit()

        db.delete(proj)
        db.commit()

        remaining = db.query(ProjectFile).filter(ProjectFile.project_id == proj.id).all()
        assert len(remaining) == 0


# ── RAG integration tests ───────────────────────────────────────────────────

class TestRAGIntegration:
    @patch("app.rag.chromadb")
    def test_get_project_collection(self, mock_chromadb):
        from app.rag import get_project_collection
        mock_client = MagicMock()
        mock_chromadb.PersistentClient.return_value = mock_client
        mock_collection = MagicMock()
        mock_client.get_or_create_collection.return_value = mock_collection

        result = get_project_collection(42)
        mock_client.get_or_create_collection.assert_called_once()
        call_kwargs = mock_client.get_or_create_collection.call_args
        assert "project_42" in str(call_kwargs)

    @patch("app.rag.chromadb")
    def test_get_project_collection_readonly(self, mock_chromadb):
        from app.rag import get_project_collection
        mock_client = MagicMock()
        mock_chromadb.PersistentClient.return_value = mock_client
        mock_client.get_collection.side_effect = Exception("not found")

        result = get_project_collection(99, read_only=True)
        assert result is None

    @patch("app.rag.chromadb")
    def test_delete_project_collection(self, mock_chromadb):
        from app.rag import delete_project_collection
        mock_client = MagicMock()
        mock_chromadb.PersistentClient.return_value = mock_client

        delete_project_collection(42, "test")
        mock_client.delete_collection.assert_called_once_with(name="project_42")


# ── Chat service integration test ────────────────────────────────────────────

class TestChatServiceProjectRAG:
    @patch("app.services.project_file_service.search_project")
    @patch("app.rag.search")
    @patch("app.services.chat_service.plan_action")
    @patch("app.memory.get_memory_context")
    @patch("app.personality.get_profile_context")
    def test_project_rag_injected(self, mock_profile, mock_memory, mock_plan, mock_rag, mock_proj_search):
        from app.services.chat_service import plan_and_enrich
        mock_rag.return_value = []
        mock_proj_search.return_value = [
            {"text": "Project doc content", "source": "readme.md", "distance": 0.5}
        ]
        mock_profile.return_value = None
        mock_memory.return_value = None
        mock_plan.return_value = {"type": "unknown", "data": {}, "reply": "test"}

        class FakeMsg:
            role = "user"
            content = "hello"

        db_mock = MagicMock()
        result = plan_and_enrich(
            db_mock, "hello",
            {"is_guest": False, "user_id": 1},
            [FakeMsg()], "trace", project_id=7,
        )
        mock_proj_search.assert_called_once_with(7, "hello", n_results=3, trace_id="trace")
        assert result["rag_context"] is not None
        assert "readme.md" in result["rag_context"]
