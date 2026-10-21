"""Offline v1 -> v2 demonstration. Run: python run_demo.py"""
from pathlib import Path
from app.db import SessionLocal, init_db
from app.models import DocumentVersion, Selection, SelectionItem, VersionNode
from app.services import JsonGenerationStore, build_mock_cases, ingest_path, selection_content, staleness

root = Path(__file__).parent
init_db()
with SessionLocal() as db:
    v1 = ingest_path(db, root / "ct200_manual.pdf", "ct200_manual.pdf")
    target = db.query(VersionNode).filter(VersionNode.version_id == v1["version_id"], VersionNode.heading.like("2.1.1.1%")).first()
    selection = Selection(document_id=v1["document_id"], name="v1 overview")
    db.add(selection); db.flush()
    selection.items.append(SelectionItem(version_id=v1["version_id"], logical_node_id=target.logical_node_id,
                                         version_node_id=target.id, source_content_hash=target.content_hash))
    db.commit()
    sources = selection_content(db, selection)
    generation_id = __import__("uuid").uuid4().hex
    JsonGenerationStore().add({"generation_id": generation_id, "selection_id": selection.id,
                               "provider": "mock", "test_cases": [c.model_dump() for c in build_mock_cases(sources)]})
    v2 = ingest_path(db, root / "ct200_manual_v2.pdf", "ct200_manual_v2.pdf")
    print({"v1": v1, "v2": v2, "generation": generation_id, "selection_after_v2": staleness(db, selection)})

