"""CLI script to ingest docs/*.txt into ChromaDB for RAG.

Usage:
    conda run -n chili-env python -m app.ingest
"""
from .rag import ingest_documents


def main():
    print("CHILI RAG Ingestion")
    print("=" * 40)
    result = ingest_documents(trace_id="cli-ingest")

    if result["ok"]:
        print(f"  Files ingested: {result['files']}")
        print(f"  Chunks stored:  {result['chunks']}")
        print("Done! ChromaDB is ready for RAG queries.")
    else:
        print(f"  Error: {result['error']}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
