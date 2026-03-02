"""Tests for the RAG module: chunking logic and search."""
import pytest
from unittest.mock import patch, MagicMock

from app.rag import chunk_text, search, ingest_documents, CHUNK_MAX_CHARS


class TestChunking:
    def test_single_short_paragraph(self):
        text = "WiFi password is spicypepper2026."
        chunks = chunk_text(text, source="test.txt")
        assert len(chunks) == 1
        assert chunks[0]["text"] == text
        assert chunks[0]["source"] == "test.txt"
        assert chunks[0]["id"] == "test.txt::chunk0"

    def test_multiple_paragraphs_under_limit(self):
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = chunk_text(text, source="test.txt")
        assert len(chunks) == 1
        assert "Paragraph one." in chunks[0]["text"]
        assert "Paragraph three." in chunks[0]["text"]

    def test_long_text_splits_into_multiple_chunks(self):
        paragraphs = [f"This is paragraph number {i} with some filler content to take up space." for i in range(20)]
        text = "\n\n".join(paragraphs)
        chunks = chunk_text(text, source="long.txt")

        assert len(chunks) > 1
        for c in chunks:
            assert len(c["text"]) <= CHUNK_MAX_CHARS + 100  # allow some overflow for last paragraph
            assert c["source"] == "long.txt"

        all_text = " ".join(c["text"] for c in chunks)
        assert "paragraph number 0" in all_text
        assert "paragraph number 19" in all_text

    def test_empty_text(self):
        chunks = chunk_text("", source="empty.txt")
        assert chunks == []

    def test_whitespace_only(self):
        chunks = chunk_text("   \n\n   \n\n   ", source="ws.txt")
        assert chunks == []

    def test_chunk_ids_are_unique(self):
        text = "\n\n".join([f"Para {i} " * 30 for i in range(10)])
        chunks = chunk_text(text, source="multi.txt")
        ids = [c["id"] for c in chunks]
        assert len(ids) == len(set(ids))

    def test_source_preserved(self):
        text = "Some content."
        chunks = chunk_text(text, source="house-info.txt")
        assert all(c["source"] == "house-info.txt" for c in chunks)


class TestSearch:
    def test_returns_empty_when_no_collection(self):
        """search() should return [] gracefully when ChromaDB has no data."""
        with patch("app.rag._get_collection", return_value=None):
            results = search("wifi password")
            assert results == []

    def test_returns_empty_when_collection_is_empty(self):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 0
        with patch("app.rag._get_collection", return_value=mock_collection):
            results = search("wifi password")
            assert results == []

    def test_returns_results_from_collection(self):
        mock_collection = MagicMock()
        mock_collection.count.return_value = 5
        mock_collection.query.return_value = {
            "documents": [["WiFi password is spicypepper2026", "Trash day is Tuesday"]],
            "metadatas": [[{"source": "house-info.txt"}, {"source": "house-info.txt"}]],
            "distances": [[0.3, 0.7]],
        }
        with patch("app.rag._get_collection", return_value=mock_collection):
            results = search("wifi password", n_results=2)
            assert len(results) == 2
            assert results[0]["text"] == "WiFi password is spicypepper2026"
            assert results[0]["source"] == "house-info.txt"
            assert results[0]["distance"] == 0.3
            assert results[1]["distance"] == 0.7

    def test_handles_exception_gracefully(self):
        with patch("app.rag._get_collection", side_effect=Exception("ChromaDB error")):
            results = search("anything")
            assert results == []


class TestIngest:
    def test_missing_docs_dir(self, tmp_path):
        with patch("app.rag.DOCS_DIR", tmp_path / "nonexistent"):
            result = ingest_documents()
            assert result["ok"] is False
            assert "not found" in result["error"]

    def test_empty_docs_dir(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        with patch("app.rag.DOCS_DIR", docs):
            result = ingest_documents()
            assert result["ok"] is False
            assert "No .txt" in result["error"]

    def test_successful_ingest(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "test.txt").write_text("WiFi password is spicypepper2026.", encoding="utf-8")

        mock_collection = MagicMock()
        mock_collection.get.return_value = {"ids": []}

        with patch("app.rag.DOCS_DIR", docs), \
             patch("app.rag._get_collection", return_value=mock_collection):
            result = ingest_documents()
            assert result["ok"] is True
            assert result["files"] == 1
            assert result["chunks"] >= 1
            mock_collection.add.assert_called_once()
