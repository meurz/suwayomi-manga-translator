"""Compare uncached serial/pipelined page throughput; keep input/output manga private."""

import argparse
import asyncio
import hashlib
import io
import json
import os
import time
import zipfile
from pathlib import Path

import httpx
from PIL import Image

from suwayomi_translator.chapters import page_entries


async def benchmark(args):
    names = page_entries(args.archive, args.pages)
    with zipfile.ZipFile(args.archive) as archive:
        pages = [archive.read(name) for name in names]
    headers = {"X-Worker-Token": os.environ["WORKER_TOKEN"]}
    async with httpx.AsyncClient(timeout=300, trust_env=False, headers=headers) as client:
        for concurrency in args.concurrency:
            slots = asyncio.Semaphore(concurrency)
            started = time.monotonic()

            async def process(number, data):
                async with slots:
                    begin = time.monotonic()
                    response = await client.post(
                        args.base.rstrip("/") + "/process", files={"image": ("page", data)}
                    )
                    response.raise_for_status()
                    with Image.open(io.BytesIO(response.content)) as image:
                        image.load()
                        with Image.open(io.BytesIO(data)) as original:
                            assert image.size == original.size
                    outcome = response.headers["x-outcome"]
                    assert outcome in {"translated", "skipped"}
                    if outcome == "skipped":
                        assert response.content == data
                    args.output.mkdir(parents=True, exist_ok=True)
                    (args.output / f"c{concurrency}-{number:04}.image").write_bytes(
                        response.content
                    )
                    result = {
                        "number": number,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "seconds": round(time.monotonic() - begin, 3),
                        "worker_seconds": float(response.headers["x-elapsed"]),
                        "outcome": outcome,
                        "regions": int(response.headers["x-regions"]),
                    }
                    print(json.dumps({"concurrency": concurrency, **result}), flush=True)
                    return result

            results = await asyncio.gather(*(process(i, data) for i, data in enumerate(pages)))
            report = {
                "concurrency": concurrency,
                "pages": len(pages),
                "seconds": round(time.monotonic() - started, 3),
                "results": results,
            }
            (args.output / f"c{concurrency}.json").write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps({k: v for k, v in report.items() if k != "results"}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--pages", type=int, required=True, help="Expected archive page count")
    parser.add_argument("--base", default="http://127.0.0.1:18442")
    parser.add_argument("--concurrency", type=int, choices=[1, 2], nargs="+", default=[1, 2])
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(benchmark(parser.parse_args()))


if __name__ == "__main__":
    main()
