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


def _smart_crop_target(path: Path, output_dir: str = "") -> tuple[Path, dict[str, Any]]:
    try:
        from urirun_connector_smart_crop import detect_document_crop
    except Exception as exc:  # noqa: BLE001
        return path, {"ok": False, "reason": f"smart-crop connector unavailable: {exc}", "originalPath": str(path)}

    crop = detect_document_crop(path, output_dir=output_dir or None)
    if crop.get("ok") and crop.get("path"):
        return Path(str(crop["path"])).expanduser().resolve(), crop
    return path, crop


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


_PADDLE_OCR_CACHE: dict[tuple[bool, bool, str, str, str], Any] = {}


def _paddle_truthy(env_name: str, default: str = "1") -> bool:
    return str(os.getenv(env_name, default)).strip().lower() in {"1", "true", "yes", "on"}


def _paddle_instance(*, orientation: bool, unwarp: bool, lang: str) -> Any:
    """Build (once) and cache a PaddleOCR pipeline.

    The instance is expensive to construct (model load ~20s), so it is memoised per
    (orientation, unwarp, lang, det_model, rec_model) combination. ``enable_mkldnn=False``
    is mandatory: the default oneDNN/PIR executor crashes on these models in this venv with
    ``ConvertPirAttribute2RuntimeAttribute`` (paddle 3.3.x). See the smart-crop note.

    ``URI_OCR_PADDLE_DET_MODEL`` / ``URI_OCR_PADDLE_REC_MODEL`` override the detection and
    recognition models — set them to ``*_mobile_*`` variants (e.g. ``PP-OCRv5_mobile_det``)
    for a faster, lower-accuracy read on slow CPUs.
    """
    det_model = str(os.getenv("URI_OCR_PADDLE_DET_MODEL", "")).strip()
    rec_model = str(os.getenv("URI_OCR_PADDLE_REC_MODEL", "")).strip()
    key = (orientation, unwarp, lang, det_model, rec_model)
    cached = _PADDLE_OCR_CACHE.get(key)
    if cached is not None:
        return cached
    # Avoid the model-source connectivity probe (it blocks and prints to stdout).
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddleocr import PaddleOCR  # type: ignore

    kwargs: dict[str, Any] = {
        "use_doc_orientation_classify": orientation,
        "use_doc_unwarping": unwarp,
        "enable_mkldnn": False,
    }
    if lang:
        kwargs["lang"] = lang
    if det_model:
        kwargs["text_detection_model_name"] = det_model
    if rec_model:
        kwargs["text_recognition_model_name"] = rec_model
    instance = PaddleOCR(**kwargs)
    _PADDLE_OCR_CACHE[key] = instance
    return instance


def _paddle_image(path: Path, lang: str, max_chars: int, max_boxes: int = 250) -> dict[str, Any]:
    """OCR a full-frame image with PaddleOCR (PP-OCRv5/v6 det+rec + doc preprocessing).

    Runs on the *whole* frame so document text is never lost to an aggressive crop,
    and applies document orientation + (optional) UVDoc dewarping first. Controlled by
    ``URI_OCR_DISABLE_PADDLE`` (skip), ``URI_OCR_PADDLE_UNWARP`` (dewarp, default on),
    ``URI_OCR_PADDLE_ORIENT`` (orientation, default on) and ``URI_OCR_PADDLE_LANG``.
    """
    import contextlib

    if _paddle_truthy("URI_OCR_DISABLE_PADDLE", "0"):
        return {"ok": False, "backend": "paddle", "error": "paddle disabled via URI_OCR_DISABLE_PADDLE"}
    # The shared lang string is tesseract-style ("eng+pol"); PaddleOCR's default
    # multilingual recognizer already reads Latin/Polish, so only force a lang when
    # the operator asks for one explicitly.
    paddle_lang = str(os.getenv("URI_OCR_PADDLE_LANG", "")).strip()
    orientation = _paddle_truthy("URI_OCR_PADDLE_ORIENT", "1")
    unwarp = _paddle_truthy("URI_OCR_PADDLE_UNWARP", "1")
    try:
        ocr = _paddle_instance(orientation=orientation, unwarp=unwarp, lang=paddle_lang)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "paddle", "error": f"paddle unavailable: {exc}"}

    try:
        # PaddleOCR chatters to stdout; keep the JSON return channel clean.
        with contextlib.redirect_stdout(sys.stderr):
            prediction = ocr.predict(str(path))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "paddle", "error": f"paddle predict failed: {exc}"}

    if not prediction:
        return {"ok": False, "backend": "paddle", "error": "paddle returned no result"}
    res = prediction[0]
    texts = list(res.get("rec_texts") or [])
    scores = list(res.get("rec_scores") or [])
    polys = res.get("rec_polys")
    boxes: list[dict[str, Any]] = []
    for idx, line in enumerate(texts[:max_boxes]):
        box: dict[str, Any] = {"text": line}
        if idx < len(scores):
            box["score"] = round(float(scores[idx]), 4)
        if polys is not None and idx < len(polys):
            try:
                box["poly"] = [[int(p[0]), int(p[1])] for p in polys[idx]]
            except (TypeError, ValueError, IndexError):
                pass
        boxes.append(box)
    text, truncated = _clip("\n".join(texts), max_chars)
    pre = res.get("doc_preprocessor_res") or {}
    return {
        "ok": bool(texts),
        "backend": "paddle",
        "path": str(path),
        "text": text,
        "chars": len(text),
        "truncated": truncated,
        "boxes": boxes,
        "box_count": len(texts),
        "docPreprocess": {
            "orientation": orientation,
            "unwarp": unwarp,
            "angle": pre.get("angle"),
        },
        "error": None if texts else "paddle found no text",
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


def _failed_attempts_result(kind: str, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Return one actionable failure while preserving every backend attempt."""
    if not attempts:
        return {"ok": False, "backend": "auto", "error": f"no {kind} OCR attempts", "attempts": []}
    compact = [
        {"backend": item.get("backend"), "ok": item.get("ok"), "error": item.get("error")}
        for item in attempts
    ]
    errors = [
        f"{item.get('backend') or 'backend'}: {item.get('error') or 'failed'}"
        for item in compact
        if not item.get("ok")
    ]
    return {
        "ok": False,
        "backend": "auto",
        "error": "; ".join(errors) or f"{kind} OCR failed",
        "attempts": compact,
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
    # Paddle first: it OCRs the full frame (no crop-loss) with doc orientation/dewarp
    # and reads Polish receipts far more reliably. Evaluated lazily so the cheaper
    # fallbacks only run when paddle is unavailable or finds nothing. It returns
    # ok=False quickly when paddle is not installed or disabled.
    providers = [
        lambda: _paddle_image(path, lang, max_chars, max_boxes),
        lambda: _imgl_image_text(path, lang, max_chars, max_boxes, source_paths),
        lambda: _tesseract_image(path, lang, max_chars, timeout),
        lambda: _img2nl_image_text(path, max_chars, source_paths),
    ]
    attempts: list[dict[str, Any]] = []
    for provider in providers:
        result = provider()
        attempts.append(result)
        if result.get("ok") and str(result.get("text") or "").strip():
            result["attempts"] = [{"backend": item.get("backend"), "ok": item.get("ok")} for item in attempts]
            return result
    return _failed_attempts_result("image", attempts)


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

    return _failed_attempts_result("document", attempts)


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
        "paddleocr": _module_probe("paddleocr", source_paths),
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
    smart_crop: bool = False,
    crop_output_dir: str = "",
    crop_fail_if_uncertain: bool = False,
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

    smart_crop_meta: dict[str, Any] | None = None
    ocr_target = target
    if smart_crop and target.suffix.lower() in IMAGE_EXTS:
        ocr_target, smart_crop_meta = _smart_crop_target(target, crop_output_dir or output_dir)
        if crop_fail_if_uncertain and not smart_crop_meta.get("ok"):
            if temp_dir is not None:
                temp_dir.cleanup()
            return urirun.fail(
                str(smart_crop_meta.get("reason", "smart crop failed")),
                connector=CONNECTOR_ID,
                input=original,
                smartCrop=smart_crop_meta,
            )

    try:
        selected = backend.strip().lower() or "auto"
        if selected == "auto":
            result = _document_auto(ocr_target, lang, max_chars, source_paths, output_dir, timeout)
        elif selected == "pdftotext":
            result = _pdftotext(ocr_target, max_chars, timeout)
        elif selected == "pymupdf":
            result = _pymupdf_text(ocr_target, max_chars)
        elif selected == "tesseract":
            result = _tesseract_image(ocr_target, lang, max_chars, timeout)
        elif selected in {"paddle", "paddleocr"}:
            result = _paddle_image(ocr_target, lang, max_chars)
        elif selected == "imgl":
            result = _imgl_image_text(ocr_target, lang, max_chars, max_boxes=250, source_paths=source_paths)
        elif selected == "img2nl":
            result = _img2nl_image_text(ocr_target, max_chars, source_paths)
        elif selected in {"wronai", "wronai-ocr", "pdf-ocr"}:
            result = _wronai_pdf_ocr(ocr_target, max_chars, output_dir, source_paths, lang, timeout)
        else:
            return urirun.fail(f"unsupported OCR backend: {backend}", connector=CONNECTOR_ID, path=str(target))
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    extra = {"smartCrop": smart_crop_meta, "originalPath": str(target), "ocrPath": str(ocr_target)} if smart_crop_meta is not None else {}
    if result.get("ok"):
        return urirun.tag(urirun.ok(connector=CONNECTOR_ID, input=original, **extra, **_payload(result)), "text")
    return urirun.fail(str(result.get("error", "OCR failed")), connector=CONNECTOR_ID, input=original, **extra, **_payload(result))


@conn.handler("document/query/text_from_uri", isolated=True, meta={"label": "Extract text from a document fetched by URI", "cliAlias": "text-from-uri"})
def document_text_from_uri(
    source_node_url: str = "",
    source_uri: str = "fs://host/file/query/blob",
    source_payload_json: str = "{}",
    backend: str = "auto",
    lang: str = "eng+pol",
    max_chars: int = 20000,
    max_input_bytes: int = 10 * 1024 * 1024,
    smart_crop: bool = False,
    crop_output_dir: str = "",
    crop_fail_if_uncertain: bool = False,
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
        smart_crop=smart_crop,
        crop_output_dir=crop_output_dir,
        crop_fail_if_uncertain=crop_fail_if_uncertain,
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
    smart_crop: bool = False,
    crop_output_dir: str = "",
    crop_fail_if_uncertain: bool = False,
    source_paths: str = "",
    timeout: int = 60,
) -> dict[str, Any]:
    """OCR/analyze a local image path, returning text and boxes where available."""
    if not image:
        return urirun.fail("image is required", connector=CONNECTOR_ID)
    target = _path(image)
    if not target.is_file():
        return urirun.fail(f"image not found: {target}", connector=CONNECTOR_ID, image=str(target))

    smart_crop_meta: dict[str, Any] | None = None
    ocr_target = target
    if smart_crop:
        ocr_target, smart_crop_meta = _smart_crop_target(target, crop_output_dir)
        if crop_fail_if_uncertain and not smart_crop_meta.get("ok"):
            return urirun.fail(
                str(smart_crop_meta.get("reason", "smart crop failed")),
                connector=CONNECTOR_ID,
                image=str(target),
                smartCrop=smart_crop_meta,
            )

    selected = backend.strip().lower() or "auto"
    if selected == "auto":
        result = _image_auto(ocr_target, lang, max_chars, max_boxes, source_paths, timeout)
    elif selected in {"paddle", "paddleocr"}:
        result = _paddle_image(ocr_target, lang, max_chars, max_boxes)
    elif selected == "imgl":
        result = _imgl_image_text(ocr_target, lang, max_chars, max_boxes, source_paths)
    elif selected == "tesseract":
        result = _tesseract_image(ocr_target, lang, max_chars, timeout)
    elif selected == "img2nl":
        result = _img2nl_image_text(ocr_target, max_chars, source_paths)
    else:
        return urirun.fail(f"unsupported image OCR backend: {backend}", connector=CONNECTOR_ID, image=str(target))

    extra = {"smartCrop": smart_crop_meta, "originalPath": str(target), "ocrPath": str(ocr_target)} if smart_crop_meta is not None else {}
    if result.get("ok"):
        return urirun.tag(urirun.ok(connector=CONNECTOR_ID, image=str(target), **extra, **_payload(result)), "text")
    return urirun.fail(
        str(result.get("error", "image OCR failed")),
        connector=CONNECTOR_ID,
        image=str(target),
        **extra,
        **_payload(result),
    )


@conn.handler("image/latest/query/text", isolated=True, meta={"label": "Extract text from latest image"})
def image_latest_text(
    image: str = "",
    backend: str = "auto",
    lang: str = "eng+pol",
    max_chars: int = 12000,
    max_boxes: int = 250,
    smart_crop: bool = False,
    crop_output_dir: str = "",
    crop_fail_if_uncertain: bool = False,
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
        smart_crop=smart_crop,
        crop_output_dir=crop_output_dir,
        crop_fail_if_uncertain=crop_fail_if_uncertain,
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
    smart_crop: bool = False,
    crop_output_dir: str = "",
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
            smart_crop=smart_crop,
            crop_output_dir=crop_output_dir,
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
    return urirun.tag(urirun.ok(**{k: v for k, v in report.items() if k != "ok"}), "text-batch")


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
