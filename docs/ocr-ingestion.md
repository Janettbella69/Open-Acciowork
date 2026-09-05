# PDF / image Markdown ingestion

This implements the upload-input rule in `CLAUDE.md` and PRD §3.5 B / §3.9.
An upload keeps the original Artifact and registers `<original path>.md` as a
second Artifact linked to the original ID. The companion identifies the actual
extractor, warnings, and page boundaries. Text is sanitized and enclosed in the
shared untrusted-upload envelope. It is not a new Agent/model orchestration loop.

## Implemented contract

- `ingest.IngestConfig.from_settings(settings)` is passed to `FileLedger` by the
  control plane. The upload route offloads synchronous ingestion from the event loop.
- A disposable Python isolated-mode worker processes an input copy. It receives
  only OCR settings, local model paths, and the optional Tesseract executable;
  Provider/connector credentials, proxy environment, Python path overrides, and
  the parent environment are not inherited. Raw extractor logs are discarded.
- PaddleOCR-VL is explicitly opt-in. Both nonempty local model directories are
  required before importing/constructing `PaddleOCRVL`. It uses the native v1.6
  layout-plus-VL pipeline and the documented `predict_iter` / per-page
  `markdown['markdown_texts']` interface. Legacy `PaddleOCR.ocr()` is never used.
- Each result must correspond to the expected page count/order. Headers and
  footnotes are retained by disabling the library's default ignored-label list.
  Unsupported/missing page results fall back honestly. Image references are
  replaced by an explicit untranscribed-region marker, never left pointing to
  nonexistent image files. No precise coordinate provenance is claimed.
- PDFs fall back to pypdf's existing text layer only. Scans without a text layer
  are failures, not successful extracts made out of page headings. Mixed PDFs
  preserve page-numbered missing-text markers. Images fall back to Tesseract;
  every GIF/TIFF frame is processed, and a nonzero Tesseract exit rejects output.
  There is no silent language fallback or switch of the Agent's Provider.
- Signatures, declared media types, and filename suffixes route supported
  PDF/PNG/JPEG/WebP/TIFF/GIF uploads (including extensionless files). Decoder
  validation still rejects malformed binaries; a declared type is not proof.
- The SDK's existing `PreToolUse` hook denies `Read` of uploaded PDF/image
  originals, including symlink aliases, before auto-approval. It gives the exact
  safe companion path, or instructs the Agent to request a new upload/text copy
  when the companion is missing/unsafe. The stable security prompt tells every
  main Agent to honor missing-page/failure/truncation notices.
- Original downloads, normal text reads, generated-media/screenshot review, and
  sandboxed Bash binary processing remain available. This is a native model-input
  guard, **not** an OS-level ban on all binary bytes or a substitute for Bash's
  sandbox; arbitrary copying/encoding by permitted shell code is not prevented
  by the upload-specific Read hook.

The API and model are based on the [official PaddleOCR-VL guide](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL.html),
the [3.7 wrapper source](https://github.com/PaddlePaddle/PaddleOCR/blob/release/3.7/paddleocr/_pipelines/paddleocr_vl.py),
and the [1.6 pipeline configuration](https://github.com/PaddlePaddle/PaddleX/blob/release/3.7/paddlex/configs/pipelines/PaddleOCR-VL-1.6.yaml).
PaddleOCR's default constructor downloads models when paths are omitted; OPHIRA
does not invoke that default behavior.

## Operator setup

The base install includes pypdf and Pillow. Tesseract is an optional local binary;
install its requested language data before relying on it. A missing language pack
fails visibly instead of returning empty success.

For VL, first choose a supported host and install the appropriate PaddlePaddle
engine following the official hardware-specific instructions. CPU/GPU engine
wheels differ; OPHIRA intentionally does not install a GPU runtime automatically.
Then install the separately pinned OCR extra into the same venv that runs the API:

```bash
.venv/bin/python -m pip install -e './backend[ocr]'
```

Provision complete, compatible `PP-DocLayoutV3` and `PaddleOCR-VL-1.6-0.9B`
model directories through a reviewed operator download, record their hashes and
license, and make them read-only to the service identity. Model directories are
trusted executable dependency material; do not accept model paths from upload
requests or point them at uploaded packages. Configure the server environment or
`backend/.env` (illustrative paths, not auto-created directories):

```dotenv
OPHIRA_OCR_PADDLE_ENABLED=true
OPHIRA_OCR_LAYOUT_MODEL_DIR=/srv/ophira-models/PP-DocLayoutV3
OPHIRA_OCR_VL_MODEL_DIR=/srv/ophira-models/PaddleOCR-VL-1.6-0.9B
OPHIRA_OCR_DEVICE=cpu
OPHIRA_OCR_TIMEOUT_SECONDS=120
OPHIRA_OCR_MAX_BYTES=32000000
OPHIRA_OCR_MAX_PAGES=20
OPHIRA_OCR_MAX_PIXELS=40000000
OPHIRA_OCR_MEMORY_MB=8192
OPHIRA_OCR_TESSERACT_LANGUAGES=chi_sim+eng
```

Restart the API after changing configuration. Supported device values are `cpu`
and `gpu:0`; GPU mode still requires a separately provisioned compatible engine.
OCR does not accept a remote model endpoint or an API key, and never intentionally
transmits an upload to a model provider. HF offline flags and a Python socket audit
guard disable normal Python model downloads/network requests. Scratch caches are
discarded after each upload; models initialize for each upload, so size the timeout
using measured cold-start behavior, not warm-model benchmark timings.

Default limits are 60 seconds wall time per document, 32 MB input, 20 pages/frames,
40 million aggregate image pixels, and a 62,000-character extracted body. The
shared envelope imposes an additional 64,000-character bound. The worker's
per-region VL generation ceiling is 4,096 tokens. Exceeding byte/page/pixel limits
rejects extraction; text limits explicitly mark truncation. An upload timeout kills
and reaps the worker process group, including a running Tesseract subprocess.
It does not retain a partially completed OCR body from a killed worker.

PDF page-count/format/encryption checks precede model loading. CPU, core-dump,
and regular-file size limits apply on the Unix worker. Linux additionally enforces
the configured address-space limit; macOS does not enforce that memory limit here.
Image pixel caps do not constrain Paddle's internal PDF rasterizer allocations;
the process/time limits remain their bounded-failure layer.

**Deployment security limitation:** Python isolated mode, restricted environment,
resource limits, and socket auditing are defence in depth, not a hostile-decoder
security boundary. Native decoders share the host UID and can bypass Python audit
hooks. Production still needs an unprivileged worker/container with only the input
and read-only models mounted, an OS-enforced no-egress policy, and CPU/memory/PID
quotas. No claim of multi-tenant binary-decoder isolation is made by this change.

## Verification and remaining acceptance

Offline checks (no model download, endpoint, secret vault, or actual project data):

```bash
PYTHONPATH=backend/src .venv/bin/python -m pytest \
  backend/tests/test_ingest.py backend/tests/test_ingest_sdk.py \
  backend/tests/test_sdk_adapter.py backend/tests/test_untrusted.py -q
.venv/bin/ruff check backend/src backend/tests
```

These cover the official adapter contract using a stub pipeline, multi-page/frame
handling, invalid/missing model configuration, legacy-only installations, empty and
partial extraction, failure/truncation notices, resource validation, process
environment/timeout cleanup, original-Artifact linkage, SDK Read guard/path safety,
and preservation of normal file/command capabilities. A real child process parses
malformed input to verify the failure transport. They **do not prove OCR accuracy**.

Real acceptance requires a provisioned engine and both model packages, plus an
authorized fixture pack: searchable and scanned multipage Chinese/English PDFs,
mixed text/scans, table-heavy quotes with price/footnote conditions, and image and
multi-frame inputs. Through the real upload API, verify companion Artifacts,
page completeness, original-byte integrity, and visible failure states; then run
the main SDK Agent against those companions and inspect the trace for Markdown
Read rather than native raw-upload Read. Compare extracted commercial quantities
and caveats to human-checked originals and visually inspect those pages; text
extraction alone is not visual/layout verification. Also measure cold-start latency,
memory/concurrency behavior and confirm the no-egress deployment boundary.

The current workspace has no PaddleOCR/PaddleX/PaddlePaddle installation or local
VL model provisioning. No real VL inference, paid service, model download,
production decoder container, or PDF visual QA was performed for this change.
TextIn coordinate-level provenance and model-quality acceptance remain deferred;
this implementation must not be presented as their completion.
