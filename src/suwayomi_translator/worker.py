"""CPU inference worker, isolated from the responsive gateway process."""

import asyncio
import io
import logging
import os
import threading
import time

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import Response
from PIL import Image

from .provider import should_translate, translate_regions

app = FastAPI(docs_url=None, redoc_url=None)
_lock = threading.Lock()
_pipeline = None
Image.MAX_IMAGE_PIXELS = 24_000_000


def pipeline():
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    import torch
    from manga_translator import MangaTranslator

    torch.set_num_threads(int(os.getenv("TORCH_THREADS", "2")))
    torch.set_num_interop_threads(1)

    class ReaderTranslator(MangaTranslator):
        async def _report_progress(self, state, finished=False):
            if hasattr(self, "page_start"):
                print(f"stage={state} elapsed={time.monotonic() - self.page_start:.2f}", flush=True)
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
            decisions = await translate_regions([r.text for r in regions])
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

    _pipeline = ReaderTranslator(
        {
            "use_gpu": False,
            "kernel_size": 3,
            "verbose": False,
            "ignore_errors": False,
            "model_dir": os.getenv("MODEL_DIR", "/opt/mit/models"),
            "font_path": "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        }
    )
    return _pipeline


@app.get("/health")
def health():
    return {"ok": True, "engine": "manga-image-translator", "device": "cpu"}


@app.post("/process")
def process(image: UploadFile, force: bool = False):
    from manga_translator.config import Config

    data = image.file.read(25 * 1024 * 1024 + 1)
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(413, "Image exceeds 25 MiB")
    started = time.monotonic()
    try:
        source = Image.open(io.BytesIO(data))
        source.load()
        if getattr(source, "n_frames", 1) > 1:
            raise ValueError("Animated images are not supported")
        with _lock:
            engine = pipeline()
            engine.force = force
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
    except Exception as exc:
        logging.getLogger(__name__).exception("Worker failed: %s", type(exc).__name__)
        # Never expose upstream request bodies or credentials in HTTP errors.
        raise HTTPException(502, f"Image processing failed ({type(exc).__name__})") from None
