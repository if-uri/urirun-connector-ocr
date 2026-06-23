# OCR connector examples

Probe a node:

```bash
urirun run 'ocr://host/backend/query/probe' ocr.registry.json \
  --payload '{}' --execute --allow 'ocr://**'
```

Read a PDF invoice:

```bash
urirun run 'ocr://host/document/query/text' ocr.registry.json \
  --payload '{"path":"~/Downloads/2026/5/2026.05/saas/example.pdf","backend":"auto"}' \
  --execute --allow 'ocr://**'
```

OCR a document fetched from another node over URI:

```bash
urirun run 'ocr://host/document/query/text_from_uri' ocr.registry.json \
  --payload '{"source_node_url":"http://192.168.188.201:8765","source_uri":"fs://host/file/query/blob","source_payload_json":"{\"path\":\"2026.05/invoice.pdf\"}"}' \
  --execute --allow 'ocr://**'
```

OCR a whole invoice folder and write reports:

```bash
urirun run 'ocr://host/document/query/batch' ocr.registry.json \
  --payload '{"root":"~/Downloads/2026/5","extensions":"pdf,png,jpg","output_json":"~/Downloads/2026/5/ocr-report.json","output_csv":"~/Downloads/2026/5/ocr-report.csv"}' \
  --execute --allow 'ocr://**'
```

Analyze a screenshot through imgl:

```bash
urirun run 'ocr://host/image/query/text' ocr.registry.json \
  --payload '{"image":"/tmp/screen.png","backend":"imgl","lang":"eng+pol"}' \
  --execute --allow 'ocr://**'
```
