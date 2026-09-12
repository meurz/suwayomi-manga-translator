"""Durable chapter jobs; publish a reader mapping only after every page is verified."""

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path

from PIL import Image

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
PAGE_LIMIT = 25 * 1024 * 1024
ARCHIVE_LIMIT = 1024 * 1024 * 1024
PAGE_COUNT_LIMIT = 1000


class WorkerUnavailable(RuntimeError):
    """A transient worker outage; keep the chapter queued until it recovers."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inspect_image(data):
    if len(data) > PAGE_LIMIT:
        raise ValueError("Page exceeds 25 MiB")
    with Image.open(io.BytesIO(data)) as image:
        if image.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
            raise ValueError("Unsupported page image")
        if getattr(image, "n_frames", 1) != 1:
            raise ValueError("Animated pages are not supported")
        if image.width * image.height > 24_000_000:
            raise ValueError("Page exceeds 24 million pixels")
        image.load()
        return Image.MIME[image.format], image.size


def page_entries(path, expected):
    with zipfile.ZipFile(path) as archive:
        images = [
            entry
            for entry in archive.infolist()
            if not entry.is_dir() and Path(entry.filename).suffix.lower() in IMAGE_SUFFIXES
        ]
        if not images or len(images) > PAGE_COUNT_LIMIT or len(images) != expected:
            raise ValueError("Archive page count does not match the Suwayomi chapter")
        if len({entry.filename for entry in images}) != len(images):
            raise ValueError("Archive has duplicate page names")
        if any(entry.flag_bits & 1 or entry.file_size > PAGE_LIMIT for entry in images):
            raise ValueError("Archive has encrypted or oversized pages")
        if sum(entry.file_size for entry in images) > ARCHIVE_LIMIT:
            raise ValueError("Expanded chapter exceeds 1 GiB")

        # Never extract archive paths. Output files use generated numeric names.
        def order(entry):
            return [
                (0, int(part)) if part.isdigit() else (1, part.casefold())
                for part in re.split(r"(\d+)", entry.filename)
            ]

        return [entry.filename for entry in sorted(images, key=order)]


class ChapterManager:
    def __init__(self, root, source, translate, profile):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.source, self.translate, self.profile = source, translate, profile
        self.db = sqlite3.connect(self.root / "chapters.sqlite")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS chapters (
            id INTEGER PRIMARY KEY, metadata TEXT NOT NULL, profile TEXT NOT NULL,
            status TEXT NOT NULL, total INTEGER DEFAULT 0, completed INTEGER DEFAULT 0,
            error TEXT DEFAULT '', attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0,
            requested REAL DEFAULT 0, archive_sha TEXT DEFAULT '', updated REAL NOT NULL
          );
          CREATE TABLE IF NOT EXISTS pages (
            chapter_id INTEGER NOT NULL, number INTEGER NOT NULL, original_sha TEXT NOT NULL,
            result_sha TEXT NOT NULL, mime TEXT NOT NULL, outcome TEXT NOT NULL,
            regions INTEGER NOT NULL, PRIMARY KEY(chapter_id, number)
          );
          CREATE INDEX IF NOT EXISTS pages_original ON pages(original_sha);
          CREATE TABLE IF NOT EXISTS seen (id INTEGER PRIMARY KEY, present INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.execute(
            "UPDATE chapters SET status='queued' WHERE status IN ('fetching','translating','verifying')"
        )
        self.db.commit()
        self.last_sync = 0
        self.watch_error = ""
        self.active = None
        self.tasks = []

    def option(self, key, default):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_option(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
        self.db.commit()

    def get(self, chapter_id):
        row = self.db.execute("SELECT * FROM chapters WHERE id=?", (chapter_id,)).fetchone()
        return dict(row) if row else None

    def update(self, chapter_id, **values):
        values["updated"] = time.time()
        self.db.execute(
            "UPDATE chapters SET " + ",".join(f"{key}=?" for key in values) + " WHERE id=?",
            (*values.values(), chapter_id),
        )
        self.db.commit()

    def add(self, chapter, status="queued"):
        chapter_id = int(chapter["id"])
        row = self.get(chapter_id)
        if self.active == chapter_id:
            raise ValueError("Wait for the active page to finish before retrying")
        if row and row["status"] not in {"available", "failed", "cancelled"}:
            return row
        if row and row["profile"] != self.profile:
            self.db.execute("DELETE FROM pages WHERE chapter_id=?", (chapter_id,))
        self.db.execute(
            "INSERT INTO chapters(id,metadata,profile,status,total,updated) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata,profile=excluded.profile,"
            "status=excluded.status,error='',attempts=0,next_try=0,requested=0,updated=excluded.updated",
            (
                chapter_id,
                json.dumps(chapter),
                self.profile,
                status,
                chapter["pageCount"],
                time.time(),
            ),
        )
        self.db.commit()
        return self.get(chapter_id)

    def summary(self):
        jobs = []
        for row in self.db.execute("SELECT * FROM chapters ORDER BY updated DESC LIMIT 200"):
            job = dict(row)
            meta = json.loads(job.pop("metadata"))
            job.update(
                name=meta["name"],
                manga=meta["manga"]["title"],
                manga_id=meta["mangaId"],
                source_order=meta["sourceOrder"],
            )
            jobs.append(job)
        return {
            "jobs": jobs,
            "paused": self.option("paused", False),
            "auto_downloads": self.option("auto_downloads", True),
            "initialized": self.option("initialized", False),
            "last_sync": self.last_sync,
            "watch_error": self.watch_error,
        }

    def capacity(self, extra=0):
        used = sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        if used + extra > int(os.getenv("CHAPTER_MAX_BYTES", str(20 * 1024**3))):
            raise ValueError("Chapter storage limit reached; remove an old translated copy")
        if shutil.disk_usage(self.root).free < extra + 128 * 1024 * 1024:
            raise ValueError("Insufficient disk space for chapter processing")

    async def sync_downloads(self):
        chapters = await self.source.downloaded()
        initialized = self.option("initialized", False)
        previous = {r[0]: r[1] for r in self.db.execute("SELECT id,present FROM seen")}
        for chapter in chapters:
            if not previous.get(chapter["id"], 0) and not self.get(chapter["id"]):
                status = (
                    "queued" if initialized and self.option("auto_downloads", True) else "available"
                )
                self.add(chapter, status)
        with self.db:
            self.db.execute("UPDATE seen SET present=0")
            self.db.executemany(
                "INSERT OR REPLACE INTO seen VALUES (?,1)", [(c["id"],) for c in chapters]
            )
        self.set_option("initialized", True)
        self.last_sync = time.time()
        self.watch_error = ""

    async def watch(self):
        while True:
            try:
                await self.sync_downloads()
            except Exception as exc:
                self.watch_error = f"Suwayomi download sync unavailable ({type(exc).__name__})"
            await asyncio.sleep(float(os.getenv("CHAPTER_POLL_SECONDS", "30")))

    async def acquire(self, chapter, folder):
        original = folder / "original.cbz"
        if original.exists():
            return original
        temp = folder / "original.part"
        try:
            if str(chapter["manga"]["sourceId"]) == "0":
                path = self.source.local_archive(chapter)
                if path.stat().st_size > ARCHIVE_LIMIT:
                    raise ValueError("Chapter archive exceeds 1 GiB")
                self.capacity(path.stat().st_size)
                await asyncio.to_thread(shutil.copyfile, path, temp)
            else:
                async with self.source.client.stream(
                    "GET", f"/api/v1/chapter/{chapter['id']}/download?markAsRead=false"
                ) as response:
                    response.raise_for_status()
                    total = 0
                    self.capacity()
                    next_check = 0
                    with temp.open("wb") as file:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            total += len(chunk)
                            if total > ARCHIVE_LIMIT:
                                raise ValueError("Chapter archive exceeds 1 GiB")
                            if total >= next_check:
                                self.capacity(16 * 1024 * 1024)
                                next_check = total + 16 * 1024 * 1024
                            file.write(chunk)
            await asyncio.to_thread(page_entries, temp, chapter["pageCount"])
            temp.replace(original)
            return original
        finally:
            temp.unlink(missing_ok=True)

    def lookup(self, data):
        rows = list(
            self.db.execute(
                "SELECT pages.* FROM pages JOIN chapters ON chapters.id=pages.chapter_id "
                "WHERE original_sha=? AND chapters.status='ready' ORDER BY chapters.updated DESC",
                (digest(data),),
            )
        )
        for row in rows:
            path = self.root / str(row["chapter_id"]) / f"{row['number']:05d}.image"
            if path.exists():
                result = path.read_bytes()
                if digest(result) == row["result_sha"]:
                    return result, row["mime"]
            self.update(
                row["chapter_id"], status="failed", error="Stored page damaged; retry chapter"
            )
        return None

    def existing_page(self, chapter_id, number, source_hash, folder):
        row = self.db.execute(
            "SELECT * FROM pages WHERE chapter_id=? AND number=?", (chapter_id, number)
        ).fetchone()
        path = folder / f"{number:05d}.image"
        return bool(
            row
            and row["original_sha"] == source_hash
            and path.exists()
            and digest(path.read_bytes()) == row["result_sha"]
        )

    def build_archive(self, folder, pages):
        temp = folder / "translated.part"
        archive = folder / "translated.cbz"
        extensions = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/webp": "webp",
            "image/gif": "gif",
        }
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED) as output:
            for page in pages:
                data = (folder / f"{page['number']:05d}.image").read_bytes()
                if digest(data) != page["result_sha"]:
                    raise ValueError("Stored page failed final integrity verification")
                inspect_image(data)
                output.writestr(f"{page['number'] + 1:05d}.{extensions[page['mime']]}", data)
        with zipfile.ZipFile(temp) as output:
            if output.testzip() is not None:
                raise ValueError("Translated archive failed CRC verification")
        with temp.open("rb") as file:
            checksum = hashlib.file_digest(file, "sha256").hexdigest()
        temp.replace(archive)
        return checksum

    async def process(self, chapter_id):
        row = self.get(chapter_id)
        if row["profile"] != self.profile:
            self.db.execute("DELETE FROM pages WHERE chapter_id=?", (chapter_id,))
            self.update(chapter_id, profile=self.profile, completed=0)
        folder = self.root / str(chapter_id)
        folder.mkdir(exist_ok=True)
        chapter = await self.source.chapter(chapter_id)
        if self.get(chapter_id)["status"] == "cancelled":
            return
        local = str(chapter["manga"]["sourceId"]) == "0"
        if not local and not chapter["isDownloaded"] and not (folder / "original.cbz").exists():
            if not row["requested"]:
                await self.source.enqueue_download(chapter_id)
            if self.get(chapter_id)["status"] == "cancelled":
                return
            self.update(
                chapter_id,
                status="downloading",
                requested=row["requested"] or time.time(),
                next_try=time.time() + 15,
            )
            if row["requested"] and row["requested"] < time.time() - 1800:
                raise ValueError(
                    "Original download has not completed; check Suwayomi's download queue"
                )
            return
        if not 0 < chapter["pageCount"] <= PAGE_COUNT_LIMIT:
            raise ValueError("Chapter has an invalid or unsupported page count")
        self.update(
            chapter_id,
            status="fetching",
            error="",
            metadata=json.dumps(chapter),
            total=chapter["pageCount"],
        )
        original = await self.acquire(chapter, folder)
        names = await asyncio.to_thread(page_entries, original, chapter["pageCount"])
        if self.get(chapter_id)["status"] == "cancelled":
            return
        self.update(chapter_id, status="translating", completed=0)
        with zipfile.ZipFile(original) as archive:
            for number, name in enumerate(names):
                if self.get(chapter_id)["status"] == "cancelled":
                    return
                if self.option("paused", False):
                    self.update(chapter_id, status="queued")
                    return
                data = await asyncio.to_thread(archive.read, name)
                source_hash = digest(data)
                if not self.existing_page(chapter_id, number, source_hash, folder):
                    _, original_size = await asyncio.to_thread(inspect_image, data)
                    result, mime, outcome, regions = await self.translate(data)
                    actual_mime, size = await asyncio.to_thread(inspect_image, result)
                    if (
                        size != original_size
                        or mime != actual_mime
                        or outcome not in {"translated", "skipped"}
                    ):
                        raise ValueError("Worker returned an invalid translated page")
                    if outcome == "skipped":
                        result = data
                        mime, _ = inspect_image(data)
                    self.capacity(len(result))
                    target = folder / f"{number:05d}.image"
                    target.with_suffix(".part").write_bytes(result)
                    target.with_suffix(".part").replace(target)
                    self.db.execute(
                        "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?)",
                        (chapter_id, number, source_hash, digest(result), mime, outcome, regions),
                    )
                    self.db.commit()
                self.update(chapter_id, completed=number + 1)
        # No lookup can see any of these pages until this chapter becomes ready.
        if self.get(chapter_id)["status"] == "cancelled":
            return
        self.update(chapter_id, status="verifying")
        pages = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM pages WHERE chapter_id=? ORDER BY number", (chapter_id,)
            )
        ]
        if len(pages) != len(names):
            raise ValueError("Chapter page manifest is incomplete")
        self.capacity(sum((folder / f"{p['number']:05d}.image").stat().st_size for p in pages))
        checksum = await asyncio.to_thread(self.build_archive, folder, pages)
        if self.get(chapter_id)["status"] == "cancelled":
            return
        self.update(chapter_id, status="ready", archive_sha=checksum, error="", attempts=0)

    async def run(self):
        while True:
            if not self.option("paused", False):
                row = self.db.execute(
                    "SELECT id FROM chapters WHERE status IN ('queued','downloading','waiting') "
                    "AND next_try<=? ORDER BY updated LIMIT 1",
                    (time.time(),),
                ).fetchone()
                if row:
                    self.active = row[0]
                    try:
                        await self.process(self.active)
                    except asyncio.CancelledError:
                        if self.get(self.active)["status"] != "cancelled":
                            self.update(self.active, status="queued")
                        raise
                    except WorkerUnavailable:
                        if self.get(self.active)["status"] != "cancelled":
                            self.update(
                                self.active,
                                status="waiting",
                                error="Worker unavailable; retrying in 60 seconds",
                                next_try=time.time() + 60,
                            )
                    except Exception as exc:
                        current = self.get(self.active)
                        if current["status"] != "cancelled":
                            attempts = current["attempts"] + 1
                            message = (
                                str(exc)
                                if isinstance(exc, (ValueError, RuntimeError))
                                else type(exc).__name__
                            )
                            self.update(
                                self.active,
                                status="waiting" if attempts < 3 else "failed",
                                attempts=attempts,
                                error=message[:180],
                                next_try=time.time() + 60,
                            )
                    finally:
                        self.active = None
                    continue
            await asyncio.sleep(1)

    def start(self):
        self.tasks = [asyncio.create_task(self.watch()), asyncio.create_task(self.run())]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.source.client.aclose()
        self.db.close()

    def remove(self, chapter_id):
        if self.active == chapter_id:
            raise ValueError("Cancel the active chapter and wait for its current page first")
        with self.db:
            self.db.execute("DELETE FROM pages WHERE chapter_id=?", (chapter_id,))
            self.db.execute("DELETE FROM chapters WHERE id=?", (chapter_id,))
        folder = self.root / str(chapter_id)
        if folder.exists():
            shutil.rmtree(folder)
