import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import settings
from .db import SessionLocal, init_db
from .models import Document, DocumentVersion, LogicalNode, Selection, SelectionItem, VersionNode
from .parser import normalize, parse_pdf
from .schemas import GenerationOut, NodeOut, SelectionIn, VersionOut
from .services import JsonGenerationStore, build_mock_cases, selection_content, staleness
from .llm import OpenAICompatibleProvider, generate_with_retries

app = FastAPI(title="Tri9T AI CT-200 Document QA Service", version="0.1.0")


@app.on_event("startup")
def startup() -> None:
    init_db()


def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _version(db: Session, document_id: str, version_id: str | None) -> DocumentVersion:
    if version_id:
        found = db.get(DocumentVersion, version_id)
        if not found or found.document_id != document_id:
            raise HTTPException(404, "Version not found for document")
        return found
    found = db.execute(select(DocumentVersion).where(DocumentVersion.document_id == document_id)
                       .order_by(DocumentVersion.version_number.desc())).scalars().first()
    if not found:
        raise HTTPException(404, "Document has no versions")
    return found


def _node_out(db: Session, node: VersionNode) -> dict:
    children = db.execute(select(VersionNode).where(VersionNode.version_id == node.version_id,
                                                     VersionNode.parent_node_id == node.id)
                          .order_by(VersionNode.id)).scalars().all()
    return {"id": node.id, "logical_node_id": node.logical_node_id, "version_id": node.version_id,
            "heading": node.heading, "content": node.content, "node_type": node.node_type, "level": node.level,
            "parent_node_id": node.parent_node_id, "children": [_node_out(db, c) for c in children],
            "section_path": node.section_path, "content_hash": node.content_hash,
            "source_pages": json.loads(node.source_pages), "table_rows": json.loads(node.table_rows or "[]"),
            "uncertain": node.uncertain,
            "ambiguous_match": node.ambiguous_match}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/")
def root() -> dict:
    return {
        "service": "Tri9T AI CT-200 Document QA Service",
        "status": "ok",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "health": "/health",
    }


@app.post("/documents/ingest", status_code=201)
def ingest(file: UploadFile = File(...), db: Session = Depends(db_session)) -> dict:
    if Path(file.filename or "manual.pdf").suffix.lower() != ".pdf":
        raise HTTPException(415, "Only PDF input is supported")
    tmp = Path(".tri9t_upload.pdf")
    tmp.write_bytes(file.file.read())
    try:
        title, parsed, source_hash = parse_pdf(tmp, settings.ocr_enabled)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(422, f"PDF parsing failed: {exc}") from exc
    tmp.unlink(missing_ok=True)
    document = db.execute(select(Document).where(Document.title == title)).scalars().first()
    if not document:
        document = Document(title=title)
        db.add(document)
        db.flush()
    existing = db.execute(select(DocumentVersion).where(DocumentVersion.document_id == document.id,
                                                        DocumentVersion.source_sha256 == source_hash)).scalars().first()
    if existing:
        return {"document_id": document.id, "version_id": existing.id, "version_number": existing.version_number,
                "created": False, "message": "Identical source already ingested"}
    previous = db.execute(select(DocumentVersion).where(DocumentVersion.document_id == document.id)
                          .order_by(DocumentVersion.version_number.desc())).scalars().first()
    version = DocumentVersion(document_id=document.id, version_number=(previous.version_number + 1 if previous else 1),
                              source_filename=file.filename or "manual.pdf", source_sha256=source_hash)
    db.add(version)
    db.flush()
    prior_nodes = db.execute(select(VersionNode).join(DocumentVersion)
                             .where(DocumentVersion.document_id == document.id)).scalars().all()
    by_key: dict[tuple[str, str, str], list[VersionNode]] = {}
    for old in prior_nodes:
        by_key.setdefault((normalize(old.section_path), normalize(old.heading), old.node_type), []).append(old)
    node_by_key: dict[str, VersionNode] = {}
    for item in parsed:
        candidates = by_key.get((normalize(item.section_path), normalize(item.heading), item.node_type), [])
        ambiguous = len(candidates) > 1
        logical = candidates[0].logical_node if len(candidates) == 1 else LogicalNode(document_id=document.id)
        if len(candidates) != 1:
            db.add(logical)
            db.flush()
        parent = node_by_key.get(item.parent_key) if item.parent_key else None
        node = VersionNode(version_id=version.id, logical_node_id=logical.id, node_key=item.node_key,
                           heading=item.heading, content=item.content, node_type=item.node_type, level=item.level,
                           parent_node_id=parent.id if parent else None, section_path=item.section_path,
                           content_hash=item.content_hash, source_pages=json.dumps(item.pages),
                           table_rows=json.dumps(item.table_rows),
                           uncertain=item.uncertain, ambiguous_match=ambiguous)
        db.add(node)
        db.flush()
        node_by_key[item.node_key] = node
    db.commit()
    return {"document_id": document.id, "version_id": version.id, "version_number": version.version_number,
            "created": True, "node_count": len(parsed), "title": title}


@app.get("/documents/{document_id}/versions", response_model=list[VersionOut])
def versions(document_id: str, db: Session = Depends(db_session)):
    return db.execute(select(DocumentVersion).where(DocumentVersion.document_id == document_id)
                      .order_by(DocumentVersion.version_number)).scalars().all()


@app.get("/documents/{document_id}/sections", response_model=list[NodeOut])
def sections(document_id: str, version_id: str | None = Query(default=None), db: Session = Depends(db_session)):
    version = _version(db, document_id, version_id)
    nodes = db.execute(select(VersionNode).where(VersionNode.version_id == version.id,
                                                  VersionNode.parent_node_id.is_(None))
                       .order_by(VersionNode.id)).scalars().all()
    return [_node_out(db, n) for n in nodes]


@app.get("/nodes/{node_id}", response_model=NodeOut)
def node(node_id: str, db: Session = Depends(db_session)):
    found = db.get(VersionNode, node_id)
    if not found:
        raise HTTPException(404, "Node not found")
    return _node_out(db, found)


@app.get("/documents/{document_id}/search")
def search(document_id: str, q: str, version_id: str | None = None, db: Session = Depends(db_session)):
    version = _version(db, document_id, version_id)
    term = f"%{q}%"
    found = db.execute(select(VersionNode).where(VersionNode.version_id == version.id,
                                                  or_(VersionNode.heading.ilike(term), VersionNode.content.ilike(term)))).scalars().all()
    return {"version_id": version.id, "results": [_node_out(db, n) for n in found]}


@app.get("/documents/{document_id}/compare/{logical_node_id}")
def compare(document_id: str, logical_node_id: str, from_version: str, to_version: str | None = None,
            db: Session = Depends(db_session)):
    old_v = _version(db, document_id, from_version)
    new_v = _version(db, document_id, to_version)
    old = db.execute(select(VersionNode).where(VersionNode.version_id == old_v.id,
                                                VersionNode.logical_node_id == logical_node_id)).scalars().first()
    new = db.execute(select(VersionNode).where(VersionNode.version_id == new_v.id,
                                                VersionNode.logical_node_id == logical_node_id)).scalars().first()
    if not old and not new:
        raise HTTPException(404, "Logical node not found in either version")
    status = "added" if not old else "removed" if not new else "unchanged" if old.content_hash == new.content_hash else "changed"
    return {"logical_node_id": logical_node_id, "from_version": old_v.version_number, "to_version": new_v.version_number,
            "status": status, "from_hash": old.content_hash if old else None, "to_hash": new.content_hash if new else None,
            "summary": "Content hashes are identical" if status == "unchanged" else f"Node is {status}",
            "from": _node_out(db, old) if old else None, "to": _node_out(db, new) if new else None}


@app.post("/selections", status_code=201)
def create_selection(payload: SelectionIn, db: Session = Depends(db_session)):
    if not db.get(Document, payload.document_id):
        raise HTTPException(404, "Document not found")
    selection = Selection(document_id=payload.document_id, name=payload.name)
    db.add(selection)
    db.flush()
    for requested in payload.items:
        version = db.get(DocumentVersion, requested.version_id)
        n = db.get(VersionNode, requested.node_id)
        if not version or version.document_id != payload.document_id or not n or n.version_id != version.id:
            db.rollback()
            raise HTTPException(400, "Each node_id/version_id pair must belong to this document and version")
        selection.items.append(SelectionItem(version_id=version.id, logical_node_id=n.logical_node_id,
                                              version_node_id=n.id, source_content_hash=n.content_hash))
    db.commit()
    return {"selection_id": selection.id, "name": selection.name, "item_count": len(selection.items)}


@app.get("/selections/{selection_id}")
def get_selection(selection_id: str, db: Session = Depends(db_session)):
    found = db.get(Selection, selection_id)
    if not found:
        raise HTTPException(404, "Selection not found")
    return {"selection_id": found.id, "document_id": found.document_id, "name": found.name,
            "items": selection_content(db, found), "staleness": staleness(db, found)}


@app.get("/selections/{selection_id}/staleness")
def selection_staleness(selection_id: str, db: Session = Depends(db_session)):
    found = db.get(Selection, selection_id)
    if not found:
        raise HTTPException(404, "Selection not found")
    return staleness(db, found)


@app.post("/generations", response_model=GenerationOut)
def generate(selection_id: str, provider: str | None = None, db: Session = Depends(db_session)):
    selection = db.get(Selection, selection_id)
    if not selection:
        raise HTTPException(404, "Selection not found")
    sources = selection_content(db, selection)
    selected_provider = provider or settings.llm_provider
    if selected_provider == "mock":
        class MockProvider:
            name = "mock"
            def complete(self, prompt: str):
                return [case.model_dump() for case in build_mock_cases(sources)]
        llm = MockProvider()
    elif selected_provider in {"openai", "openai_compatible"}:
        try:
            llm = OpenAICompatibleProvider(settings.llm_api_key or "", settings.llm_model or "",
                                           settings.llm_base_url, settings.llm_timeout_seconds)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from exc
    else:
        raise HTTPException(501, f"Unsupported LLM provider: {selected_provider}")

    prompt = "Generate 3 to 5 QA test-case ideas using only the pinned source text below.\n" + \
             "\n".join(f"[{source['node_id']}] {source['heading']}: {source['content']}" for source in sources)
    try:
        cases = generate_with_retries(llm, prompt,
                                      allowed_source_references={s["node_id"] for s in sources})
    except (ValueError, TypeError) as exc:
        raise HTTPException(502, f"Provider returned invalid structured output: {exc}") from exc
    generation_id = str(uuid4())
    record = {"generation_id": generation_id, "selection_id": selection_id, "provider": selected_provider,
              "created_at": datetime.now(timezone.utc).isoformat(), "source_versions": sorted({s["version_number"] for s in sources}),
              "source_node_ids": [s["node_id"] for s in sources], "source_hashes": [s["content_hash"] for s in sources],
              "generation_config": {"provider": selected_provider,
                                    "model": getattr(llm, "model", None),
                                    "timeout_seconds": getattr(llm, "timeout_seconds", None)},
              "test_cases": [c.model_dump() for c in cases]}
    JsonGenerationStore().add(record)
    return {"generation_id": generation_id, "selection_id": selection_id, "provider": selected_provider, "test_cases": cases}


@app.get("/generations/{generation_id}")
def get_generation(generation_id: str):
    found = next((r for r in JsonGenerationStore()._read() if r.get("generation_id") == generation_id), None)
    if not found:
        raise HTTPException(404, "Generation not found")
    return found
