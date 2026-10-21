"""Offline v1 -> v2 demonstration. Run: python run_demo.py"""
from pathlib import Path
from app.db import SessionLocal, init_db
from app.main import compare, generate, get_generation, get_selection, search, sections
from app.models import DocumentVersion, Selection, SelectionItem, VersionNode
from app.services import ingest_path, staleness

root = Path(__file__).parent
init_db()
with SessionLocal() as db:
    v1 = ingest_path(db, root / "ct200_manual.pdf", "ct200_manual.pdf")
    target = db.query(VersionNode).filter(VersionNode.version_id == v1["version_id"], VersionNode.heading.like("2.1.1.1%")).first()
    selection = Selection(document_id=v1["document_id"], name="v1 overview")
    db.add(selection); db.flush()
    selected_nodes = [target] + db.query(VersionNode).filter(
        VersionNode.version_id == v1["version_id"], VersionNode.node_type == "section",
        VersionNode.id != target.id).order_by(VersionNode.id).limit(2).all()
    for node in selected_nodes:
        selection.items.append(SelectionItem(version_id=v1["version_id"], logical_node_id=node.logical_node_id,
                                             version_node_id=node.id, source_content_hash=node.content_hash))
    db.commit()
    browsed = sections(v1["document_id"], version_id=v1["version_id"], db=db)
    searched = search(v1["document_id"], "battery", v1["version_id"], db)
    selection_before = get_selection(selection.id, db)
    generation = generate(selection.id, provider="mock", db=db)
    retrieved_generation = get_generation(generation["generation_id"])
    v2 = ingest_path(db, root / "ct200_manual_v2.pdf", "ct200_manual_v2.pdf")
    comparison = compare(v1["document_id"], target.logical_node_id, v1["version_id"], v2["version_id"], db)
    selection_after = get_selection(selection.id, db)
    print({"v1": v1, "v2": v2, "browse_top_level_count": len(browsed),
           "search_match_count": len(searched["results"]),
           "selection_before_version": selection_before["items"][0]["version_number"],
           "generation_case_count": len(retrieved_generation["test_cases"]),
           "generation_provenance": {"selection_id": retrieved_generation["selection_id"],
                                     "source_versions": retrieved_generation["source_versions"],
                                     "source_node_ids": retrieved_generation["source_node_ids"],
                                     "source_hashes": retrieved_generation["source_hashes"]},
           "comparison": {"status": comparison["status"], "from_version": comparison["from_version"],
                          "to_version": comparison["to_version"]},
           "selection_after_version": selection_after["items"][0]["version_number"],
           "selection_after_v2": staleness(db, selection)})

