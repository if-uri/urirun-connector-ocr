# urirun-connector-ocr

OCR routes for urirun nodes.

The connector uses lightweight local tools first and optional repository
backends when they are available on the node:

- **PaddleOCR** (`paddle`) for full-frame image OCR — PP-OCRv5/v6 detection +
  recognition with document orientation + UVDoc dewarping. **Preferred backend in
  `auto` mode**: it reads Polish receipts/invoices on the whole frame far more
  reliably than tesseract and never loses the header/footer to an aggressive crop
- `pdftotext` for text PDFs
- `tesseract` CLI for image OCR
- `semcod/imgl` for screenshot/UI OCR with bounding boxes
- `wronai/img2nl` for image analysis and optional RapidOCR text
- `wronai/ocr` for heavier PDF OCR through PyMuPDF/Ollama
- `oqlos/vql` as the layout schema target used by the imgl pipeline
- `urirun-connector-smart-crop` when `smart_crop=true`, so image OCR runs on a
  detected receipt/document crop instead of the full camera frame

### PaddleOCR backend (`backend="paddle"`)

`image`/`document` text routes accept `backend="paddle"` (also the first backend
tried in `backend="auto"`). It runs in-process and is tuned by env:

| env | default | meaning |
| --- | --- | --- |
| `URI_OCR_DISABLE_PADDLE` | `0` | set `1` to skip paddle entirely |
| `URI_OCR_PADDLE_UNWARP` | `1` | UVDoc dewarping (slowest stage; off ≈ ~25% faster) |
| `URI_OCR_PADDLE_ORIENT` | `1` | document-orientation classification |
| `URI_OCR_PADDLE_LANG` | _(unset)_ | force a recognizer language; default reads Latin/Polish |
| `URI_OCR_PADDLE_DET_MODEL` / `URI_OCR_PADDLE_REC_MODEL` | _(unset)_ | override detection/recognition model (e.g. `*_mobile_*` for speed) |

> Requires `paddleocr` + `paddle` in the environment. `enable_mkldnn=False` is forced
> (the oneDNN/PIR path crashes on this paddle build). ~25–33s/frame on CPU; the
> recognition over many lines dominates, so mobile models help only marginally.

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

`text_from_uri` authenticates the cross-node `/run` call with `URIRUN_RUN_TOKEN`,
addressed **by reference**: the value may be the literal token or a secrets-layer
reference (`secret://keyring/urirun#run-token`, `getv://URIRUN_RUN_TOKEN`), resolved
deny-by-default (widen the allow-list with `URIRUN_RUN_TOKEN_ALLOW`).
