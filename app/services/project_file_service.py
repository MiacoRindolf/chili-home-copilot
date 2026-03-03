"""Project file service: save, extract text, ingest into project-scoped RAG, and search."""
import uuid
from pathlib import Path
from typing import Optional

from sqlalchemy.orm import Session

from ..logger import log_info
from ..models import ProjectFile
from .. import rag as rag_module

PROJECTS_DIR = Path(__file__).resolve().parents[2] / "data" / "projects"

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".csv", ".json", ".log", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
})
CODE_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".scss",
    ".java", ".go", ".rs", ".c", ".cpp", ".h", ".hpp", ".cs",
    ".rb", ".php", ".sh", ".bat", ".sql", ".r", ".swift", ".kt",
})
PDF_EXTENSIONS = frozenset({".pdf"})
IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

ALL_ALLOWED = TEXT_EXTENSIONS | CODE_EXTENSIONS | PDF_EXTENSIONS | IMAGE_EXTENSIONS


def _project_dir(project_id: int) -> Path:
    d = PROJECTS_DIR / str(project_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def validate_file(filename: str, file_size: int) -> Optional[str]:
    """Return an error message if the file is invalid, or None if OK."""
    ext = Path(filename).suffix.lower()
    if ext not in ALL_ALLOWED:
        return f"Unsupported file type: {ext}"
    if file_size > MAX_FILE_SIZE:
        return f"File too large ({file_size / 1024 / 1024:.1f} MB). Max is 10 MB."
    return None


def save_file(project_id: int, file_bytes: bytes, filename: str, content_type: str, db: Session) -> ProjectFile:
    """Save file to disk and create a ProjectFile record."""
    ext = Path(filename).suffix.lower()
    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest = _project_dir(project_id) / stored_name
    dest.write_bytes(file_bytes)

    pf = ProjectFile(
        project_id=project_id,
        original_name=filename,
        stored_name=stored_name,
        content_type=content_type,
        file_size=len(file_bytes),
    )
    db.add(pf)
    db.commit()
    db.refresh(pf)
    return pf


def extract_text(file_path: Path, content_type: str, trace_id: str = "extract") -> str:
    """Extract text content from a file based on its type."""
    ext = file_path.suffix.lower()

    if ext in TEXT_EXTENSIONS or ext in CODE_EXTENSIONS:
        try:
            return file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return file_path.read_text(encoding="latin-1")

    if ext in PDF_EXTENSIONS:
        return _extract_pdf(file_path, trace_id)

    if ext in IMAGE_EXTENSIONS:
        return _extract_image_description(file_path, trace_id)

    return ""


def _extract_pdf(file_path: Path, trace_id: str) -> str:
    """Extract text from PDF using PyPDF2."""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(str(file_path))
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text()
            if text:
                pages.append(text)
        result = "\n\n".join(pages)
        log_info(trace_id, f"pdf_extracted pages={len(reader.pages)} chars={len(result)}")
        return result
    except Exception as e:
        log_info(trace_id, f"pdf_extraction_error={e}")
        return ""


def _extract_image_description(file_path: Path, trace_id: str) -> str:
    """Use vision model to describe an image for RAG ingestion."""
    try:
        from .. import vision as vision_module
        filename = file_path.name
        description, _model = vision_module.describe_image(
            [filename], "Describe this image in detail for document context.", "", trace_id
        )
        log_info(trace_id, f"image_described file={filename} chars={len(description)}")
        return f"[Image: {file_path.stem}]\n{description}"
    except Exception as e:
        log_info(trace_id, f"image_description_error={e}")
        return f"[Image: {file_path.stem}]"


def ingest_file(project_id: int, project_file: ProjectFile, trace_id: str = "ingest") -> dict:
    """Extract text from a project file, chunk it, and add to the project's ChromaDB collection."""
    file_path = _project_dir(project_id) / project_file.stored_name
    if not file_path.exists():
        return {"ok": False, "error": "File not found on disk"}

    text = extract_text(file_path, project_file.content_type, trace_id)
    if not text.strip():
        log_info(trace_id, f"no_text_extracted file={project_file.original_name}")
        return {"ok": True, "chunks": 0}

    chunks = rag_module.chunk_text(text, source=project_file.original_name)
    if not chunks:
        return {"ok": True, "chunks": 0}

    try:
        collection = rag_module.get_project_collection(project_id)
        collection.add(
            ids=[c["id"] for c in chunks],
            documents=[c["text"] for c in chunks],
            metadatas=[{"source": c["source"], "file_id": str(project_file.id)} for c in chunks],
        )
        log_info(trace_id, f"project_file_ingested project={project_id} file={project_file.original_name} chunks={len(chunks)}")
        return {"ok": True, "chunks": len(chunks)}
    except Exception as e:
        log_info(trace_id, f"project_ingest_error={e}")
        return {"ok": False, "error": str(e)}


def remove_file(project_id: int, project_file: ProjectFile, db: Session, trace_id: str = "remove"):
    """Remove file from disk, ChromaDB, and database."""
    file_path = _project_dir(project_id) / project_file.stored_name
    if file_path.exists():
        file_path.unlink()

    try:
        collection = rag_module.get_project_collection(project_id, read_only=True)
        if collection and collection.count() > 0:
            existing = collection.get(where={"file_id": str(project_file.id)})
            if existing["ids"]:
                collection.delete(ids=existing["ids"])
                log_info(trace_id, f"chromadb_chunks_removed file_id={project_file.id} count={len(existing['ids'])}")
    except Exception as e:
        log_info(trace_id, f"chromadb_removal_error={e}")

    db.delete(project_file)
    db.commit()


def remove_project_collection(project_id: int, trace_id: str = "remove"):
    """Delete the entire ChromaDB collection for a project."""
    rag_module.delete_project_collection(project_id, trace_id)


def search_project(project_id: int, query: str, n_results: int = 3, trace_id: str = "proj_rag") -> list[dict]:
    """Search a project's ChromaDB collection for relevant chunks."""
    try:
        collection = rag_module.get_project_collection(project_id, read_only=True)
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
        log_info(trace_id, f"project_rag_search project={project_id} hits={len(hits)}")
        return hits
    except Exception as e:
        log_info(trace_id, f"project_rag_search_error={e}")
        return []
