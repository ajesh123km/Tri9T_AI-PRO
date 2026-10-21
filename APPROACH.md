# Implementation approach and evidence

## PDF inspection findings

`ct200_manual.pdf` is an unencrypted, untagged, native-text A5 PDF with 6 pages and no embedded images. `ct200_manual_v2.pdf` is the same format with 7 pages and no embedded images. Native extraction returned text on every page, so OCR is an opt-in fallback only for a page with fewer than 40 native characters. Text uses a title font of about 22 pt, major headings about 16.5 pt, subsection headings about 12.9 pt, and body text about 11 pt.

Observed irregularities, verified from block positions and text:

1. Section `3.4 Auto Shutoff` appears before `3.3 Result Display and Classification`; numbering order is preserved rather than sorted.
2. `2.1.1.1 Battery Life Under Typical Use` is a real heading rendered at body-sized text, so numbering is considered in addition to font size.
3. Text crosses page boundaries: section 3.1 continues from page 2 to page 3; v2 also splits 4.3, 6.1, and 8.1 across pages.
4. The PDFs contain two tabular regions: general specifications and error codes. Initial auditing found that the parser flattened these tables and did not expose cells. The parser now uses PyMuPDF's geometry-aware table finder, retaining verified row/column cells on typed table child nodes while source section text remains intact.
5. Classification requirements are numbered list content and are additionally represented as list-item child nodes.

The audit also found and fixed a dropped first list item: the previous splitter discarded `1. Normal` while retaining items 2-5. Regression coverage now requires all five items and verifies their parent is section 3.3. Rendered-page checks confirmed page-crossing content is preserved: 3.1 spans v1 pages 2-3, v2 4.3 spans pages 4-5, and v2 8.1 spans pages 6-7. The parser records those page lists.

The verified v1/v2 changes are: approximately 300 to 250 battery cycles; 15% to 10% low-battery threshold; 40 mmHg to 30 mmHg inflation increment; E3 auto-deflation within 2 to 1.5 seconds; E6 added; and section 5.3 Data Export added.

## Architecture and data model

`app/parser.py` extracts ordered blocks and builds section nodes with stable per-version node IDs, parent keys, paths, pages, types, hashes, and uncertainty flags. SQLAlchemy stores `Document`, immutable `DocumentVersion`, `LogicalNode`, `VersionNode`, `Selection`, and `SelectionItem`. A selection stores version ID, logical node ID, exact version-node ID, and source hash.

Matching uses normalized section path + heading + node type. A unique candidate preserves the logical node across versions; no candidate creates an addition; multiple candidates are marked ambiguous rather than silently choosing. Hashes detect content changes but never act as identity. Historical nodes remain untouched.

## Generation and staleness

The generation boundary is provider-agnostic. The bundled explicit mock produces 3 deterministic cases from exact pinned source text through the same bounded provider/retry wrapper used by the endpoint. Pydantic validation enforces required fields, 3-5 cases, types, non-empty values, and references limited to the pinned node IDs. Invalid output, unsupported references, and provider timeouts return controlled errors before storage. Successful output is stored as a distinct generation attempt with selection, versions, node IDs, and hashes. Atomic replacement plus a process lock avoids partial writes; multi-process concurrency is a known limitation.

Staleness compares pinned hashes to the latest node with the same logical ID and reports `current`, `stale`, `source_removed`, or `review_required`. Direct source changes are distinguished from inferred impact. The current implementation leaves inferred cross-section impacts empty; it never rewrites historical cases. Identical text is not treated as proof of contextual correctness.

## Decision log

1. The most dangerous silent failure is incorrect logical-node matching, especially duplicate headings. It is detected by normalized-path candidate counts and surfaced through `ambiguous_match`/`review_required`; tests cover unique IDs and matching.
2. Simplicity was chosen for table extraction and the local JSON generation store. Table structure is preserved as source text plus a typed node rather than a fully geometric cell model; JSON storage breaks first under multi-process/multi-host concurrency.
3. Scanned/image-only PDFs are intentionally unsupported unless OCR is enabled and Tesseract/Pillow are installed. Pages with weak native text then attempt OCR; parse failure is returned as a controlled 422 rather than silently discarding content.

## Validation evidence

The repository includes parser and workflow tests for the supplied PDFs, hierarchy irregularities, hashes, matching, idempotent ingestion, pinned selections, malformed provider validation, bounded retries, timeout handling, unsupported references, distinct generation history, stale status, verified table cells, list completeness, parent relationships, paragraph/page boundaries, and the v1/v2 regression values. The verified command is `python -m unittest discover -s tests -v` with 19 tests passing and one HTTP TestClient workflow skipped in the restricted final-review environment; the same tests are compatible with pytest when it is installed. `python run_demo.py` was executed successfully and reported browse, search, selection retrieval, three generated cases, provenance, changed-node comparison, and stale-selection status. The HTTP workflow test must be run with `RUN_HTTP_TESTS=1` on a host where TestClient can start an asyncio event loop.

This audit verifies the supplied PDFs' currently supported native-text structures. It does not establish complete extraction for arbitrary PDFs, scanned pages, figures, or tables whose geometry cannot be resolved by the PDF table finder.

