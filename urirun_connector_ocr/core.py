# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.

"""OCR connector for urirun.

The route surface is intentionally small:

* ``ocr://host/backend/query/probe``      -- report available OCR backends
* ``ocr://host/document/query/text``      -- extract text from a PDF/image/text file
* ``ocr://host/image/query/text``         -- OCR/analyze an image, optionally via imgl
* ``ocr://host/image/latest/query/text``  -- same, using ``URI_OCR_LATEST_IMAGE``

Backend order in ``auto`` mode is conservative. It uses cheap deterministic
extractors first (``pdftotext``, PyMuPDF text, tesseract/imgl for images) and only
calls the heavier wronai/ocr Ollama pipeline when explicitly requested or when
``URI_OCR_ENABLE_AI=1``. Optional local source checkouts are discovered from
``URI_OCR_SOURCE_PATHS`` plus the project locations used in this workspace.
"""

from __future__ import annotations

import base64
import binascii
import importlib
import csv
import json
import os
import hashlib
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import urirun

CONNECTOR_ID = "ocr"
conn = urirun.connector(CONNECTOR_ID, scheme="ocr")

PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
TEXT_EXTS = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"}


def _default_source_paths() -> list[Path]:
    """Known local source checkouts that provide OCR/layout functionality."""
    return [
        Path("/home/tom/github/semcod/imgl"),
        Path("/home/tom/github/semcod/imgl/imgl"),
        Path("/home/tom/github/wronai/imgl"),
        Path("/home/tom/github/wronai/img2nl/src"),
        Path("/home/tom/github/wronai/ocr"),
        Path("/home/tom/github/oqlos/vql/src"),
    ]


def _split_paths(raw: str) -> list[str]:
    items: list[str] = []
    for chunk in raw.replace(",", os.pathsep).split(os.pathsep):
        chunk = chunk.strip()
        if chunk:
            items.append(chunk)
    return items


def _split_words(raw: str) -> list[str]:
    return [item.strip().lower() for item in raw.replace(";", ",").split(",") if item.strip()]


def _extend_source_paths(source_paths: str = "") -> list[str]:
    """Add optional sibling source checkouts to sys.path, returning paths found."""
    raw_paths = _split_paths(os.getenv("URI_OCR_SOURCE_PATHS", ""))
    raw_paths.extend(_split_paths(source_paths))
    raw_paths.extend(str(path) for path in _default_source_paths())

    found: list[str] = []
    for raw in raw_paths:
        path = Path(raw).expanduser()
        if not path.exists():
            continue
        resolved = str(path.resolve())
        found.append(resolved)
        if resolved not in sys.path:
            sys.path.insert(0, resolved)
    return found


def _module_probe(module: str, source_paths: str = "") -> dict[str, Any]:
    _extend_source_paths(source_paths)
    try:
        mod = importlib.import_module(module)
        return {"available": True, "module": module, "file": getattr(mod, "__file__", "")}
    except Exception as exc:  # noqa: BLE001 - probe should never raise
        return {"available": False, "module": module, "error": f"{type(exc).__name__}: {exc}"}


def _tool_probe(tool: str) -> dict[str, Any]:
    path = shutil.which(tool)
    return {"available": bool(path), "tool": tool, "path": path or ""}


def _path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _clip(text: str, max_chars: int) -> tuple[str, bool]:
    limit = max(0, int(max_chars))
    if not limit or len(text) <= limit:
        return text, False
    return text[:limit], True


def _run(argv: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in result.items() if k not in {"ok", "error", "connector"}}


def _decode_bytes_b64(bytes_b64: str, max_input_bytes: int) -> bytes:
    try:
        raw = base64.b64decode(bytes_b64.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError(f"invalid bytes_b64: {exc}") from exc
    if len(raw) > max_input_bytes:
        raise ValueError(f"bytes_b64 exceeds max_input_bytes ({len(raw)} > {max_input_bytes})")
    return raw


def _suffix_for_filename(filename: str) -> str:
    suffix = Path(filename or "document.bin").suffix.lower()
    if suffix and len(suffix) <= 16 and all(ch.isalnum() or ch in "._-" for ch in suffix):
        return suffix
    return ".bin"


def _route_value(envelope: dict[str, Any]) -> dict[str, Any]:
    result = envelope.get("result") or {}
    if isinstance(result, dict) and isinstance(result.get("value"), dict):
        return result["value"]
    if isinstance(result, dict) and isinstance(result.get("response"), dict):
        return result["response"]
    return result if isinstance(result, dict) else {}


def _post_uri_run(node_url: str, uri: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    body = json.dumps({"uri": uri, "payload": payload}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = os.getenv("URIRUN_RUN_TOKEN", "")
    if token:
        headers["X-Urirun-Token"] = token
    request = urllib.request.Request(
        node_url.rstrip("/") + "/run",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            data = {"ok": False, "error": raw}
        data.setdefault("ok", False)
        data.setdefault("status", exc.code)
        return data


def _write_json_report(path: str, data: dict[str, Any]) -> str:
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(out)


def _write_csv_report(path: str, rows: list[dict[str, Any]]) -> str:
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["path", "ok", "backend", "chars", "truncated", "error"]
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    return str(out)


def _read_text_file(path: Path, max_chars: int) -> dict[str, Any]:
    raw = path.read_bytes()
    text, truncated = _clip(raw.decode("utf-8", "replace"), max_chars)
    return {
        "ok": True,
        "backend": "text-file",
        "path": str(path),
        "text": text,
        "chars": len(text),
        "bytes": len(raw),
        "truncated": truncated,
    }


def _pdftotext(path: Path, max_chars: int, timeout: int) -> dict[str, Any]:
    if not shutil.which("pdftotext"):
        return {"ok": False, "backend": "pdftotext", "error": "pdftotext is not installed"}
    proc = _run(["pdftotext", "-layout", str(path), "-"], timeout=timeout)
    if proc.returncode != 0:
        return {
            "ok": False,
            "backend": "pdftotext",
            "error": (proc.stderr or f"pdftotext exited {proc.returncode}").strip(),
        }
    text, truncated = _clip(proc.stdout, max_chars)
    return {
        "ok": True,
        "backend": "pdftotext",
        "path": str(path),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
    }


def _pymupdf_text(path: Path, max_chars: int) -> dict[str, Any]:
    try:
        import fitz  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "pymupdf", "error": f"PyMuPDF unavailable: {exc}"}

    chunks: list[str] = []
    try:
        with fitz.open(path) as doc:
            for page in doc:
                chunks.append(page.get_text("text"))
                if sum(len(item) for item in chunks) >= max_chars:
                    break
            page_count = doc.page_count
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "pymupdf", "error": str(exc)}

    text, truncated = _clip("\n".join(chunks), max_chars)
    return {
        "ok": True,
        "backend": "pymupdf",
        "path": str(path),
        "text": text,
        "chars": len(text),
        "pages": page_count,
        "truncated": truncated,
    }


def _tesseract_image(path: Path, lang: str, max_chars: int, timeout: int) -> dict[str, Any]:
    if not shutil.which("tesseract"):
        return {"ok": False, "backend": "tesseract", "error": "tesseract is not installed"}
    argv = ["tesseract", str(path), "stdout"]
    if lang:
        argv.extend(["-l", lang])
    proc = _run(argv, timeout=timeout)
    if proc.returncode != 0 and "+" in lang:
        fallback_lang = lang.split("+", 1)[0]
        proc = _run(["tesseract", str(path), "stdout", "-l", fallback_lang], timeout=timeout)
    if proc.returncode != 0:
        return {
            "ok": False,
            "backend": "tesseract",
            "error": (proc.stderr or f"tesseract exited {proc.returncode}").strip(),
        }
    text, truncated = _clip(proc.stdout, max_chars)
    return {
        "ok": True,
        "backend": "tesseract",
        "path": str(path),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
    }


def _imgl_image_text(
    path: Path,
    lang: str,
    max_chars: int,
    max_boxes: int,
    source_paths: str,
) -> dict[str, Any]:
    _extend_source_paths(source_paths)
    try:
        from imgl import analyze  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "imgl", "error": f"imgl unavailable: {exc}"}

    try:
        scene = analyze(str(path), lang=lang)
        data = scene.to_dict()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "imgl", "error": str(exc)}

    boxes = data.get("ocr_boxes") or []
    text = " ".join(str(box.get("text", "")).strip() for box in boxes if box.get("text"))
    text, truncated = _clip(text, max_chars)
    return {
        "ok": True,
        "backend": "imgl",
        "path": str(path),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "boxes": boxes[:max_boxes],
        "box_count": len(boxes),
        "scene": data.get("scene", {}),
        "metadata": data.get("metadata", {}),
    }


def _img2nl_image_text(path: Path, max_chars: int, source_paths: str) -> dict[str, Any]:
    _extend_source_paths(source_paths)
    try:
        from img2nl.api import analyze_image  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "img2nl", "error": f"img2nl unavailable: {exc}"}

    try:
        result = analyze_image(
            str(path),
            skip_thumbnail=True,
            source_type="screenshot",
            goal="describe",
            enable_ui_detect=True,
        )
        data = result.to_dict()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "img2nl", "error": str(exc)}

    special = ((data.get("features") or {}).get("special_hits") or {})
    ocr = special.get("ocr") or {}
    lines = [str(line) for line in ocr.get("lines", [])]
    text = "\n".join(lines).strip() or str(data.get("text") or "")
    text, truncated = _clip(text, max_chars)
    return {
        "ok": bool(data.get("ok")),
        "backend": "img2nl",
        "path": str(path),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "ocr": ocr,
        "description": data.get("text", ""),
        "error": data.get("error"),
    }


def _wronai_pdf_ocr(
    path: Path,
    max_chars: int,
    output_dir: str,
    source_paths: str,
    lang: str,
    timeout: int,
) -> dict[str, Any]:
    _extend_source_paths(source_paths)
    try:
        from pdf_processor.processing.pdf_processor import PDFProcessor, PDFProcessorConfig  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "wronai-ocr", "error": f"wronai/ocr unavailable: {exc}"}

    out_dir = Path(output_dir).expanduser() if output_dir else Path(tempfile.mkdtemp(prefix="urirun-ocr-"))
    try:
        config = PDFProcessorConfig(
            input_path=path,
            output_dir=out_dir,
            language=lang or "polish",
            timeout=max(30, timeout),
            save_images=False,
            save_svg=False,
            save_text=True,
        )
        result = PDFProcessor(config).process_pdf(path)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "wronai-ocr", "error": str(exc), "output_dir": str(out_dir)}

    text_parts: list[str] = []
    for item in result.get("output_files", []):
        if item.get("type") and "text" in item.get("type", ""):
            file_path = Path(item.get("path", ""))
            if file_path.is_file():
                text_parts.append(file_path.read_text(encoding="utf-8", errors="replace"))

    if not text_parts:
        for file_path in out_dir.rglob("*.txt"):
            text_parts.append(file_path.read_text(encoding="utf-8", errors="replace"))

    text, truncated = _clip("\n\n".join(text_parts), max_chars)
    return {
        "ok": result.get("status") == "completed",
        "backend": "wronai-ocr",
        "path": str(path),
        "output_dir": str(out_dir),
        "processor": result,
        "text": text,
        "chars": len(text),
        "truncated": truncated,
    }


def _image_auto(
    path: Path,
    lang: str,
    max_chars: int,
    max_boxes: int,
    source_paths: str,
    timeout: int,
) -> dict[str, Any]:
    attempts = [
        _imgl_image_text(path, lang, max_chars, max_boxes, source_paths),
        _tesseract_image(path, lang, max_chars, timeout),
        _img2nl_image_text(path, max_chars, source_paths),
    ]
    for result in attempts:
        if result.get("ok") and str(result.get("text") or "").strip():
            result["attempts"] = [{"backend": item.get("backend"), "ok": item.get("ok")} for item in attempts]
            return result
    fallback = attempts[0] if attempts else {"ok": False, "error": "no image OCR attempts"}
    fallback["attempts"] = [
        {"backend": item.get("backend"), "ok": item.get("ok"), "error": item.get("error")}
        for item in attempts
    ]
    return fallback


def _document_auto(
    path: Path,
    lang: str,
    max_chars: int,
    source_paths: str,
    output_dir: str,
    timeout: int,
) -> dict[str, Any]:
    ext = path.suffix.lower()
    if ext in TEXT_EXTS:
        return _read_text_file(path, max_chars)
    if ext in IMAGE_EXTS:
        return _image_auto(path, lang, max_chars, max_boxes=250, source_paths=source_paths, timeout=timeout)
    if ext not in PDF_EXTS:
        return {"ok": False, "backend": "auto", "error": f"unsupported file type: {ext or 'none'}"}

    attempts = [_pdftotext(path, max_chars, timeout), _pymupdf_text(path, max_chars)]
    if os.getenv("URI_OCR_ENABLE_AI") == "1":
        attempts.append(_wronai_pdf_ocr(path, max_chars, output_dir, source_paths, lang, timeout))

    for result in attempts:
        if result.get("ok") and str(result.get("text") or "").strip():
            result["attempts"] = [{"backend": item.get("backend"), "ok": item.get("ok")} for item in attempts]
            return result

    fallback = attempts[0] if attempts else {"ok": False, "error": "no document OCR attempts"}
    fallback["attempts"] = [
        {"backend": item.get("backend"), "ok": item.get("ok"), "error": item.get("error")}
        for item in attempts
    ]
    return fallback


def _iter_document_files(
    root: Path,
    *,
    pattern: str,
    recursive: bool,
    extensions: str,
    max_files: int,
) -> list[Path]:
    allowed_exts = set(_split_words(extensions))
    if allowed_exts:
        allowed_exts = {ext if ext.startswith(".") else f".{ext}" for ext in allowed_exts}
    else:
        allowed_exts = PDF_EXTS | IMAGE_EXTS | TEXT_EXTS

    patterns = _split_words(pattern) or ["*"]
    seen: set[Path] = set()
    files: list[Path] = []
    for pat in patterns:
        iterator = root.rglob(pat) if recursive else root.glob(pat)
        for candidate in iterator:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_file():
                continue
            if resolved.suffix.lower() not in allowed_exts:
                continue
            seen.add(resolved)
            files.append(resolved)
            if max_files and len(files) >= max_files:
                return sorted(files)
    return sorted(files)


@conn.handler("backend/query/probe", isolated=True, meta={"label": "Probe OCR backends", "cliAlias": "probe"})
def ocr_probe(source_paths: str = "") -> dict[str, Any]:
    """Report system tools and optional Python OCR/layout backends available."""
    found_paths = _extend_source_paths(source_paths)
    tools = {
        name: _tool_probe(name)
        for name in ("pdftotext", "pdfimages", "pdftoppm", "magick", "tesseract", "ocrmypdf", "ollama")
    }
    modules = {
        "imgl": _module_probe("imgl", source_paths),
        "img2nl": _module_probe("img2nl", source_paths),
        "pdf_processor": _module_probe("pdf_processor", source_paths),
        "vql": _module_probe("vql", source_paths),
        "fitz": _module_probe("fitz", source_paths),
        "rapidocr_onnxruntime": _module_probe("rapidocr_onnxruntime", source_paths),
    }
    return urirun.ok(
        connector=CONNECTOR_ID,
        tools=tools,
        modules=modules,
        source_paths=found_paths,
        ai_auto_enabled=os.getenv("URI_OCR_ENABLE_AI") == "1",
    )


@conn.handler("document/query/text", isolated=True, meta={"label": "Extract text from a document", "cliAlias": "text"})
def document_text(
    path: str = "",
    bytes_b64: str = "",
    filename: str = "",
    backend: str = "auto",
    lang: str = "eng+pol",
    max_chars: int = 20000,
    max_input_bytes: int = 10 * 1024 * 1024,
    output_dir: str = "",
    source_paths: str = "",
    timeout: int = 90,
) -> dict[str, Any]:
    """Extract text from a local PDF, image, or UTF-8-ish text file."""
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if bytes_b64:
        try:
            raw = _decode_bytes_b64(bytes_b64, max_input_bytes)
        except ValueError as exc:
            return urirun.fail(str(exc), connector=CONNECTOR_ID)
        temp_dir = tempfile.TemporaryDirectory(prefix="urirun-ocr-input-")
        target = Path(temp_dir.name) / f"input{_suffix_for_filename(filename)}"
        target.write_bytes(raw)
        original = {"filename": filename or target.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    else:
        if not path:
            return urirun.fail("path or bytes_b64 is required", connector=CONNECTOR_ID)
        target = _path(path)
        if not target.is_file():
            return urirun.fail(f"file not found: {target}", connector=CONNECTOR_ID, path=str(target))
        original = {"filename": filename or target.name, "bytes": target.stat().st_size}

    try:
        selected = backend.strip().lower() or "auto"
        if selected == "auto":
            result = _document_auto(target, lang, max_chars, source_paths, output_dir, timeout)
        elif selected == "pdftotext":
            result = _pdftotext(target, max_chars, timeout)
        elif selected == "pymupdf":
            result = _pymupdf_text(target, max_chars)
        elif selected == "tesseract":
            result = _tesseract_image(target, lang, max_chars, timeout)
        elif selected == "imgl":
            result = _imgl_image_text(target, lang, max_chars, max_boxes=250, source_paths=source_paths)
        elif selected == "img2nl":
            result = _img2nl_image_text(target, max_chars, source_paths)
        elif selected in {"wronai", "wronai-ocr", "pdf-ocr"}:
            result = _wronai_pdf_ocr(target, max_chars, output_dir, source_paths, lang, timeout)
        else:
            return urirun.fail(f"unsupported OCR backend: {backend}", connector=CONNECTOR_ID, path=str(target))
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    if result.get("ok"):
        return urirun.ok(connector=CONNECTOR_ID, input=original, **_payload(result))
    return urirun.fail(str(result.get("error", "OCR failed")), connector=CONNECTOR_ID, input=original, **_payload(result))


@conn.handler("document/query/text_from_uri", isolated=True, meta={"label": "Extract text from a document fetched by URI", "cliAlias": "text-from-uri"})
def document_text_from_uri(
    source_node_url: str = "",
    source_uri: str = "fs://host/file/query/blob",
    source_payload_json: str = "{}",
    backend: str = "auto",
    lang: str = "eng+pol",
    max_chars: int = 20000,
    max_input_bytes: int = 10 * 1024 * 1024,
    source_paths: str = "",
    timeout: int = 120,
) -> dict[str, Any]:
    """Fetch a binary document through another URI and OCR it on this host."""
    node_url = source_node_url or os.getenv("URI_OCR_SOURCE_NODE_URL", "")
    if not node_url:
        return urirun.fail("source_node_url or URI_OCR_SOURCE_NODE_URL is required", connector=CONNECTOR_ID)
    try:
        source_payload = json.loads(source_payload_json or "{}")
    except json.JSONDecodeError as exc:
        return urirun.fail(f"invalid source_payload_json: {exc}", connector=CONNECTOR_ID)
    if not isinstance(source_payload, dict):
        return urirun.fail("source_payload_json must decode to an object", connector=CONNECTOR_ID)

    envelope = _post_uri_run(node_url, source_uri, source_payload, timeout)
    value = _route_value(envelope)
    if not value.get("ok"):
        return urirun.fail(
            str(value.get("error") or envelope.get("error") or "source URI failed"),
            connector=CONNECTOR_ID,
            source_uri=source_uri,
            source_node_url=node_url,
            source=value,
        )
    if not value.get("bytes_b64"):
        return urirun.fail("source URI did not return bytes_b64", connector=CONNECTOR_ID, source=value)

    filename = str(value.get("name") or value.get("path") or "document.bin")
    result = document_text(
        bytes_b64=str(value["bytes_b64"]),
        filename=filename,
        backend=backend,
        lang=lang,
        max_chars=max_chars,
        max_input_bytes=max_input_bytes,
        source_paths=source_paths,
        timeout=timeout,
    )
    if result.get("ok"):
        result["source"] = {
            "node_url": node_url,
            "uri": source_uri,
            "payload": source_payload,
            "path": value.get("path", ""),
            "sha256": value.get("sha256", ""),
            "size": value.get("size", 0),
            "mime": value.get("mime", ""),
        }
    return result


@conn.handler("image/query/text", isolated=True, meta={"label": "Extract text from an image", "cliAlias": "image"})
def image_text(
    image: str = "",
    backend: str = "auto",
    lang: str = "eng+pol",
    max_chars: int = 12000,
    max_boxes: int = 250,
    source_paths: str = "",
    timeout: int = 60,
) -> dict[str, Any]:
    """OCR/analyze a local image path, returning text and boxes where available."""
    if not image:
        return urirun.fail("image is required", connector=CONNECTOR_ID)
    target = _path(image)
    if not target.is_file():
        return urirun.fail(f"image not found: {target}", connector=CONNECTOR_ID, image=str(target))

    selected = backend.strip().lower() or "auto"
    if selected == "auto":
        result = _image_auto(target, lang, max_chars, max_boxes, source_paths, timeout)
    elif selected == "imgl":
        result = _imgl_image_text(target, lang, max_chars, max_boxes, source_paths)
    elif selected == "tesseract":
        result = _tesseract_image(target, lang, max_chars, timeout)
    elif selected == "img2nl":
        result = _img2nl_image_text(target, max_chars, source_paths)
    else:
        return urirun.fail(f"unsupported image OCR backend: {backend}", connector=CONNECTOR_ID, image=str(target))

    if result.get("ok"):
        return urirun.ok(connector=CONNECTOR_ID, image=str(target), **_payload(result))
    return urirun.fail(
        str(result.get("error", "image OCR failed")),
        connector=CONNECTOR_ID,
        image=str(target),
        **_payload(result),
    )


@conn.handler("image/latest/query/text", isolated=True, meta={"label": "Extract text from latest image"})
def image_latest_text(
    image: str = "",
    backend: str = "auto",
    lang: str = "eng+pol",
    max_chars: int = 12000,
    max_boxes: int = 250,
    source_paths: str = "",
    timeout: int = 60,
) -> dict[str, Any]:
    """OCR the latest image path, defaulting to URI_OCR_LATEST_IMAGE."""
    resolved = image or os.getenv("URI_OCR_LATEST_IMAGE", "")
    if not resolved:
        return urirun.fail("image or URI_OCR_LATEST_IMAGE is required", connector=CONNECTOR_ID)
    return image_text(
        image=resolved,
        backend=backend,
        lang=lang,
        max_chars=max_chars,
        max_boxes=max_boxes,
        source_paths=source_paths,
        timeout=timeout,
    )


@conn.handler("document/query/batch", isolated=True, meta={"label": "Batch extract text from documents", "cliAlias": "batch"})
def document_batch(
    root: str = ".",
    pattern: str = "*",
    recursive: bool = True,
    extensions: str = "",
    backend: str = "auto",
    lang: str = "eng+pol",
    max_files: int = 100,
    max_chars_per_file: int = 2000,
    output_json: str = "",
    output_csv: str = "",
    source_paths: str = "",
    timeout: int = 90,
) -> dict[str, Any]:
    """Extract text from many local documents and optionally write JSON/CSV reports."""
    base = _path(root)
    if not base.exists():
        return urirun.fail(f"root not found: {base}", connector=CONNECTOR_ID, root=str(base))
    if not base.is_dir():
        return urirun.fail(f"root is not a directory: {base}", connector=CONNECTOR_ID, root=str(base))

    files = _iter_document_files(
        base,
        pattern=pattern,
        recursive=recursive,
        extensions=extensions,
        max_files=max(0, int(max_files)),
    )
    rows: list[dict[str, Any]] = []
    ok_count = 0
    for file_path in files:
        result = document_text(
            path=str(file_path),
            backend=backend,
            lang=lang,
            max_chars=max_chars_per_file,
            output_dir="",
            source_paths=source_paths,
            timeout=timeout,
        )
        row = {
            "path": str(file_path),
            "ok": bool(result.get("ok")),
            "backend": result.get("backend", ""),
            "chars": result.get("chars", 0),
            "truncated": bool(result.get("truncated", False)),
            "text": result.get("text", ""),
            "error": result.get("error", ""),
        }
        ok_count += 1 if row["ok"] else 0
        rows.append(row)

    report: dict[str, Any] = {
        "ok": True,
        "connector": CONNECTOR_ID,
        "root": str(base),
        "pattern": pattern,
        "recursive": recursive,
        "extensions": sorted((PDF_EXTS | IMAGE_EXTS | TEXT_EXTS) if not extensions else set(_split_words(extensions))),
        "count": len(rows),
        "ok_count": ok_count,
        "failed_count": len(rows) - ok_count,
        "results": rows,
    }
    reports: dict[str, str] = {}
    if output_json:
        reports["json"] = _write_json_report(output_json, report)
    if output_csv:
        reports["csv"] = _write_csv_report(output_csv, rows)
    if reports:
        report["reports"] = reports
    return urirun.ok(**{k: v for k, v in report.items() if k != "ok"})


def urirun_bindings() -> dict[str, Any]:
    """Serializable v2 bindings for this connector."""
    return conn.bindings()


def connector_manifest() -> dict[str, Any]:
    """Full manifest: prose plus derived routes."""
    return conn.manifest(urirun.load_manifest(__package__))


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point."""
    return conn.cli(argv, manifest_prose=urirun.load_manifest(__package__))


if __name__ == "__main__":
    raise SystemExit(main())
