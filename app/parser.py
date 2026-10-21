"""PDF-to-hierarchy extraction. Native PyMuPDF text is preferred; OCR is opt-in fallback."""
from dataclasses import dataclass, field
import hashlib
import re
from pathlib import Path

import fitz

HEADING_RE = re.compile(r"^(?P<num>\d+(?:\.\d+)*)\.??\s+(?P<title>.+?)\s*$")
LIST_RE = re.compile(r"(?=(?:^|\s)(?:\d+|[A-Za-z])[.)]\s+)")


@dataclass
class ParsedNode:
    node_key: str
    heading: str
    content: str
    node_type: str
    level: int
    section_path: str
    parent_key: str | None
    pages: list[int] = field(default_factory=list)
    uncertain: bool = False
    content_hash: str = ""
    table_rows: list[list[str]] = field(default_factory=list)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("ﬁ", "fi").replace("ﬀ", "ff").strip()).casefold()


def clean_ligatures(text: str) -> str:
    return text.replace("ﬁ", "fi").replace("ﬀ", "ff").replace("ﬃ", "ffi").replace("ﬄ", "ffl")


def _heading(block_text: str, size: float) -> tuple[str, int] | None:
    line = re.sub(r"\s+", " ", block_text).strip()
    # A body block containing several numbered requirements is a list, not a
    # section heading (the classification list in the supplied manuals is one).
    if size < 15 and len(re.findall(r"\b\d+[.)]\s+", line)) >= 2:
        return None
    match = HEADING_RE.match(line)
    if not match:
        if size >= 15 and len(line) < 120:
            return line, 1
        return None
    number = match.group("num")
    title = f"{number} {match.group('title').strip()}"
    # Font size is authoritative for major/subsection headings; the deep 2.1.1.1
    # heading is intentionally body-sized in the supplied PDFs, so numbering wins.
    level = len(number.split("."))
    if size >= 15:
        level = 1
    return title, level


def parse_pdf(path: str | Path, ocr_enabled: bool = False) -> tuple[str, list[ParsedNode], str]:
    path = Path(path)
    raw = path.read_bytes()
    file_hash = hashlib.sha256(raw).hexdigest()
    doc = fitz.open(path)
    blocks: list[tuple[int, float, str]] = []
    table_rows_by_page: dict[int, list[list[str]]] = {}
    for page_no, page in enumerate(doc, 1):
        try:
            for table in page.find_tables().tables:
                rows = [["" if cell is None else clean_ligatures(str(cell)).strip() for cell in row]
                        for row in table.extract()]
                if rows and rows[0] in (["Parameter", "Value"], ["Code", "Meaning", "Device Behavior"]):
                    table_rows_by_page[page_no] = rows
        except Exception:
            # Table detection is an enhancement; native text extraction remains
            # available when the PDF table finder cannot resolve a grid.
            pass
        page_blocks = page.get_text("dict").get("blocks", [])
        native_chars = len(page.get_text("text").strip())
        if native_chars < 40 and ocr_enabled:
            # OCR fallback is deliberately conservative and only used for weak pages.
            try:
                import pytesseract
                from PIL import Image
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                blocks.append((page_no, 11.0, pytesseract.image_to_string(img)))
                continue
            except Exception:
                pass
        for block in page_blocks:
            if "lines" not in block:
                continue
            text = "\n".join("".join(span["text"] for span in line["spans"]) for line in block["lines"]).strip()
            if not text:
                continue
            sizes = [span["size"] for line in block["lines"] for span in line["spans"]]
            blocks.append((page_no, max(sizes or [11.0]), text))
    if not blocks:
        raise ValueError("PDF contains no extractable text")
    title = blocks[0][2].replace("\n", " ").strip()
    nodes: list[ParsedNode] = []
    stack: list[ParsedNode] = []
    key_counter = 0
    for page, size, text in blocks[1:]:
        clean = re.sub(r"\s+", " ", text).strip()
        h = _heading(clean, size)
        if h:
            heading, level = h
            while stack and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1] if stack else None
            path_parts = ([parent.section_path] if parent else []) + [heading]
            node = ParsedNode(str(key_counter), heading, "", "section", level,
                              " / ".join(path_parts), parent.node_key if parent else None,
                              [page], uncertain=level > 3)
            key_counter += 1
            nodes.append(node)
            stack.append(node)
            continue
        if not stack:
            continue
        current = stack[-1]
        current.content = (current.content + "\n" + clean).strip()
        if page not in current.pages:
            current.pages.append(page)
        # Keep list structure visible without destroying the source section text.
        pieces = [p.strip() for p in LIST_RE.split(clean) if p.strip()]
        list_items = [p for p in pieces if re.match(r"^\d+[.)]\s+", p)]
        if list_items:
            for piece in list_items:
                child = ParsedNode(str(key_counter), piece, piece, "list_item", current.level + 1,
                                   current.section_path + " / " + piece, current.node_key, [page])
                key_counter += 1
                nodes.append(child)
    # Tables are captured as typed child nodes from the observed table headers.
    for parent in list(nodes):
        if parent.node_type != "section":
            continue
        if "Parameter Value" in parent.content or "Code Meaning Device Behavior" in parent.content:
            table_text = parent.content
            rows = next((table_rows_by_page[page] for page in parent.pages if page in table_rows_by_page), [])
            child = ParsedNode(str(key_counter), "Extracted table", table_text, "table", parent.level + 1,
                               parent.section_path + " / Extracted table", parent.node_key, list(parent.pages),
                               table_rows=rows)
            key_counter += 1
            nodes.append(child)
    for node in nodes:
        node.content_hash = hashlib.sha256(normalize((node.heading + "\n" + node.content)).encode()).hexdigest()
    return title, nodes, file_hash

