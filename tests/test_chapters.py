import asyncio
import io
import zipfile

import httpx
import pytest
from PIL import Image

from suwayomi_translator.app import create_app
from suwayomi_translator.chapters import ChapterManager, WorkerUnavailable, page_entries
from suwayomi_translator.suwayomi import Suwayomi


def png(color):
    out = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(out, "PNG")
    return out.getvalue()


def archive_bytes(pages):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for i, page in enumerate(pages):
            archive.writestr(f"{i + 1}.png", page)
    return out.getvalue()


class Source:
    def __init__(self, path, pages):
        self.path = path
        self.path.write_bytes(archive_bytes(pages))
        self.items = []
        self.enqueued = []
        self.meta = dict(
            id=1,
            mangaId=2,
            name="Chapter 1",
            sourceOrder=1,
            pageCount=len(pages),
            isDownloaded=False,
            url="chapter.cbz",
            manga=dict(title="Test", sourceId="0"),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=self.path.read_bytes())
            ),
            base_url="http://source",
        )

    async def chapter(self, chapter_id):
        return {**self.meta, "id": chapter_id}

    async def downloaded(self):
        return self.items

    async def enqueue_download(self, chapter_id):
        self.enqueued.append(chapter_id)

    def local_archive(self, chapter):
        return self.path


@pytest.fixture
async def manager(tmp_path):
    source = Source(tmp_path / "source.cbz", [png("white"), png("black")])

    async def translate(data):
        return png("gray"), "image/png", "translated", 2

    manager = ChapterManager(tmp_path / "prepared", source, translate, "test")
    yield manager
    await manager.close()


async def test_no_partial_publication_and_resume_after_failure(manager):
    original = manager.source.path.read_bytes()
    calls = []

    async def translate(data):
        calls.append(data)
        assert manager.lookup(png("white")) is None
        if data == png("black"):
            raise RuntimeError("Interrupted")
        return png("gray"), "image/png", "translated", 2

    manager.translate = translate
    manager.add(manager.source.meta)
    with pytest.raises(RuntimeError):
        await manager.process(1)
    assert manager.get(1)["completed"] == 1
    assert manager.lookup(png("white")) is None
    assert not (manager.root / "1/translated.cbz").exists()

    async def recover(data):
        calls.append(data)
        return data, "image/png", "skipped", 0

    manager.translate = recover
    await manager.process(1)
    assert calls == [png("white"), png("black"), png("black")]
    assert manager.get(1)["status"] == "ready"
    assert manager.lookup(png("white"))[0] == png("gray")
    assert manager.lookup(png("black"))[0] == png("black")
    assert manager.source.path.read_bytes() == original
    assert (manager.root / "1/original.cbz").read_bytes() == original
    with zipfile.ZipFile(manager.root / "1/translated.cbz") as archive:
        assert archive.read("00002.png") == png("black")
        assert archive.testzip() is None


async def test_baseline_manual_and_new_download_auto_queue(manager):
    manager.source.items = [manager.source.meta]
    await manager.sync_downloads()
    assert manager.get(1)["status"] == "available"
    manager.source.items.append({**manager.source.meta, "id": 3})
    await manager.sync_downloads()
    assert manager.get(3)["status"] == "queued"
    manager.set_option("auto_downloads", False)
    manager.source.items.append({**manager.source.meta, "id": 4})
    await manager.sync_downloads()
    assert manager.get(4)["status"] == "available"
    manager.remove(3)
    await manager.sync_downloads()
    assert manager.get(3) is None


async def test_native_download_wait_then_raw_archive(manager):
    manager.source.meta["manga"]["sourceId"] = "123"
    manager.add(manager.source.meta)
    await manager.process(1)
    assert manager.source.enqueued == [1]
    assert manager.get(1)["status"] == "downloading"
    await manager.process(1)
    assert manager.source.enqueued == [1]
    manager.source.meta["isDownloaded"] = True
    await manager.process(1)
    assert manager.get(1)["status"] == "ready"


async def test_download_deadline_is_not_reset_by_polling(manager):
    manager.source.meta["manga"]["sourceId"] = "123"
    manager.add(manager.source.meta)
    manager.update(1, requested=1)
    with pytest.raises(ValueError, match="download queue"):
        await manager.process(1)
    assert manager.get(1)["requested"] == 1


async def test_cancel_during_inference_prevents_publication(manager):
    async def translate(data):
        manager.update(1, status="cancelled")
        return data, "image/png", "skipped", 0

    manager.translate = translate
    manager.add(manager.source.meta)
    await manager.process(1)
    assert manager.get(1)["status"] == "cancelled"
    assert manager.lookup(png("white")) is None
    assert manager.get(1)["completed"] == 1


async def test_corrupt_prepared_page_unpublishes_chapter_and_retry_repairs(manager):
    manager.add(manager.source.meta)
    await manager.process(1)
    (manager.root / "1/00000.image").write_bytes(b"broken")
    assert manager.lookup(png("white")) is None
    assert manager.get(1)["status"] == "failed"
    manager.add(manager.source.meta)
    await manager.process(1)
    assert manager.lookup(png("white"))[0] == png("gray")


async def test_restart_recovers_progress_and_durable_settings(manager):
    manager.add(manager.source.meta)
    manager.update(1, status="translating", completed=1)
    manager.set_option("paused", True)
    manager.db.close()
    replacement = ChapterManager(manager.root, manager.source, manager.translate, "test")
    manager.db = replacement.db
    assert replacement.get(1)["status"] == "queued"
    assert replacement.get(1)["completed"] == 1
    assert replacement.option("paused", False)


async def test_offline_worker_keeps_retrying_without_failure_budget(manager):
    async def unavailable(data):
        raise WorkerUnavailable()

    manager.translate = unavailable
    manager.add(manager.source.meta)
    task = asyncio.create_task(manager.run())
    try:
        for _ in range(100):
            await asyncio.sleep(0.005)
            if manager.get(1)["status"] == "waiting":
                break
        assert manager.get(1)["status"] == "waiting"
        assert manager.get(1)["attempts"] == 0
        assert manager.lookup(png("white")) is None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "result,mime", [(b"<html>bad</html>", "image/png"), (png("gray"), "image/jpeg")]
)
async def test_bad_worker_result_is_never_ready(manager, result, mime):
    async def translate(data):
        return result, mime, "translated", 1

    manager.translate = translate
    manager.add(manager.source.meta)
    with pytest.raises((ValueError, OSError)):
        await manager.process(1)
    assert manager.get(1)["status"] != "ready"


def test_archive_rejects_count_mismatch_and_oversize(tmp_path, monkeypatch):
    path = tmp_path / "bad.cbz"
    path.write_bytes(archive_bytes([png("white")]))
    with pytest.raises(ValueError, match="count"):
        page_entries(path, 2)
    monkeypatch.setattr("suwayomi_translator.chapters.PAGE_LIMIT", 10)
    with pytest.raises(ValueError, match="oversized"):
        page_entries(path, 1)


async def test_local_archive_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setenv("SUWAYOMI_LOCAL_DIR", str(tmp_path))
    source = Suwayomi("http://source")
    try:
        with pytest.raises(ValueError, match="inside"):
            source.local_archive({"url": "../outside.cbz"})
    finally:
        await source.client.aclose()


async def test_reader_export_and_admin_without_worker(tmp_path, monkeypatch):
    monkeypatch.delenv("SUWAYOMI_URL", raising=False)
    monkeypatch.setenv("TRANSLATION_MODE", "chapters")
    monkeypatch.setenv("ADMIN_TOKEN", "admin")
    monkeypatch.setenv("PROCESSOR_TOKEN", "processor")
    app = create_app(tmp_path / "data", "http://worker")
    auth = {"X-Admin-Token": "admin"}
    reader = {"X-Processor-Token": "processor"}
    async with app.router.lifespan_context(app):
        await app.state.client.aclose()

        def offline(request):
            raise AssertionError("Prepared/unprepared reads must not call the worker")

        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
        source = Source(tmp_path / "source.cbz", [png("white")])

        async def translate(data):
            return png("gray"), "image/png", "translated", 2

        manager = ChapterManager(tmp_path / "data/chapters", source, translate, "test")
        app.state.chapters = manager
        manager.add(source.meta)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/admin/api/chapters")).status_code == 401
            r = await client.post("/convert", headers=reader, files={"image": png("white")})
            assert r.content == png("white") and r.headers["x-outcome"] == "unprepared"
            assert (
                await client.get("/admin/api/chapters/1/export", headers=auth)
            ).status_code == 409
            await manager.process(1)
            r = await client.post("/convert", headers=reader, files={"image": png("white")})
            assert r.content == png("gray") and r.headers["x-outcome"] == "prepared"
            export = await client.get("/admin/api/chapters/1/export", headers=auth)
            assert export.status_code == 200
            assert zipfile.ZipFile(io.BytesIO(export.content)).read("00001.png") == png("gray")
            assert (
                await client.post(
                    "/admin/api/chapters/options", headers=auth, json={"paused": "no"}
                )
            ).status_code == 400
            assert (
                await client.post(
                    "/admin/api/chapters/options",
                    headers={**auth, "Origin": "http://evil"},
                    json={"paused": True},
                )
            ).status_code == 403
            (manager.root / "1/translated.cbz").write_bytes(b"broken")
            assert (
                await client.get("/admin/api/chapters/1/export", headers=auth)
            ).status_code == 409
            assert manager.get(1)["status"] == "failed"
            assert (
                await client.post("/admin/api/chapters/1/remove", headers=auth)
            ).status_code == 200
            assert source.path.exists()


async def test_full_gateway_chapter_job_rejects_fallback_then_recovers(tmp_path, monkeypatch):
    import importlib

    module = importlib.import_module("suwayomi_translator.app")
    source = Source(tmp_path / "source.cbz", [png("white")])
    monkeypatch.setenv("SUWAYOMI_URL", "http://source")
    monkeypatch.setenv("ADMIN_TOKEN", "admin")
    monkeypatch.setattr(module, "Suwayomi", lambda base: source)
    app = create_app(tmp_path / "data", "http://worker")
    async with app.router.lifespan_context(app):
        await app.state.client.aclose()
        app.state.client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(503))
        )
        manager = app.state.chapters
        manager.add(source.meta)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if manager.get(1)["status"] == "waiting":
                break
        assert manager.get(1)["status"] == "waiting"
        assert manager.get(1)["completed"] == 0
        assert manager.lookup(png("white")) is None
        await app.state.client.aclose()
        app.state.client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, content=png("gray"), headers={"X-Outcome": "translated", "X-Regions": "1"}
                )
            )
        )
        app.state.store.db.execute("UPDATE jobs SET updated=0")
        app.state.store.db.commit()
        manager.update(1, next_try=0)
        for _ in range(200):
            await asyncio.sleep(0.01)
            if manager.get(1)["status"] == "ready":
                break
        assert manager.get(1)["status"] == "ready"
        assert manager.lookup(png("white"))[0] == png("gray")


async def test_profile_change_reprocesses_incomplete_chapter(manager):
    manager.add(manager.source.meta)
    await manager.process(1)
    manager.update(1, status="queued")
    manager.profile = "new-profile"
    calls = []

    async def translate(data):
        calls.append(data)
        return png("red"), "image/png", "translated", 2

    manager.translate = translate
    await manager.process(1)
    assert len(calls) == 2
    assert manager.get(1)["profile"] == "new-profile"
    assert manager.lookup(png("white"))[0] == png("red")
