import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Document, DocumentVersion, LogicalNode, Selection, SelectionItem, VersionNode
from .parser import normalize
from .schemas import TestCase


def ingest_path(db: Session, path: str | Path, filename: str) -> dict:
    """Ingest a local PDF for scripts and tests without requiring an HTTP client."""
    from .parser import parse_pdf
    title, parsed, source_hash = parse_pdf(path, settings.ocr_enabled)
    document = db.execute(select(Document).where(Document.title == title)).scalars().first()
    if not document:
        document = Document(title=title); db.add(document); db.flush()
    existing = db.execute(select(DocumentVersion).where(DocumentVersion.document_id == document.id,
                                                        DocumentVersion.source_sha256 == source_hash)).scalars().first()
    if existing:
        return {"document_id": document.id, "version_id": existing.id, "version_number": existing.version_number, "created": False}
    previous = db.execute(select(DocumentVersion).where(DocumentVersion.document_id == document.id)
                          .order_by(DocumentVersion.version_number.desc())).scalars().first()
    version = DocumentVersion(document_id=document.id, version_number=(previous.version_number + 1 if previous else 1),
                              source_filename=filename, source_sha256=source_hash)
    db.add(version); db.flush()
    prior = db.execute(select(VersionNode).join(DocumentVersion).where(DocumentVersion.document_id == document.id)).scalars().all()
    by_key: dict[tuple[str, str, str], list[VersionNode]] = {}
    for old in prior:
        by_key.setdefault((normalize(old.section_path), normalize(old.heading), old.node_type), []).append(old)
    node_by_key = {}
    for item in parsed:
        candidates = by_key.get((normalize(item.section_path), normalize(item.heading), item.node_type), [])
        logical = candidates[0].logical_node if len(candidates) == 1 else LogicalNode(document_id=document.id)
        if len(candidates) != 1:
            db.add(logical); db.flush()
        parent = node_by_key.get(item.parent_key) if item.parent_key else None
        node = VersionNode(version_id=version.id, logical_node_id=logical.id, node_key=item.node_key,
                           heading=item.heading, content=item.content, node_type=item.node_type, level=item.level,
                           parent_node_id=parent.id if parent else None, section_path=item.section_path,
                           content_hash=item.content_hash, source_pages=json.dumps(item.pages),
                           table_rows=json.dumps(item.table_rows),
                           uncertain=item.uncertain, ambiguous_match=len(candidates) > 1)
        db.add(node); db.flush(); node_by_key[item.node_key] = node
    db.commit()
    return {"document_id": document.id, "version_id": version.id, "version_number": version.version_number,
            "created": True, "node_count": len(parsed)}


class JsonGenerationStore:
    """Durable local alternative to MongoDB. Writes are atomic under a process lock."""
    _lock = threading.Lock()
    def __init__(self, path: str = settings.generated_store_path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def add(self, record: dict) -> dict:
        with self._lock:
            records = self._read()
            records.append(record)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(records, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        return record

    def find_by_selection(self, selection_id: str) -> list[dict]:
        return [r for r in self._read() if r.get("selection_id") == selection_id]


def selection_content(db: Session, selection: Selection) -> list[dict]:
    out = []
    for item in selection.items:
        node = db.get(VersionNode, item.version_node_id)
        version = db.get(DocumentVersion, item.version_id)
        if not node or not version:
            raise ValueError("Selection item points to missing historical data")
        out.append({"selection_item_id": item.id, "node_id": node.logical_node_id, "version_id": version.id,
                    "version_number": version.version_number, "heading": node.heading, "content": node.content,
                    "content_hash": node.content_hash, "source_pages": json.loads(node.source_pages)})
    return out


def build_mock_cases(sources: list[dict]) -> list[TestCase]:
    cases = []
    for i, src in enumerate(sources[:3], 1):
        cases.append(TestCase(title=f"Verify {src['heading']}", source_references=[src["node_id"]],
            preconditions=["Use the CT-200 manual requirement as the test oracle."],
            test_steps=[f"Exercise the behavior described by {src['heading']}.", "Record the observed result."],
            expected_result=src["content"] or src["heading"], priority="medium",
            rationale="Offline mock case derived only from the pinned source text."))
    return cases


def staleness(db: Session, selection: Selection) -> dict:
    latest = db.execute(select(DocumentVersion).where(DocumentVersion.document_id == selection.document_id)
                        .order_by(DocumentVersion.version_number.desc())).scalars().first()
    changed, removed, ambiguous = [], [], []
    source_version = None
    for item in selection.items:
        old = db.get(VersionNode, item.version_node_id)
        old_version = db.get(DocumentVersion, item.version_id)
        source_version = old_version.version_number if old_version else source_version
        current = None
        if latest:
            candidates = db.execute(select(VersionNode).where(VersionNode.version_id == latest.id,
                                                              VersionNode.logical_node_id == item.logical_node_id)).scalars().all()
            if len(candidates) == 1:
                current = candidates[0]
            elif len(candidates) > 1 or (old and old.ambiguous_match):
                ambiguous.append(item.logical_node_id)
            if not current and old:
                same_key = db.execute(select(VersionNode).where(VersionNode.version_id == latest.id)).scalars().all()
                same_key = [node for node in same_key
                             if normalize(node.section_path) == normalize(old.section_path)
                             and normalize(node.heading) == normalize(old.heading)
                             and node.node_type == old.node_type]
                if len(same_key) > 1 or any(node.ambiguous_match for node in same_key):
                    ambiguous.append(item.logical_node_id)
        if not current:
            if item.logical_node_id not in ambiguous:
                removed.append(item.logical_node_id)
        elif current.content_hash != item.source_content_hash:
            changed.append({"logical_node_id": item.logical_node_id, "old_hash": item.source_content_hash,
                            "latest_hash": current.content_hash, "heading": current.heading})
    status = "current"
    if ambiguous: status = "review_required"
    elif removed: status = "source_removed"
    elif changed: status = "stale"
    return {"status": status, "source_version": source_version,
            "latest_version": latest.version_number if latest else None,
            "changed_nodes": changed, "removed_nodes": removed, "ambiguous_nodes": ambiguous,
            "explanation": {"direct_source_changes": bool(changed or removed or ambiguous),
                            "inferred_cross_section_impacts": [],
                            "note": "Identical text does not guarantee contextual correctness."}}

