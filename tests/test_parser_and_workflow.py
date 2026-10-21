import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from app.parser import normalize, parse_pdf


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

