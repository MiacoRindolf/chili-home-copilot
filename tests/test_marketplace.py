"""Comprehensive tests for the Marketplace module: router, service, and model."""
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
import json
import zipfile
import tempfile

import pytest
from app.models import MarketplaceModule, User, Device
from app.services import marketplace_service
from app.pairing import DEVICE_COOKIE_NAME


def _make_paired(db):
    """Create a paired user+device and return (user, token)."""
    user = User(name="MktUser")
    db.add(user)
    db.commit()
    db.refresh(user)
    token = "mkt-test-tok"
    db.add(Device(token=token, user_id=user.id, label="test", client_ip_last="127.0.0.1"))
    db.commit()
    return user, token


def _seed_module(db, slug="test-mod", name="Test Module", enabled=True, version="1.0.0"):
    """Insert a MarketplaceModule directly into the DB."""
    mod = MarketplaceModule(
        slug=slug,
        name=name,
        version=version,
        summary="A test module",
        description="",
        local_path="/tmp/fake/path",
        source="registry",
        enabled=enabled,
        installed_at=datetime.utcnow(),
    )
    db.add(mod)
    db.commit()
    db.refresh(mod)
    return mod


def _create_test_zip(tmp_dir: Path, mod_name: str = "test-mod") -> Path:
    """Create a minimal valid module zip with a single top-level directory."""
    zip_path = tmp_dir / "test.zip"
    mod_dir = mod_name
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(f"{mod_dir}/chili_module.yaml", "name: test\nversion: 1.0.0\n")
        zf.writestr(f"{mod_dir}/__init__.py", "")
    return zip_path


# ── Model Tests ──────────────────────────────────────────────────────────────


class TestMarketplaceModel:
    def test_create_module(self, db):
        mod = _seed_module(db)
        assert mod.id is not None
        assert mod.slug == "test-mod"
        assert mod.enabled is True

    def test_unique_slug(self, db):
        _seed_module(db, slug="unique-mod")
        with pytest.raises(Exception):
            _seed_module(db, slug="unique-mod")

    def test_local_path_obj(self, db):
        mod = _seed_module(db)
        assert isinstance(mod.local_path_obj(), Path)

    def test_installed_at_default(self, db):
        mod = _seed_module(db)
        assert mod.installed_at is not None

    def test_disable_module(self, db):
        mod = _seed_module(db, enabled=True)
        mod.enabled = False
        db.commit()
        db.refresh(mod)
        assert mod.enabled is False


# ── Service Tests ────────────────────────────────────────────────────────────


class TestMarketplaceServiceParsing:
    def test_parse_valid_registry(self):
        index = {
            "modules": [
                {
                    "slug": "hello",
                    "name": "Hello Module",
                    "summary": "Greets you",
                    "version": "1.2.0",
                    "tags": ["productivity"],
                    "download_url": "https://example.com/hello.zip",
                },
            ]
        }
        parsed = marketplace_service._parse_registry_modules(index)
        assert len(parsed) == 1
        assert parsed[0].slug == "hello"
        assert parsed[0].name == "Hello Module"
        assert parsed[0].version == "1.2.0"
        assert parsed[0].tags == ["productivity"]

    def test_parse_empty_registry(self):
        parsed = marketplace_service._parse_registry_modules({"modules": []})
        assert parsed == []

    def test_parse_missing_modules_key(self):
        parsed = marketplace_service._parse_registry_modules({})
        assert parsed == []

    def test_parse_malformed_entry_skipped(self):
        index = {
            "modules": [
                {"no_slug": True},
                {"slug": "valid", "name": "OK", "summary": "Fine", "version": "1.0.0"},
            ]
        }
        parsed = marketplace_service._parse_registry_modules(index)
        assert len(parsed) == 1
        assert parsed[0].slug == "valid"


class TestMarketplaceServiceListStatus:
    @patch("app.services.marketplace_service._fetch_registry_index")
    def test_list_registry_empty(self, mock_fetch, db):
        mock_fetch.return_value = {"modules": []}
        items = marketplace_service.list_registry_with_status(db, "test")
        assert items == []

    @patch("app.services.marketplace_service._fetch_registry_index")
    def test_list_merges_installed(self, mock_fetch, db):
        mock_fetch.return_value = {
            "modules": [
                {"slug": "alpha", "name": "Alpha", "summary": "First", "version": "2.0.0"},
            ]
        }
        _seed_module(db, slug="alpha", version="1.0.0", enabled=True)
        items = marketplace_service.list_registry_with_status(db, "test")
        assert len(items) == 1
        assert items[0]["installed"] is True
        assert items[0]["enabled"] is True
        assert items[0]["installed_version"] == "1.0.0"
        assert items[0]["has_update"] is True

    @patch("app.services.marketplace_service._fetch_registry_index")
    def test_list_includes_local_only_modules(self, mock_fetch, db):
        mock_fetch.return_value = {"modules": []}
        _seed_module(db, slug="local-only", enabled=False)
        items = marketplace_service.list_registry_with_status(db, "test")
        assert len(items) == 1
        assert items[0]["slug"] == "local-only"
        assert items[0]["installed"] is True
        assert items[0]["enabled"] is False


class TestMarketplaceServiceEnableDisable:
    def test_enable_module(self, db):
        _seed_module(db, slug="toggle-mod", enabled=False)
        mod = marketplace_service.set_enabled(db, "toggle-mod", True)
        assert mod.enabled is True

    def test_disable_module(self, db):
        _seed_module(db, slug="toggle-mod", enabled=True)
        mod = marketplace_service.set_enabled(db, "toggle-mod", False)
        assert mod.enabled is False

    def test_enable_nonexistent_raises(self, db):
        with pytest.raises(ValueError, match="not installed"):
            marketplace_service.set_enabled(db, "no-such-mod", True)


class TestMarketplaceServiceUninstall:
    def test_uninstall_existing(self, db):
        _seed_module(db, slug="removable")
        marketplace_service.uninstall(db, "removable")
        assert db.query(MarketplaceModule).filter(MarketplaceModule.slug == "removable").first() is None

    def test_uninstall_nonexistent_is_noop(self, db):
        marketplace_service.uninstall(db, "ghost-mod")

    def test_uninstall_cleans_files(self, db, tmp_path):
        mod_dir = tmp_path / "removable"
        mod_dir.mkdir()
        (mod_dir / "file.txt").write_text("content")
        mod = _seed_module(db, slug="removable")
        mod.local_path = str(mod_dir)
        db.commit()

        marketplace_service.uninstall(db, "removable")
        assert not mod_dir.exists()


class TestSafeExtractZip:
    def test_valid_zip(self, tmp_path):
        zip_path = _create_test_zip(tmp_path)
        dest = tmp_path / "extracted"
        dest.mkdir()
        result = marketplace_service._safe_extract_zip(zip_path, dest)
        assert result.exists()
        assert (result / "chili_module.yaml").exists()

    def test_empty_zip_raises(self, tmp_path):
        zip_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(zip_path, "w"):
            pass
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ValueError, match="empty"):
            marketplace_service._safe_extract_zip(zip_path, dest)

    def test_multiple_top_dirs_raises(self, tmp_path):
        zip_path = tmp_path / "multi.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("dir_a/file.txt", "a")
            zf.writestr("dir_b/file.txt", "b")
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ValueError, match="single top-level"):
            marketplace_service._safe_extract_zip(zip_path, dest)

    def test_path_traversal_raises(self, tmp_path):
        zip_path = tmp_path / "evil.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../etc/passwd", "bad")
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(ValueError, match="Unsafe"):
            marketplace_service._safe_extract_zip(zip_path, dest)


class TestVerifyChecksum:
    def test_no_checksum_passes(self, tmp_path):
        f = tmp_path / "file.bin"
        f.write_bytes(b"data")
        assert marketplace_service._verify_checksum(f, None) is True

    def test_valid_checksum(self, tmp_path):
        import hashlib
        f = tmp_path / "file.bin"
        f.write_bytes(b"hello world")
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert marketplace_service._verify_checksum(f, expected) is True

    def test_invalid_checksum(self, tmp_path):
        f = tmp_path / "file.bin"
        f.write_bytes(b"hello world")
        assert marketplace_service._verify_checksum(f, "bad_hash") is False


# ── Router / API Tests ───────────────────────────────────────────────────────


class TestMarketplacePage:
    def test_guest_sees_pair_required(self, client):
        resp = client.get("/marketplace")
        assert resp.status_code == 200
        assert "Pair" in resp.text or "pair" in resp.text

    def test_paired_user_sees_marketplace(self, db, client):
        _, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.get("/marketplace")
        assert resp.status_code == 200
        assert "Marketplace" in resp.text


class TestMarketplaceListAPI:
    @patch("app.services.marketplace_service._fetch_registry_index")
    def test_list_modules(self, mock_fetch, db, client):
        mock_fetch.return_value = {"modules": [
            {"slug": "mod-a", "name": "Mod A", "summary": "Test", "version": "1.0.0"},
        ]}
        resp = client.get("/api/marketplace/modules")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["slug"] == "mod-a"

    @patch("app.services.marketplace_service._fetch_registry_index")
    def test_list_empty(self, mock_fetch, db, client):
        mock_fetch.return_value = {"modules": []}
        resp = client.get("/api/marketplace/modules")
        assert resp.status_code == 200
        assert resp.json() == []


class TestMarketplaceEnableAPI:
    def test_enable_requires_pairing(self, client):
        resp = client.post("/api/marketplace/enable", json={"slug": "x"})
        assert resp.status_code == 403

    def test_enable_nonexistent_returns_404(self, db, client):
        _, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/marketplace/enable", json={"slug": "nonexistent"})
        assert resp.status_code == 404

    def test_enable_success(self, db, client):
        _, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        _seed_module(db, slug="en-mod", enabled=False)
        resp = client.post("/api/marketplace/enable", json={"slug": "en-mod"})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    def test_enable_bad_slug_rejected(self, db, client):
        _, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/marketplace/enable", json={"slug": "INVALID SLUG!"})
        assert resp.status_code == 422


class TestMarketplaceDisableAPI:
    def test_disable_requires_pairing(self, client):
        resp = client.post("/api/marketplace/disable", json={"slug": "x"})
        assert resp.status_code == 403

    def test_disable_success(self, db, client):
        _, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        _seed_module(db, slug="dis-mod", enabled=True)
        resp = client.post("/api/marketplace/disable", json={"slug": "dis-mod"})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False


class TestMarketplaceInstallAPI:
    def test_install_requires_pairing(self, client):
        resp = client.post("/api/marketplace/install", json={"slug": "x"})
        assert resp.status_code == 403

    @patch("app.services.marketplace_service._fetch_registry_index")
    def test_install_not_in_registry(self, mock_fetch, db, client):
        _, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        mock_fetch.return_value = {"modules": []}
        resp = client.post("/api/marketplace/install", json={"slug": "missing-mod"})
        assert resp.status_code == 400

    def test_install_empty_slug(self, db, client):
        _, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.post("/api/marketplace/install", json={"slug": ""})
        assert resp.status_code == 422


class TestMarketplaceUninstallAPI:
    def test_uninstall_requires_pairing(self, client):
        resp = client.delete("/api/marketplace/modules/some-mod")
        assert resp.status_code == 403

    def test_uninstall_nonexistent_is_204(self, db, client):
        _, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        resp = client.delete("/api/marketplace/modules/ghost")
        assert resp.status_code == 204

    def test_uninstall_success(self, db, client):
        _, token = _make_paired(db)
        client.cookies.set(DEVICE_COOKIE_NAME, token)
        _seed_module(db, slug="del-mod")
        resp = client.delete("/api/marketplace/modules/del-mod")
        assert resp.status_code == 204
        assert db.query(MarketplaceModule).filter(MarketplaceModule.slug == "del-mod").first() is None
