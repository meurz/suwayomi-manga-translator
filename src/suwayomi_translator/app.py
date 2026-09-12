"""Bounded translation queue and durable, content-addressed image cache."""

import asyncio
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from PIL import Image, UnidentifiedImageError

from . import pngstream

Image.MAX_IMAGE_PIXELS = 24_000_000
MAX_BYTES = 25 * 1024 * 1024
VERSION = "0.1.0"


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "jobs.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, status TEXT, mime TEXT, created REAL, updated REAL,
            elapsed REAL DEFAULT 0, regions INTEGER DEFAULT 0, error TEXT DEFAULT '',
            hits INTEGER DEFAULT 0, force INTEGER DEFAULT 0)""")
        self.db.execute(
            "UPDATE jobs SET status='failed',error='Service restarted; retry this page' "
            "WHERE status IN ('queued','processing')"
        )
        self.db.commit()

    def get(self, key):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        return dict(row) if row else None

    def put(self, key, data, mime, force):
        now = time.time()
        self.atomic(self.root / f"{key}.original", data)
        self.db.execute(
            "INSERT OR REPLACE INTO jobs "
            "(id,status,mime,created,updated,force) VALUES (?,?,?,?,?,?)",
            (key, "queued", mime, now, now, int(force)),
        )
        self.db.commit()

    @staticmethod
    def atomic(path, data):
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(data)
        temp.replace(path)

    def update(self, key, **values):
        values["updated"] = time.time()
        self.db.execute(
            "UPDATE jobs SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
            (*values.values(), key),
        )
        self.db.commit()

    def recent(self):
        return [
            dict(r) for r in self.db.execute("SELECT * FROM jobs ORDER BY updated DESC LIMIT 100")
        ]

    def prune(self, max_bytes):
        rows = list(self.db.execute("SELECT id,status,updated FROM jobs ORDER BY updated DESC"))
        used = 0
        for row in rows:
            paths = [self.root / f"{row['id']}.{kind}" for kind in ("original", "result")]
            used += sum(p.stat().st_size for p in paths if p.exists())
            if row["status"] in {"queued", "processing"}:
                continue
            if used > max_bytes or row["updated"] < time.time() - 30 * 86400:
                for path in paths:
                    path.unlink(missing_ok=True)
                self.db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
        self.db.commit()


def image_info(data):
    try:
        with Image.open(io.BytesIO(data)) as im:
            if im.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                raise ValueError("Unsupported image format")
            if getattr(im, "n_frames", 1) > 1:
                raise ValueError("Animated images are not supported")
            im.verify()
            return Image.MIME[im.format]
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise HTTPException(400, "Invalid, oversized or unsupported image") from exc


def cache_key(data, profile, force=False):
    return hashlib.sha256(profile.encode() + b"\0" + str(force).encode() + b"\0" + data).hexdigest()


def create_app(root=None, worker_url=None):
    root = Path(root or os.getenv("DATA_DIR", "data"))
    worker_url = worker_url or os.getenv("WORKER_URL", "http://worker:8001")
    tasks = {}
    slots = asyncio.Semaphore(1)
    profile = os.getenv("CACHE_PROFILE", "mit-95227a2-chs-v1")
    state = {"enabled": True}
    state_file = root / "settings.json"

    @asynccontextmanager
    async def lifespan(app):
        app.state.store = Store(root)
        if state_file.exists():
            state.update(json.loads(state_file.read_text()))
        app.state.client = httpx.AsyncClient(timeout=300, trust_env=False)
        yield
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await app.state.client.aclose()
        app.state.store.db.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

    def store():
        return app.state.store

    def authorize(request, admin=False):
        if admin and request.method != "GET" and request.headers.get("origin"):
            from urllib.parse import urlsplit

            if urlsplit(request.headers["origin"]).netloc != request.headers.get("host"):
                raise HTTPException(403, "Cross-origin write rejected")
        expected = os.getenv("ADMIN_TOKEN" if admin else "PROCESSOR_TOKEN", "")
        received = request.headers.get("x-admin-token" if admin else "x-processor-token", "")
        if not expected or not secrets.compare_digest(received, expected):
            raise HTTPException(401, "Authentication required")

    async def process(key, data, force):
        started = time.monotonic()
        try:
            async with slots:
                store().update(key, status="processing")
                r = await app.state.client.post(
                    worker_url + "/process",
                    params={"force": str(force).lower()},
                    files={"image": ("page", data, store().get(key)["mime"])},
                )
                if r.status_code != 200:
                    raise RuntimeError(f"Worker HTTP {r.status_code}")
                mime = image_info(r.content)
                outcome = r.headers.get("x-outcome")
                if outcome not in {"translated", "skipped"}:
                    raise RuntimeError("Invalid worker outcome")
                if outcome == "skipped":
                    mime = store().get(key)["mime"]
                result = data if outcome == "skipped" else r.content
                store().atomic(root / f"{key}.result", result)
                store().update(
                    key,
                    status=outcome,
                    mime=mime,
                    elapsed=round(time.monotonic() - started, 3),
                    regions=int(r.headers.get("x-regions", 0)),
                    error="",
                )
        except asyncio.CancelledError:
            store().update(key, status="failed", error="Service stopped; retry this page")
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            store().update(
                key,
                status="failed",
                error=message[:150],
                elapsed=round(time.monotonic() - started, 3),
            )
        finally:
            tasks.pop(key, None)
            store().prune(int(os.getenv("CACHE_MAX_BYTES", str(2 * 1024**3))))

    async def submit(data, force=False):
        mime = image_info(data)
        key = cache_key(data, profile, force)
        previous = store().get(key)
        if previous and previous["status"] in {"translated", "skipped"}:
            if (root / f"{key}.result").exists():
                store().update(key, hits=previous["hits"] + 1)
                return key, True
        if key not in tasks:
            # Cool down failures instead of charging/retrying on every reader refresh.
            if (
                previous
                and previous["status"] == "failed"
                and previous["updated"] > time.time() - 60
            ):
                return key, False
            if len(tasks) >= int(os.getenv("MAX_PENDING", "8")):
                raise HTTPException(503, "Translation queue is full")
            store().put(key, data, mime, force)
            tasks[key] = asyncio.create_task(process(key, data, force))
        return key, False

    async def read_upload(image):
        data = await image.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "Image exceeds 25 MiB")
        return data

    @app.get("/health")
    async def health():
        return {"ok": True, "version": VERSION, "pending": len(tasks), "enabled": state["enabled"]}

    @app.post("/convert")
    async def convert(request: Request, image: UploadFile):
        authorize(request)
        data = await read_upload(image)
        mime = image_info(data)
        if not state["enabled"]:
            return Response(data, media_type=mime, headers={"X-Outcome": "disabled"})
        try:
            key, hit = await submit(data)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            return Response(
                data, media_type=mime, headers={"X-Outcome": "busy", "Cache-Control": "no-store"}
            )
        if key in tasks:
            pending = tasks[key]
            try:
                await asyncio.wait_for(
                    asyncio.shield(pending), float(os.getenv("STREAM_AFTER", "10"))
                )
            except TimeoutError:

                async def stream():
                    first, size = pngstream.prefix(data)
                    yield first
                    deadline = time.monotonic() + float(os.getenv("READER_WAIT", "80"))
                    while not pending.done() and time.monotonic() < deadline:
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(pending),
                                min(5, max(0.001, deadline - time.monotonic())),
                            )
                        except TimeoutError:
                            yield pngstream.heartbeat()
                    row = store().get(key)
                    result = root / f"{key}.result"
                    output = (
                        result.read_bytes()
                        if row and row["status"] in {"translated", "skipped"} and result.exists()
                        else data
                    )
                    try:
                        tail = await asyncio.to_thread(pngstream.finish, output, size)
                    except (ValueError, OSError):
                        tail = await asyncio.to_thread(pngstream.finish, data, size)
                    yield tail

                return StreamingResponse(
                    stream(),
                    media_type="image/png",
                    headers={"Cache-Control": "no-store", "X-Outcome": "streamed"},
                )
        row = store().get(key)
        if row and row["status"] in {"translated", "skipped"}:
            return FileResponse(
                root / f"{key}.result",
                media_type=row["mime"],
                headers={"X-Outcome": row["status"], "X-Cache": "HIT" if hit else "MISS"},
            )
        return Response(
            data, media_type=mime, headers={"X-Outcome": "fallback", "Cache-Control": "no-store"}
        )

    @app.get("/admin/", response_class=HTMLResponse)
    async def index(request: Request):
        authorize(request, True)
        return (Path(__file__).parent / "static/index.html").read_text()

    @app.get("/admin/api/status")
    async def status(request: Request):
        authorize(request, True)
        return {
            "enabled": state["enabled"],
            "version": VERSION,
            "profile": profile,
            "pending": len(tasks),
            "jobs": store().recent(),
            "model": os.getenv("PUBLIC_MODEL", "Configured in worker environment"),
        }

    @app.post("/admin/api/enabled")
    async def enabled(request: Request):
        authorize(request, True)
        body = await request.json()
        if type(body.get("enabled")) is not bool:
            raise HTTPException(400, "enabled must be a boolean")
        state["enabled"] = body["enabled"]
        Store.atomic(state_file, json.dumps(state).encode())
        return state

    @app.post("/admin/api/upload")
    async def upload(request: Request, image: UploadFile, force: bool = False):
        authorize(request, True)
        key, hit = await submit(await read_upload(image), force)
        return {"id": key, "cached": hit}

    def valid_key(key):
        if not re.fullmatch(r"[0-9a-f]{64}", key) or not store().get(key):
            raise HTTPException(404, "Unknown page")

    @app.post("/admin/api/jobs/{key}/retry")
    async def retry(request: Request, key: str):
        authorize(request, True)
        valid_key(key)
        if key in tasks:
            return {"id": key, "status": "already running"}
        if len(tasks) >= int(os.getenv("MAX_PENDING", "8")):
            raise HTTPException(503, "Translation queue is full")
        row = store().get(key)
        data = (root / f"{key}.original").read_bytes()
        store().update(key, status="queued", error="")
        (root / f"{key}.result").unlink(missing_ok=True)
        tasks[key] = asyncio.create_task(process(key, data, bool(row["force"])))
        return {"id": key, "status": "queued"}

    @app.get("/admin/api/jobs/{key}/{kind}")
    async def view(request: Request, key: str, kind: str):
        authorize(request, True)
        valid_key(key)
        if kind not in {"original", "result"} or not (root / f"{key}.{kind}").exists():
            raise HTTPException(404, "Image is not ready")
        data = (root / f"{key}.{kind}").read_bytes()
        return Response(data, media_type=image_info(data), headers={"Cache-Control": "no-store"})

    return app


app = create_app()
