"""RAG module: document chunking, embedding via Ollama, and ChromaDB vector search.

Provides two main operations:
  - ingest_documents(): read .txt files from docs/, chunk them, embed, store in ChromaDB
  - search(query, n_results): find the most relevant chunks for a user query
"""
from pathlib import Path
from typing import Optional
import requests
import chromadb
from chromadb.utils.embedding_functions import OllamaEmbeddingFunction

from .config import settings
from .logger import log_info

DOCS_DIR = Path(__file__).resolve().parents[1] / "docs"
CHROMA_DIR = Path(__file__).resolve().parents[1] / "data" / "chroma"
COLLECTION_NAME = "household_docs"

OLLAMA_EMBED_URL = f"{settings.ollama_host}/api/embeddings"
EMBED_MODEL = "nomic-embed-text"

CHUNK_MAX_CHARS = 500
CHUNK_OVERLAP_CHARS = 50


def _get_embedding_function() -> OllamaEmbeddingFunction:
    return OllamaEmbeddingFunction(
        model_name=EMBED_MODEL,
        url=OLLAMA_EMBED_URL,
    )


def _get_collection(read_only: bool = False) -> Optional[chromadb.Collection]:
    """Return the ChromaDB collection, or None if the store doesn't exist yet."""
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    ef = _get_embedding_function()

    if read_only:
        try:
            return client.get_collection(name=COLLECTION_NAME, embedding_function=ef)
        except Exception:
            return None

    return client.get_or_create_collection(name=COLLECTION_NAME, embedding_function=ef)


def chunk_text(text: str, source: str) -> list[dict]:
    """Split text into chunks by paragraph boundaries, respecting CHUNK_MAX_CHARS.

    Returns list of {"id": str, "text": str, "source": str}.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    chunks = []
    current = ""
    chunk_idx = 0

    for para in paragraphs:
        if current and len(current) + len(para) + 1 > CHUNK_MAX_CHARS:
            chunks.append({
                "id": f"{source}::chunk{chunk_idx}",
                "text": current.strip(),
                "source": source,
            })
            chunk_idx += 1
            overlap = current[-CHUNK_OVERLAP_CHARS:] if len(current) > CHUNK_OVERLAP_CHARS else ""
            current = overlap + " " + para if overlap else para
        else:
            current = current + "\n\n" + para if current else para

    if current.strip():
        chunks.append({
            "id": f"{source}::chunk{chunk_idx}",
            "text": current.strip(),
            "source": source,
        })

    return chunks


def ingest_documents(trace_id: str = "ingest") -> dict:
    """Read all .txt files from docs/, chunk, embed, and store in ChromaDB.

    Returns {"ok": bool, "files": int, "chunks": int} or {"ok": False, "error": str}.
    """
    if not DOCS_DIR.exists():
        return {"ok": False, "error": f"docs/ directory not found at {DOCS_DIR}"}

    txt_files = sorted(DOCS_DIR.glob("*.txt"))
    if not txt_files:
        return {"ok": False, "error": "No .txt files found in docs/"}

    all_chunks = []
    for fpath in txt_files:
        text = fpath.read_text(encoding="utf-8")
        file_chunks = chunk_text(text, source=fpath.name)
        all_chunks.extend(file_chunks)
        log_info(trace_id, f"chunked {fpath.name}: {len(file_chunks)} chunks")

    if not all_chunks:
        return {"ok": False, "error": "No text content found in docs/"}

    try:
        collection = _get_collection(read_only=False)
        # Clear existing documents and re-ingest
        existing = collection.get()
        if existing["ids"]:
            collection.delete(ids=existing["ids"])

        collection.add(
            ids=[c["id"] for c in all_chunks],
            documents=[c["text"] for c in all_chunks],
            metadatas=[{"source": c["source"]} for c in all_chunks],
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}

    log_info(trace_id, f"ingested {len(txt_files)} files, {len(all_chunks)} chunks")
    return {"ok": True, "files": len(txt_files), "chunks": len(all_chunks)}


def get_project_collection(project_id: int, read_only: bool = False):
    """Return (or create) a project-specific ChromaDB collection."""
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    ef = _get_embedding_function()
    name = f"project_{project_id}"

    if read_only:
        try:
            return client.get_collection(name=name, embedding_function=ef)
        except Exception:
            return None
    return client.get_or_create_collection(name=name, embedding_function=ef)


def delete_project_collection(project_id: int, trace_id: str = "rag"):
    """Delete a project's ChromaDB collection entirely."""
    try:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        name = f"project_{project_id}"
        client.delete_collection(name=name)
        log_info(trace_id, f"deleted_project_collection project={project_id}")
    except Exception as e:
        log_info(trace_id, f"delete_project_collection_error={e}")


def search(query: str, n_results: int = 3, trace_id: str = "rag") -> list[dict]:
    """Search ChromaDB for chunks relevant to the query.

    Returns list of {"text": str, "source": str, "distance": float}.
    Returns empty list if ChromaDB is empty or unavailable.
    """
    try:
        collection = _get_collection(read_only=True)
        if collection is None or collection.count() == 0:
            return []

        results = collection.query(query_texts=[query], n_results=n_results)

        hits = []
        for i, doc in enumerate(results["documents"][0]):
            hits.append({
                "text": doc,
                "source": results["metadatas"][0][i]["source"],
                "distance": results["distances"][0][i],
            })

        log_info(trace_id, f"rag_search hits={len(hits)} top_dist={hits[0]['distance']:.3f}" if hits else "rag_search hits=0")
        return hits

    except Exception as e:
        log_info(trace_id, f"rag_search_error={e}")
        return []
