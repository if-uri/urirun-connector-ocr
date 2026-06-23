# Author: Tom Sapletta · https://tom.sapletta.com
# Part of the ifURI solution.

"""urirun OCR connector."""

from .core import (
    connector_manifest,
    document_batch,
    document_text,
    document_text_from_uri,
    image_latest_text,
    image_text,
    main,
    ocr_probe,
    urirun_bindings,
)

__all__ = [
    "connector_manifest",
    "document_batch",
    "document_text",
    "document_text_from_uri",
    "image_latest_text",
    "image_text",
    "main",
    "ocr_probe",
    "urirun_bindings",
]
