import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Selection, SelectionItem, VersionNode
from app.parser import normalize, parse_pdf
from app.schemas import TestCase
from app.services import JsonGenerationStore, ingest_path, staleness
from app import main


ROOT = Path(__file__).parents[1]


class ParserRegressionTests(unittest.TestCase):
    def test_actual_pdf_hierarchy_irregularities(self):
        title, nodes, _ = parse_pdf(ROOT / "ct200_manual.pdf")
        headings = [n.heading for n in nodes]
        self.assertIn("2.1.1.1 Battery Life Under Typical Use", headings)
        normalized = [normalize(h) for h in headings]
        self.assertLess(normalized.index("3.4 auto shutoff"), normalized.index("3.3 result display and classification"))
        self.assertTrue(any(n.node_type == "table" for n in nodes))

    def test_numbered_list_keeps_first_item_and_parent(self):
        _, nodes, _ = parse_pdf(ROOT / "ct200_manual.pdf")
        items = [n for n in nodes if n.node_type == "list_item"]
        self.assertEqual(len(items), 5)
        self.assertTrue(items[0].heading.startswith("1. Normal"))
        result = next(n for n in nodes if normalize(n.heading).startswith("3.3 result"))
        self.assertTrue(all(item.parent_key == result.node_key for item in items))

    def test_tables_have_verified_rows_and_columns(self):
        _, v1, _ = parse_pdf(ROOT / "ct200_manual.pdf")
        _, v2, _ = parse_pdf(ROOT / "ct200_manual_v2.pdf")
        specs = next(n for n in v1 if n.node_type == "table" and n.table_rows[0] == ["Parameter", "Value"])
        errors1 = next(n for n in v1 if n.node_type == "table" and n.table_rows[0] == ["Code", "Meaning", "Device Behavior"])
        errors2 = next(n for n in v2 if n.node_type == "table" and n.table_rows[0] == ["Code", "Meaning", "Device Behavior"])
        self.assertEqual(len(specs.table_rows), 8)
        self.assertEqual(specs.table_rows[1], ["Measurement method", "Oscillometric"])
        self.assertEqual(errors1.table_rows[-1][0], "E5")
        self.assertEqual(errors2.table_rows[-1][0], "E6")
        self.assertEqual(" ".join(errors2.table_rows[-1][2].split()), "Displays E6 on next sync attempt; does not affect measurement")

    def test_page_references_and_page_crossing_paragraphs(self):
        _, v1, _ = parse_pdf(ROOT / "ct200_manual.pdf")
        _, v2, _ = parse_pdf(ROOT / "ct200_manual_v2.pdf")
        p31 = next(n for n in v1 if normalize(n.heading).startswith("3.1 powering"))
        self.assertEqual(p31.pages, [2, 3])
        self.assertIn("Use the proﬁle button", p31.content)
        p43 = next(n for n in v2 if normalize(n.heading).startswith("4.3 alarm"))
        self.assertEqual(p43.pages, [4, 5])
        self.assertIn("which cannot be silenced", p43.content)
        p81 = next(n for n in v2 if normalize(n.heading).startswith("8.1 classification"))
        self.assertEqual(p81.pages, [6, 7])

    def test_pages_and_duplicate_safe_keys(self):
        _, nodes, _ = parse_pdf(ROOT / "ct200_manual_v2.pdf")
        self.assertEqual(len({n.node_key for n in nodes}), len(nodes))
        self.assertTrue(any(len(n.pages) > 1 for n in nodes))
        self.assertTrue(any(n.node_type == "list_item" for n in nodes))

    def test_known_v1_v2_values_are_in_source(self):
        _, v1, _ = parse_pdf(ROOT / "ct200_manual.pdf")
        _, v2, _ = parse_pdf(ROOT / "ct200_manual_v2.pdf")
        text1 = "\n".join(n.content for n in v1)
        text2 = "\n".join(n.content for n in v2)
        self.assertIn("300 measurement cycles", text1)
        self.assertIn("250 measurement cycles", text2)
        self.assertIn("below 15%", text1)
        self.assertIn("below 10%", text2)
        self.assertIn("40 mmHg increments", text1)
        self.assertIn("30 mmHg increments", text2)
        self.assertIn("within 2 seconds", text1)
        self.assertIn("within 1.5 seconds", text2)
        self.assertIn("E6 Bluetooth sync failure", text2)
        self.assertIn("5.3 Data Export", "\n".join(n.heading for n in v2))


class LLMValidationTests(unittest.TestCase):
    def test_bounded_retry_and_controlled_failure(self):
        from app.llm import generate_with_retries
        class Bad:
            name = "bad"
            def __init__(self): self.calls = 0
            def complete(self, prompt): self.calls += 1; return {"not": "a list"}
        provider = Bad()
        with self.assertRaises(ValueError): generate_with_retries(provider, "x", retries=2)
        self.assertEqual(provider.calls, 3)


class VersionIngestionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def _node(nodes, prefix):
        return next(n for n in nodes if normalize(n.heading).startswith(prefix))

    def test_v1_v2_changes_reuse_logical_nodes_without_mutating_v1(self):
        with self.Session() as db:
            v1 = ingest_path(db, ROOT / "ct200_manual.pdf", "ct200_manual.pdf")
            old = db.execute(select(VersionNode).where(VersionNode.version_id == v1["version_id"])).scalars().all()
            battery = self._node(old, "2.1.1.1 battery")
            inflation = self._node(old, "3.2 cuff")
            errors = self._node(old, "4.2 error")
            old_content = battery.content

            v2 = ingest_path(db, ROOT / "ct200_manual_v2.pdf", "ct200_manual_v2.pdf")
            new = db.execute(select(VersionNode).where(VersionNode.version_id == v2["version_id"])).scalars().all()
            self.assertEqual(battery.logical_node_id, self._node(new, "2.1.1.1 battery").logical_node_id)
            self.assertEqual(inflation.logical_node_id, self._node(new, "3.2 cuff").logical_node_id)
            self.assertEqual(errors.logical_node_id, self._node(new, "4.2 error").logical_node_id)
            self.assertIn("300 measurement cycles", old_content)
            self.assertIn("250 measurement cycles", self._node(new, "2.1.1.1 battery").content)
            self.assertIn("30 mmHg increments", self._node(new, "3.2 cuff").content)
            self.assertIn("within 1.5 seconds", self._node(new, "4.2 error").content)
            self.assertNotIn("E6", errors.content)
            self.assertIn("E6", self._node(new, "4.2 error").content)
            self.assertFalse(any(normalize(n.heading).startswith("5.3 data export") for n in old))
            self.assertTrue(any(normalize(n.heading).startswith("5.3 data export") for n in new))
            self.assertEqual(db.get(VersionNode, battery.id).content, old_content)

    def test_old_selection_keeps_original_version_text(self):
        with self.Session() as db:
            v1 = ingest_path(db, ROOT / "ct200_manual.pdf", "ct200_manual.pdf")
            old = self._node(db.execute(select(VersionNode).where(VersionNode.version_id == v1["version_id"])).scalars().all(), "2.1.1.1 battery")
            selection = Selection(document_id=v1["document_id"], name="legacy")
            selection.items.append(SelectionItem(version_id=v1["version_id"], logical_node_id=old.logical_node_id,
                                                 version_node_id=old.id, source_content_hash=old.content_hash))
            db.add(selection)
            db.commit()
            ingest_path(db, ROOT / "ct200_manual_v2.pdf", "ct200_manual_v2.pdf")
            self.assertEqual(db.get(VersionNode, old.id).content, old.content)
            self.assertEqual(selection.items[0].version_node_id, old.id)

    def test_ambiguous_match_is_review_required_not_source_removed(self):
        with self.Session() as db:
            v1 = ingest_path(db, ROOT / "ct200_manual.pdf", "ct200_manual.pdf")
            old = self._node(db.execute(select(VersionNode).where(VersionNode.version_id == v1["version_id"])).scalars().all(), "2.1.1.1 battery")
            duplicate = VersionNode(version_id=old.version_id, logical_node_id=old.logical_node_id, node_key=old.node_key + "-duplicate",
                                     heading=old.heading, content=old.content, node_type=old.node_type, level=old.level,
                                     section_path=old.section_path, content_hash=old.content_hash, source_pages=old.source_pages,
                                     table_rows=old.table_rows, uncertain=old.uncertain)
            db.add(duplicate)
            selection = Selection(document_id=v1["document_id"], name="ambiguous")
            selection.items.append(SelectionItem(version_id=v1["version_id"], logical_node_id=old.logical_node_id,
                                                 version_node_id=old.id, source_content_hash=old.content_hash))
            db.add(selection)
            db.commit()
            v2 = ingest_path(db, ROOT / "ct200_manual_v2.pdf", "ct200_manual_v2.pdf")
            current = self._node(db.execute(select(VersionNode).where(VersionNode.version_id == v2["version_id"])).scalars().all(), "2.1.1.1 battery")
            self.assertTrue(current.ambiguous_match)
            self.assertEqual(staleness(db, selection)["status"], "review_required")


class GenerationPipelineRegressionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.db = self.Session()
        result = ingest_path(self.db, ROOT / "ct200_manual.pdf", "ct200_manual.pdf")
        nodes = self.db.execute(select(VersionNode).where(VersionNode.version_id == result["version_id"],
                                                            VersionNode.node_type == "section")).scalars().all()
        self.selection = Selection(document_id=result["document_id"], name="generation audit")
        for node in nodes[:3]:
            self.selection.items.append(SelectionItem(version_id=result["version_id"], logical_node_id=node.logical_node_id,
                                                      version_node_id=node.id, source_content_hash=node.content_hash))
        self.db.add(self.selection)
        self.db.commit()
        self.store_path = ROOT / "tmp" / f"generation-audit-{uuid4()}.json"

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.store_path.unlink(missing_ok=True)

    @staticmethod
    def _valid_case(ref="node-1"):
        return {"title": "Verify requirement", "source_references": [ref],
                "preconditions": ["Device is ready"], "test_steps": ["Run the check"],
                "expected_result": "The requirement is met", "priority": "medium",
                "rationale": "Directly derived from the source"}

    def test_malformed_json_is_retried_and_rejected(self):
        from app.llm import generate_with_retries
        class Provider:
            name = "fake"
            def __init__(self): self.calls = 0
            def complete(self, prompt): self.calls += 1; return "{not-json"
        provider = Provider()
        with self.assertRaises(ValueError): generate_with_retries(provider, "prompt", retries=2)
        self.assertEqual(provider.calls, 3)

    def test_missing_fields_are_rejected(self):
        from app.llm import validate_cases
        case = self._valid_case()
        del case["expected_result"]
        with self.assertRaises(Exception): validate_cases([case, self._valid_case(), self._valid_case()])

    def test_wrong_data_types_are_rejected(self):
        from app.llm import validate_cases
        case = self._valid_case()
        case["title"] = 42
        with self.assertRaises(Exception): validate_cases([case, self._valid_case(), self._valid_case()])

    def test_case_count_must_be_between_three_and_five(self):
        from app.llm import validate_cases
        with self.assertRaises(ValueError): validate_cases([self._valid_case(), self._valid_case()])
        with self.assertRaises(ValueError): validate_cases([self._valid_case() for _ in range(6)])

    def test_timeout_is_retried_and_becomes_controlled_failure(self):
        from app.llm import generate_with_retries
        class Provider:
            name = "timeout"
            def __init__(self): self.calls = 0
            def complete(self, prompt): self.calls += 1; raise TimeoutError("provider timed out")
        provider = Provider()
        with self.assertRaises(ValueError): generate_with_retries(provider, "prompt", retries=2)
        self.assertEqual(provider.calls, 3)

    def test_unsupported_source_reference_is_api_error_and_not_stored(self):
        cases = [self._valid_case("not-selected") for _ in range(3)]
        store = JsonGenerationStore(self.store_path)
        with patch.object(main, "build_mock_cases", return_value=[TestCase.model_validate(c) for c in cases]), \
             patch.object(main, "JsonGenerationStore", return_value=store):
            with self.assertRaises(HTTPException) as raised:
                main.generate(self.selection.id, provider="mock", db=self.db)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(store._read(), [])

    def test_invalid_api_output_is_not_stored(self):
        store = JsonGenerationStore(self.store_path)
        with patch.object(main, "build_mock_cases", return_value=[]), \
             patch.object(main, "validate_cases", side_effect=ValueError("invalid output")), \
             patch.object(main, "JsonGenerationStore", return_value=store):
            with self.assertRaises(HTTPException) as raised:
                main.generate(self.selection.id, provider="mock", db=self.db)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(store._read(), [])

    def test_success_preserves_selection_provenance(self):
        store = JsonGenerationStore(self.store_path)
        with patch.object(main, "JsonGenerationStore", return_value=store):
            result = main.generate(self.selection.id, provider="mock", db=self.db)
        record = store._read()[0]
        self.assertEqual(record["selection_id"], self.selection.id)
        self.assertEqual(record["source_versions"], [1])
        self.assertEqual(record["source_node_ids"], [item.logical_node_id for item in self.selection.items])
        self.assertEqual(record["source_hashes"], [item.source_content_hash for item in self.selection.items])
        self.assertEqual(result["generation_id"], record["generation_id"])

