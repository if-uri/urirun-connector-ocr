# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.

from __future__ import annotations

import json
from pathlib import Path

import urirun
from urirun import v2

import urirun_connector_ocr.core as core
from urirun_connector_ocr import (
    connector_manifest,
    document_batch,
    document_text,
    document_text_from_uri,
    image_latest_text,
    image_text,
    ocr_probe,
    urirun_bindings,
)

ROUTE_PROBE = "ocr://host/backend/query/probe"
ROUTE_DOCUMENT = "ocr://host/document/query/text"
ROUTE_TEXT_FROM_URI = "ocr://host/document/query/text_from_uri"
ROUTE_BATCH = "ocr://host/document/query/batch"
ROUTE_IMAGE = "ocr://host/image/query/text"
ROUTE_LATEST = "ocr://host/image/latest/query/text"
ALL_ROUTES = {ROUTE_PROBE, ROUTE_DOCUMENT, ROUTE_TEXT_FROM_URI, ROUTE_BATCH, ROUTE_IMAGE, ROUTE_LATEST}


def test_probe_returns_tools_and_modules() -> None:
    result = ocr_probe()
    assert result["ok"] is True
    assert "pdftotext" in result["tools"]
    assert "imgl" in result["modules"]
    assert isinstance(result["source_paths"], list)


def test_document_text_reads_plain_text(tmp_path: Path) -> None:
    path = tmp_path / "invoice.txt"
    path.write_text("Faktura testowa\nVAT", encoding="utf-8")
    result = document_text(path=str(path))
    assert result["ok"] is True
    assert result["backend"] == "text-file"
    assert "Faktura" in result["text"]


def test_document_text_reads_base64_payload() -> None:
    result = document_text(bytes_b64="RmFrdHVyYSBob3N0IGNvbXB1dGUK", filename="invoice.txt")
    assert result["ok"] is True
    assert result["backend"] == "text-file"
    assert result["input"]["filename"] == "invoice.txt"
    assert result["text"] == "Faktura host compute\n"


def test_document_text_rejects_invalid_base64() -> None:
    result = document_text(bytes_b64="not base64!", filename="invoice.pdf")
    assert result["ok"] is False
    assert "invalid bytes_b64" in result["error"]


def test_document_text_missing_path_is_safe() -> None:
    result = document_text(path="/definitely/missing.pdf")
    assert result["ok"] is False
    assert "file not found" in result["error"]


def test_document_text_pdftotext_backend(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"%PDF-1.4 fake")

    class Proc:
        returncode = 0
        stdout = "Invoice 123"
        stderr = ""

    monkeypatch.setattr(core.shutil, "which", lambda name: "/usr/bin/pdftotext" if name == "pdftotext" else None)
    monkeypatch.setattr(core, "_run", lambda argv, timeout=60: Proc())

    result = document_text(path=str(path), backend="pdftotext")
    assert result["ok"] is True
    assert result["backend"] == "pdftotext"
    assert result["text"] == "Invoice 123"


def test_document_text_from_uri_fetches_blob_and_ocr(monkeypatch) -> None:
    def fake_post(node_url, uri, payload, timeout):
        assert node_url == "http://node"
        assert uri == "fs://host/file/query/blob"
        assert payload == {"path": "invoice.txt"}
        return {
            "ok": True,
            "result": {
                "value": {
                    "ok": True,
                    "path": "invoice.txt",
                    "name": "invoice.txt",
                    "mime": "text/plain",
                    "size": 8,
                    "sha256": "abc",
                    "bytes_b64": "SW52b2ljZQo=",
                }
            },
        }

    monkeypatch.setattr(core, "_post_uri_run", fake_post)
    result = document_text_from_uri(
        source_node_url="http://node",
        source_payload_json='{"path":"invoice.txt"}',
    )

    assert result["ok"] is True
    assert result["backend"] == "text-file"
    assert result["text"] == "Invoice\n"
    assert result["source"]["sha256"] == "abc"


def test_document_batch_reads_folder_and_writes_reports(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("Faktura A", encoding="utf-8")
    (tmp_path / "b.md").write_text("Invoice B", encoding="utf-8")
    (tmp_path / "skip.bin").write_bytes(b"ignored")
    json_report = tmp_path / "report.json"
    csv_report = tmp_path / "report.csv"

    result = document_batch(
        root=str(tmp_path),
        extensions="txt,md",
        output_json=str(json_report),
        output_csv=str(csv_report),
    )

    assert result["ok"] is True
    assert result["count"] == 2
    assert result["ok_count"] == 2
    assert json_report.is_file()
    assert csv_report.is_file()
    saved = json.loads(json_report.read_text(encoding="utf-8"))
    assert saved["count"] == 2
    assert "Faktura A" in {row["text"] for row in result["results"]}


def test_image_text_uses_imgl_backend(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "screen.png"
    path.write_bytes(b"not really a png")
    monkeypatch.setattr(
        core,
        "_imgl_image_text",
        lambda *args, **kwargs: {
            "ok": True,
            "backend": "imgl",
            "path": str(path),
            "text": "LinkedIn",
            "chars": 8,
            "boxes": [{"text": "LinkedIn"}],
            "box_count": 1,
        },
    )

    result = image_text(image=str(path), backend="imgl")
    assert result["ok"] is True
    assert result["backend"] == "imgl"
    assert result["boxes"][0]["text"] == "LinkedIn"


def test_image_latest_uses_env(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "latest.png"
    path.write_bytes(b"x")
    monkeypatch.setenv("URI_OCR_LATEST_IMAGE", str(path))
    monkeypatch.setattr(
        core,
        "_image_auto",
        lambda *args, **kwargs: {"ok": True, "backend": "mock", "text": "latest", "chars": 6},
    )
    result = image_latest_text()
    assert result["ok"] is True
    assert result["text"] == "latest"


def test_bindings_are_isolated_handlers() -> None:
    bindings = urirun_bindings()["bindings"]
    assert set(bindings) == ALL_ROUTES
    for route in ALL_ROUTES:
        assert bindings[route]["adapter"] == "local-function-subprocess"
        assert bindings[route]["python"]["module"] == "urirun_connector_ocr.core"
        assert "argv" not in bindings[route]
    assert bindings[ROUTE_DOCUMENT]["python"]["export"] == "document_text"
    assert bindings[ROUTE_TEXT_FROM_URI]["python"]["export"] == "document_text_from_uri"
    assert bindings[ROUTE_BATCH]["python"]["export"] == "document_batch"
    assert bindings[ROUTE_IMAGE]["python"]["export"] == "image_text"
    json.dumps(urirun_bindings())


def test_runtime_executes_from_compiled_registry(tmp_path: Path) -> None:
    text_path = tmp_path / "doc.txt"
    text_path.write_text("hello from registry", encoding="utf-8")
    registry = urirun.compile_registry(json.loads(json.dumps(urirun_bindings())))
    env = v2.run(
        ROUTE_DOCUMENT,
        registry,
        payload={"path": str(text_path)},
        mode="execute",
        policy=urirun.policy(allow=["ocr://*"]),
    )
    assert env["ok"] is True
    data = urirun.result_data(env)
    assert data["ok"] is True
    assert data["text"] == "hello from registry"


def test_manifest_prose_plus_derived_routes() -> None:
    manifest = connector_manifest()
    assert manifest["id"] == "ocr"
    assert manifest["uriSchemes"] == ["ocr"]
    assert set(manifest["routes"]) == ALL_ROUTES
    assert "imgl" in manifest["keywords"]
    json.dumps(manifest)
