# Tri9T AI CT-200 document QA service

This FastAPI service parses the supplied CT-200 PDFs directly with PyMuPDF, reconstructs a versioned hierarchy, pins selections to historical source text, and stores validated offline QA ideas with provenance. The bundled `mock` provider is explicit and is not clinically validated output.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

The Swagger UI is at `http://127.0.0.1:8000/docs`. Ingest with `POST /documents/ingest` as multipart PDF. Ingest `ct200_manual.pdf` first and `ct200_manual_v2.pdf` second. Identical source hashes are idempotent. See `python run_demo.py` for an offline end-to-end flow.

## API surface

* `POST /documents/ingest` - create an immutable document version.
* `GET /documents/{document_id}/versions` - list versions.
* `GET /documents/{document_id}/sections?version_id=...` - browse top-level hierarchy; latest is default.
* `GET /nodes/{node_id}` - retrieve content, children, page references, and hashes.
* `GET /documents/{document_id}/search?q=...&version_id=...` - search headings/body.
* `GET /documents/{document_id}/compare/{logical_node_id}?from_version=...&to_version=...` - compare versions.
* `POST /selections` and `GET /selections/{selection_id}` - create and retrieve pinned selections.
* `GET /selections/{selection_id}/staleness` - current/stale/removed/review-required status.
* `POST /generations?selection_id=...&provider=mock` and `GET /generations/{generation_id}` - generate/retrieve provenance-bearing QA ideas.

## Configuration

Copy `.env.example`. `DATABASE_URL` defaults to SQLite, and generated output uses an atomic JSON store at `data/generated.json` because no MongoDB dependency/service is assumed. JSON storage is durable but has single-process locking and should be replaced with MongoDB for multi-process production. `LLM_PROVIDER=mock` requires no key. A production provider adapter must validate structured output before storage; this repository intentionally returns 501 for an unimplemented non-mock provider.

## Testing

```powershell
python -m unittest discover -s tests -v
```

The suite uses deterministic mocked provider responses, temporary SQLite/JSON stores, and no external LLM calls. If the optional test dependency is installed, the same tests can also be run with `python -m pytest -q`. The supplied PDFs are regression fixtures. The implementation does not claim regulatory compliance or clinical validity.

## End-to-end demonstration

```powershell
python run_demo.py
```

The demonstration ingests v1 and v2, browses and searches v1, creates and retrieves a pinned selection, generates and retrieves three explicitly mocked QA cases with provenance, compares the battery-life node across versions, and reports the v1 selection's v2 staleness status.

Example API requests after starting the server:

```powershell
curl.exe -F "file=@ct200_manual.pdf" http://127.0.0.1:8000/documents/ingest
curl.exe -F "file=@ct200_manual_v2.pdf" http://127.0.0.1:8000/documents/ingest
curl.exe "http://127.0.0.1:8000/documents/{document_id}/sections"
curl.exe "http://127.0.0.1:8000/documents/{document_id}/search?q=battery"
curl.exe -X POST "http://127.0.0.1:8000/generations?selection_id={selection_id}&provider=mock"
curl.exe "http://127.0.0.1:8000/selections/{selection_id}/staleness"
```

