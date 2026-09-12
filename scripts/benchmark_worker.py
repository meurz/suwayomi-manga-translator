"""Measure uncached inference and network time without printing credentials."""

import argparse
import io
import json
import os
import time
from pathlib import Path

import httpx
from PIL import Image

p = argparse.ArgumentParser()
p.add_argument("page", type=Path)
p.add_argument("--base", default="http://127.0.0.1:18442")
p.add_argument("--runs", type=int, default=1)
p.add_argument("--output", type=Path, required=True)
a = p.parse_args()
a.output.mkdir(parents=True, exist_ok=True)
data = a.page.read_bytes()
original = Image.open(io.BytesIO(data)).convert("RGB")
with httpx.Client(timeout=300, trust_env=False) as client:
    for i in range(a.runs):
        start = time.monotonic()
        r = client.post(
            a.base.rstrip("/") + "/process",
            files={"image": ("page.png", data, "image/png")},
            headers={"X-Worker-Token": os.environ["WORKER_TOKEN"]},
        )
        total = round(time.monotonic() - start, 3)
        r.raise_for_status()
        image = Image.open(io.BytesIO(r.content)).convert("RGB")
        image.load()
        assert image.size == original.size
        outcome = r.headers["x-outcome"]
        identical = image.tobytes() == original.tobytes()
        if outcome == "skipped":
            assert r.content == data
        else:
            assert outcome == "translated" and not identical
        name = a.output / f"{a.page.stem}-{i + 1}"
        name.with_suffix(".png").write_bytes(r.content)
        result = {
            "page": a.page.name,
            "run": i + 1,
            "total_seconds": total,
            "worker_seconds": float(r.headers["x-elapsed"]),
            "outcome": outcome,
            "regions": int(r.headers["x-regions"]),
            "original_pixels": identical,
            "size": list(image.size),
        }
        name.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
