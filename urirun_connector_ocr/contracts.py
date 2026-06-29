# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.
"""Route contracts for the ocr connector — text extraction from documents/images, read-only."""
from __future__ import annotations

from urirun_connectors_toolkit.contract_gate import Contract

_TEXT_RESULT = {
    "ok": "bool",
    "connector": "const:ocr",
    "text": "?str",
    "backend": "?str",
    "chars": "?int",
    "bytes": "?int",
    "path": "?str",
    "truncated": "?bool",
    "input": "?obj",
    "image": "?str",
    "source": "?obj",
    "kind": "?str",
    "live": "?bool",
    "error": "?str",
}

_BATCH_RESULT = {
    "ok": "bool",
    "connector": "const:ocr",
    "root": "?str",
    "pattern": "?str",
    "recursive": "?bool",
    "extensions": "?list",
    "count": "?int",
    "ok_count": "?int",
    "failed_count": "?int",
    "results": "?list",
    "reports": "?obj",
    "kind": "?str",
    "live": "?bool",
    "error": "?str",
}

CONTRACTS: dict[str, Contract] = {
    "backend/query/probe": Contract(
        version="v1",
        effect="query",
        reversible=False,
        inp={"source_paths": "?str"},
        out={"ok": "bool", "tools": "obj", "modules": "obj",
             "source_paths": "list", "ai_auto_enabled": "bool"},
        errors=(),
        examples=(
            {
                "payload": {},
                "result": {
                    "ok": True,
                    "connector": "ocr",
                    "tools": {"tesseract": "/usr/bin/tesseract", "pdftotext": ""},
                    "modules": {"paddleocr": True, "fitz": False},
                    "source_paths": [],
                    "ai_auto_enabled": False,
                },
            },
        ),
    ),
    "document/query/text": Contract(
        version="v1",
        effect="query",
        reversible=False,
        inp={"path": "?str", "bytes_b64": "?str", "filename": "?str",
             "backend": "?str", "lang": "?str", "max_chars": "?int"},
        out=_TEXT_RESULT,
        errors=("precondition-unmet",),
        examples=(
            {
                "payload": {"path": "/tmp/document.pdf", "backend": "auto"},
                "result": {
                    "ok": True,
                    "connector": "ocr",
                    "text": "Invoice total: 100.00",
                    "backend": "pdftotext",
                    "chars": 21,
                    "path": "/tmp/document.pdf",
                    "truncated": False,
                },
            },
        ),
    ),
    "document/query/text_from_uri": Contract(
        version="v1",
        effect="query",
        reversible=False,
        inp={"source_node_url": "?str", "source_uri": "?str",
             "source_payload_json": "?str", "backend": "?str", "lang": "?str"},
        out=_TEXT_RESULT,
        errors=("precondition-unmet", "unreachable"),
        examples=(
            {
                "payload": {"source_uri": "fs://host/file/query/blob",
                            "source_payload_json": "{\"path\": \"/tmp/doc.pdf\"}"},
                "result": {
                    "ok": True,
                    "connector": "ocr",
                    "text": "Hello world",
                    "backend": "pdftotext",
                    "chars": 11,
                    "path": "/tmp/doc.pdf",
                    "truncated": False,
                    "source": {"uri": "fs://host/file/query/blob"},
                },
            },
        ),
    ),
    "image/query/text": Contract(
        version="v1",
        effect="query",
        reversible=False,
        inp={"image": "str", "backend": "?str", "lang": "?str",
             "max_chars": "?int", "max_boxes": "?int", "smart_crop": "?bool"},
        out=_TEXT_RESULT,
        errors=("precondition-unmet",),
        examples=(
            {
                "payload": {"image": "/tmp/photo.jpg"},
                "result": {
                    "ok": True,
                    "connector": "ocr",
                    "text": "Receipt total 42 PLN",
                    "backend": "paddle",
                    "chars": 20,
                    "image": "/tmp/photo.jpg",
                    "truncated": False,
                },
            },
        ),
    ),
    "image/latest/query/text": Contract(
        version="v1",
        effect="query",
        reversible=False,
        inp={"image": "?str", "backend": "?str", "lang": "?str",
             "max_chars": "?int", "max_boxes": "?int", "smart_crop": "?bool"},
        out=_TEXT_RESULT,
        errors=("precondition-unmet",),
        examples=(
            {
                "payload": {},
                "result": {
                    "ok": True,
                    "connector": "ocr",
                    "text": "Latest capture text",
                    "backend": "paddle",
                    "chars": 19,
                    "image": "/tmp/latest.jpg",
                    "truncated": False,
                },
            },
        ),
    ),
    "document/query/batch": Contract(
        version="v1",
        effect="query",
        reversible=False,
        inp={"root": "?str", "pattern": "?str", "recursive": "?bool",
             "extensions": "?str", "backend": "?str", "lang": "?str"},
        out=_BATCH_RESULT,
        errors=("precondition-unmet",),
        examples=(
            {
                "payload": {"root": "/tmp/docs", "pattern": "*.pdf"},
                "result": {
                    "ok": True,
                    "connector": "ocr",
                    "root": "/tmp/docs",
                    "pattern": "*.pdf",
                    "recursive": True,
                    "extensions": ["pdf"],
                    "results": [],
                    "count": 0,
                    "ok_count": 0,
                    "failed_count": 0,
                },
            },
        ),
    ),
}
