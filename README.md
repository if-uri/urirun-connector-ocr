# urirun-connector-ocr

OCR routes for urirun nodes.

The connector uses lightweight local tools first and optional repository
backends when they are available on the node:

- `pdftotext` for text PDFs
- `tesseract` CLI for image OCR
- `semcod/imgl` for screenshot/UI OCR with bounding boxes
- `wronai/img2nl` for image analysis and optional RapidOCR text
- `wronai/ocr` for heavier PDF OCR through PyMuPDF/Ollama
- `oqlos/vql` as the layout schema target used by the imgl pipeline
- `urirun-connector-smart-crop` when `smart_crop=true`, so image OCR runs on a
  detected receipt/document crop instead of the full camera frame

## Routes

- `ocr://host/backend/query/probe`
- `ocr://host/document/query/text`
- `ocr://host/document/query/text_from_uri`
- `ocr://host/document/query/batch`
- `ocr://host/image/query/text`
- `ocr://host/image/latest/query/text`

## Examples

```bash
urirun-ocr probe
urirun-ocr text --path ~/Downloads/invoice.pdf --max_chars 20000
urirun-ocr text --filename invoice.txt --bytes_b64 SW52b2ljZQo=
urirun-ocr batch --root ~/Downloads/2026/5 --extensions pdf,png,jpg --output_json /tmp/ocr.json
urirun-ocr text --path /tmp/screen.png --backend imgl --lang eng+pol
```

Pre-crop a receipt/document before OCR:

```bash
urirun run 'ocr://host/image/query/text' ocr.registry.json \
  --payload '{"image":"/tmp/phone-frame.jpg","smart_crop":true,"backend":"tesseract"}' \
  --execute --allow 'ocr://**'
```

Node usage:

```bash
urirun run 'ocr://host/backend/query/probe' ocr.registry.json \
  --payload '{}' --execute --allow 'ocr://**'

urirun run 'ocr://host/document/query/text' ocr.registry.json \
  --payload '{"path":"~/Downloads/invoice.pdf","max_chars":20000}' \
  --execute --allow 'ocr://**'

urirun run 'ocr://host/document/query/text_from_uri' ocr.registry.json \
  --payload '{"source_node_url":"http://192.168.188.201:8765","source_uri":"fs://host/file/query/blob","source_payload_json":"{\"path\":\"2026.05/invoice.pdf\"}"}' \
  --execute --allow 'ocr://**'

urirun run 'ocr://host/document/query/batch' ocr.registry.json \
  --payload '{"root":"~/Downloads/2026/5","extensions":"pdf,png,jpg","output_json":"~/Downloads/ocr-report.json"}' \
  --execute --allow 'ocr://**'
```

Set `URI_OCR_SOURCE_PATHS` when the optional repos are not in their default
locations.
