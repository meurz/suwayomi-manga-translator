"""CPU or CUDA inference worker, isolated from the responsive gateway process."""

import asyncio
import hashlib
import io
import json
import logging
import os
import secrets
import threading
import time
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image

from .concurrency import page_concurrency
from .language import empty_evidence, summarize_languages
from .provider import should_translate, translate_regions

app = FastAPI(docs_url=None, redoc_url=None)
_lock = threading.Lock()
_translator_class = None
_admission = threading.BoundedSemaphore(page_concurrency())
Image.MAX_IMAGE_PIXELS = 24_000_000


@app.middleware("http")
async def authenticate(request, call_next):
    if request.url.path != "/health":
        expected = os.getenv("WORKER_TOKEN", "")
        received = request.headers.get("x-worker-token", "")
        if not expected or not secrets.compare_digest(received, expected):
            return JSONResponse({"detail": "Authentication required"}, status_code=401)
    return await call_next(request)


def device():
    value = os.getenv("WORKER_DEVICE", "cpu").lower()
    if value not in {"cpu", "cuda"}:
        raise ValueError("WORKER_DEVICE must be cpu or cuda")
    return value


@contextmanager
def inference_deadline():
    """Recycle a stuck worker; the gateway retains the chapter checkpoint."""

    def expired():
        logging.getLogger(__name__).error("Inference deadline exceeded; recycling worker")
        os._exit(70)

    timer = threading.Timer(float(os.getenv("WORKER_PAGE_TIMEOUT", "300")), expired)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def translator_class():
    """Initialize once under the GPU lock; model weights are cached by upstream dispatchers."""
    global _translator_class
    if _translator_class is not None:
        return _translator_class
    import torch
    from manga_translator import MangaTranslator

    if device() == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is not available")
    torch.set_num_threads(int(os.getenv("TORCH_THREADS", "2")))
    torch.set_num_interop_threads(1)

    class ReaderTranslator(MangaTranslator):
        async def _report_progress(self, state, finished=False):
            if hasattr(self, "page_start"):
                print(
                    f"page={self.page_id} stage={state} "
                    f"elapsed={time.monotonic() - self.page_start:.2f}",
                    flush=True,
                )
            await super()._report_progress(state, finished)

        def _setup_log_file(self):
            # The gateway owns bounded task history. Do not capture global print or page text.
            pass

        async def _run_text_rendering(self, config, ctx):
            import numpy as np

            from .render_guard import ensure_rendered

            before = Image.fromarray(ctx.img_inpainted.copy())
            rendered = await super()._run_text_rendering(config, ctx)
            guarded, count = ensure_rendered(
                before,
                Image.fromarray(rendered),
                [(r.xyxy, r.translation) for r in ctx.text_regions],
                self.font_path,
            )
            if count:
                print(f"typesetting_fallbacks={count}", flush=True)
            return np.asarray(guarded)

        async def _run_text_translation(self, config, ctx):
            regions = ctx.text_regions or []
            if not regions:
                return []
            # Only network I/O runs outside the GPU lock. Each request owns its engine,
            # context, force flag and language evidence; upstream model caches stay shared.
            cloud_start = time.monotonic()
            with cloud_stage():
                decisions = await translate_regions([r.text for r in regions])
                cloud_seconds = time.monotonic() - cloud_start
            print(
                f"page={self.page_id} cloud_seconds={cloud_seconds:.3f}",
                flush=True,
            )
            self.language_evidence = summarize_languages([r.text for r in regions], decisions)
            selected = []
            for region, decision in zip(regions, decisions, strict=True):
                if not should_translate(decision, region.text, self.force):
                    continue
                region.translation = decision.translation.strip()
                region.target_lang = "CHS"
                region._alignment = config.render.alignment
                region._direction = config.render.direction
                selected.append(region)
            # Always recompute the mask from retained regions, preserving Chinese bubbles.
            ctx.mask = None
            return selected

    _translator_class = ReaderTranslator
    return _translator_class


def pipeline():
    return translator_class()(
        {
            "use_gpu": device() == "cuda",
            "kernel_size": 3,
            "verbose": False,
            "ignore_errors": False,
            "model_dir": os.getenv("MODEL_DIR", "/opt/mit/models"),
            "font_path": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        }
    )


@contextmanager
def cloud_stage():
    """Yield exclusive model ownership until the cloud request finishes or fails."""
    _lock.release()
    try:
        yield
    finally:
        _lock.acquire()


@app.get("/health")
def health():
    import torch

    ready = device() == "cpu" or torch.cuda.is_available()
    return JSONResponse(
        {
            "ok": ready,
            "engine": "manga-image-translator",
            "device": device(),
            "page_concurrency": page_concurrency(),
        },
        status_code=200 if ready else 503,
    )


@app.post("/process")
def process(image: UploadFile, force: bool = False):
    data = image.file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "Image exceeds 25 MiB")
    if not _admission.acquire(blocking=False):
        raise HTTPException(503, "Worker page capacity reached; retry later")
    started = time.monotonic()
    try:
        # The deadline includes lock waits and cloud I/O, with bounded admission.
        with inference_deadline():
            return process_page(data, force, started)
    except Exception as exc:
        logging.getLogger(__name__).exception("Worker failed: %s", type(exc).__name__)
        # Never expose upstream request bodies or credentials in HTTP errors.
        raise HTTPException(502, f"Image processing failed ({type(exc).__name__})") from None
    finally:
        _admission.release()


def process_page(data, force, started):
    from manga_translator.config import Config

    with Image.open(io.BytesIO(data)) as source:
        source.load()
        if getattr(source, "n_frames", 1) > 1:
            raise ValueError("Animated images are not supported")
        with _lock:
            engine = pipeline()
            engine.page_id = hashlib.sha256(data).hexdigest()[:12]
            engine.force = force
            engine.language_evidence = empty_evidence()
            engine.page_start = time.monotonic()
            config = Config.model_validate(
                {
                    "translator": {
                        "translator": "none",
                        "target_lang": "CHS",
                        "no_text_lang_skip": True,
                        "enable_post_translation_check": False,
                    },
                    "detector": {"detector": "default", "detection_size": 1024},
                    "ocr": {"ocr": "48px"},
                    "inpainter": {
                        "inpainter": "lama_mpe",
                        "inpainting_precision": "fp32",
                        "inpainting_size": 768,
                    },
                    "render": {"direction": "horizontal", "alignment": "center"},
                }
            )
            ctx = asyncio.run(engine.translate(source, config, skip_context_save=True))
            count = len(ctx.text_regions or [])
            headers = {
                "X-Regions": str(count),
                "X-Language-Evidence": json.dumps(engine.language_evidence),
                "X-Elapsed": str(round(time.monotonic() - started, 3)),
                "X-Outcome": "translated" if count else "skipped",
            }
            if not count:
                return Response(
                    data, media_type=Image.MIME.get(source.format, "image/png"), headers=headers
                )
            out = io.BytesIO()
            ctx.result.save(out, "PNG")
            return Response(out.getvalue(), media_type="image/png", headers=headers)
