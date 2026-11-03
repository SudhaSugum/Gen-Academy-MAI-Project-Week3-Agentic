"""Re-runnable synthetic corpus ingestion CLI.

Run: python -m src.issue_triage.ingestion.cli_ingest [--rebuild]
"""

from __future__ import annotations

import argparse
import json
import time

from langchain_openai import OpenAIEmbeddings
from langchain_pinecone import PineconeVectorStore
from pinecone import Pinecone, ServerlessSpec

from ..config import get_settings
from .corpus import DATA, build_documents


def ensure_index(client: Pinecone, settings) -> None:
    existing = {item.name for item in client.list_indexes()}
    if settings.pinecone_index not in existing:
        client.create_index(
            name=settings.pinecone_index,
            dimension=settings.embedding_dimensions,
            metric="cosine",
            spec=ServerlessSpec(cloud=settings.pinecone_cloud, region=settings.pinecone_region),
        )
    deadline = time.monotonic() + 120
    description = client.describe_index(settings.pinecone_index)
    if description.dimension != settings.embedding_dimensions:
        raise ValueError(f"Index dimension {description.dimension} does not match configured embedding dimension {settings.embedding_dimensions}; create a new index and re-embed all data.")
    while not description.status.ready:
        if time.monotonic() > deadline:
            raise TimeoutError("Pinecone index did not become ready within 120 seconds.")
        time.sleep(2)
        description = client.describe_index(settings.pinecone_index)


def ingest(rebuild: bool) -> None:
    settings = get_settings()
    if not settings.pinecone_api_key or not settings.openai_api_key:
        raise SystemExit("Set PINECONE_API_KEY and OPENAI_API_KEY in the environment first.")
    pc = Pinecone(api_key=settings.pinecone_api_key)
    ensure_index(pc, settings)
    index = pc.Index(settings.pinecone_index)
    if rebuild:
        for namespace in ("past_issues", "controls", "policies"):
            index.delete(delete_all=True, namespace=namespace)
    embeddings = OpenAIEmbeddings(model=settings.embedding_model, api_key=settings.openai_api_key, dimensions=settings.embedding_dimensions)
    store = PineconeVectorStore(index_name=settings.pinecone_index, embedding=embeddings)
    counts = {}
    for namespace, docs in build_documents().items():
        ids = [doc.metadata["chunk_id"] for doc in docs]
        store.add_documents(docs, ids=ids, namespace=namespace)
        counts[namespace] = len(docs)
    manifest = {
        "index": settings.pinecone_index, "metric": "cosine", "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.embedding_dimensions, "namespaces": counts,
        "source_formats": {"past_issues": "csv", "controls": "docx", "policies": "pdf"},
        "retrieval": "pinecone semantic + local BM25 keyword, fused with reciprocal rank fusion",
        "source_scope": "synthetic-only", "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (DATA / "index_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"index": settings.pinecone_index, "indexed_counts": counts, "rebuild": rebuild}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Index the synthetic Issue Triage Assistant V1 corpus.")
    parser.add_argument("--rebuild", action="store_true", help="Delete the three source namespaces before re-indexing.")
    args = parser.parse_args()
    ingest(args.rebuild)


if __name__ == "__main__":
    main()
