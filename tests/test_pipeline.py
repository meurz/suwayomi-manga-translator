import asyncio
import io
import json
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image
from test_chapters import archive_bytes, png
from test_chapters import manager as manager  # noqa: F401

from suwayomi_translator import worker
from suwayomi_translator.concurrency import page_concurrency
from suwayomi_translator.provider import Decision


@pytest.mark.parametrize("value", ["0", "3", "invalid"])
def test_page_concurrency_is_bounded(monkeypatch, value):
    monkeypatch.setenv("PAGE_CONCURRENCY", value)
    with pytest.raises(ValueError):
        page_concurrency()


@pytest.mark.parametrize("failure", [False, True])
async def test_worker_overlaps_cloud_serializes_gpu_and_isolates_pages(monkeypatch, failure):
    """Exercise the actual HTTP handler and translator subclass with fake model stages."""
    config = SimpleNamespace(render=SimpleNamespace(alignment="center", direction="horizontal"))
    gpu_active = 0
    engines = []
    cloud_active = 0
    cloud_ready = threading.Event()
    release_cloud = threading.Event()
    counter_lock = threading.Lock()

    def gpu_stage():
        nonlocal gpu_active
        with counter_lock:
            gpu_active += 1
            assert gpu_active == 1
        time.sleep(0.01)
        with counter_lock:
            gpu_active -= 1

    class FakeTranslator:
        def __init__(self, params):
            engines.append(self)

        async def translate(self, source, config, **kwargs):
            gpu_stage()
            text = "こんにちは" if source.getpixel((0, 0))[0] else "这已经是中文对白"
            ctx = SimpleNamespace(text_regions=[SimpleNamespace(text=text)], mask="original")
            ctx.text_regions = await self._run_text_translation(config, ctx)
            gpu_stage()
            assert ctx.mask is None
            ctx.result = Image.new("RGB", source.size, "red")
            return ctx

    async def cloud(texts):
        nonlocal cloud_active
        with counter_lock:
            cloud_active += 1
            if cloud_active == 2:
                cloud_ready.set()
        assert await asyncio.to_thread(release_cloud.wait, 5)
        if failure and texts[0] == "こんにちは":
            raise RuntimeError("Simulated cloud outage")
        return [
            Decision(
                id=0,
                language="ja" if texts[0] == "こんにちは" else "zh",
                confident=False if texts[0] == "こんにちは" else True,
                translation="你好" if texts[0] == "こんにちは" else texts[0],
            )
        ]

    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(set_num_threads=lambda n: None, set_num_interop_threads=lambda n: None),
    )
    monkeypatch.setitem(
        sys.modules, "manga_translator", SimpleNamespace(MangaTranslator=FakeTranslator)
    )
    monkeypatch.setitem(
        sys.modules,
        "manga_translator.config",
        SimpleNamespace(Config=SimpleNamespace(model_validate=lambda v: config)),
    )
    monkeypatch.setenv("WORKER_DEVICE", "cpu")
    monkeypatch.setenv("WORKER_TOKEN", "test-token")
    monkeypatch.setattr(worker, "_translator_class", None)
    monkeypatch.setattr(worker, "_lock", threading.Lock())
    monkeypatch.setattr(worker, "_admission", threading.BoundedSemaphore(2))
    monkeypatch.setattr(worker, "translate_regions", cloud)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=worker.app),
        base_url="http://worker",
        headers={"X-Worker-Token": "test-token"},
    ) as c:
        a = asyncio.create_task(c.post("/process?force=true", files={"image": png("white")}))
        b = asyncio.create_task(c.post("/process", files={"image": png("black")}))
        try:
            assert await asyncio.to_thread(cloud_ready.wait, 5), "Two cloud calls must overlap"
            assert (await c.post("/process", files={"image": png("blue")})).status_code == 503
        finally:
            release_cloud.set()
        ja, zh = await asyncio.gather(a, b)
        assert zh.status_code == 200 and zh.content == png("black")
        assert json.loads(zh.headers["x-language-evidence"])["zh_regions"] == 1
        assert ja.status_code == (502 if failure else 200)
        if not failure:
            assert ja.headers["x-outcome"] == "translated"  # force remained local to this page
            assert json.loads(ja.headers["x-language-evidence"])["zh_regions"] == 0
            assert Image.open(io.BytesIO(ja.content)).getpixel((0, 0)) == (255, 0, 0)
        assert len(engines) == 2 and engines[0] is not engines[1]
        assert not worker._lock.locked()
        assert worker._admission.acquire(False) and worker._admission.acquire(False)
        worker._admission.release()
        worker._admission.release()


@pytest.mark.parametrize("stop", ["pause", "cancel", "original", "failure"])
async def test_chapter_drains_inflight_pages_without_starting_more(manager, stop):
    manager.source.path.write_bytes(
        archive_bytes([png(c) for c in ["white", "black", "red", "blue"]])
    )
    manager.source.meta["pageCount"] = 4
    started = []
    both = asyncio.Event()
    release = asyncio.Event()

    async def translate(data):
        started.append(data)
        if len(started) == 2:
            both.set()
        await release.wait()
        if stop == "failure" and data == png("white"):
            raise RuntimeError("cloud failed")
        return data, "image/png", "skipped", 0

    manager.translate = translate
    manager.add(manager.source.meta)
    task = asyncio.create_task(manager.process(1))
    await asyncio.wait_for(both.wait(), 2)
    assert manager.lookup(png("white")) is None
    if stop == "pause":
        manager.set_option("paused", True)
    elif stop == "cancel":
        manager.update(1, status="cancelled")
    elif stop == "original":
        manager.set_policy(2, "original")
    release.set()
    if stop == "failure":
        with pytest.raises(RuntimeError, match="cloud failed"):
            await task
        assert manager.get(1)["completed"] == 1

        # The later page survived the earlier page's failure; resumption skips it.
        async def recover(data):
            assert data != png("black")
            return data, "image/png", "skipped", 0

        manager.translate = recover
        await manager.process(1)
        assert manager.get(1)["status"] == "ready"
    else:
        await task
        assert (
            manager.get(1)["status"]
            == {"pause": "queued", "cancel": "cancelled", "original": "original"}[stop]
        )
        assert not (manager.root / "1/translated.cbz").exists()
    assert len(started) == 2


async def test_gateway_allows_two_chapter_requests_and_publishes_ordered_archive(
    manager, monkeypatch
):
    import importlib

    from suwayomi_translator.app import create_app

    module = importlib.import_module("suwayomi_translator.app")
    monkeypatch.setenv("SUWAYOMI_URL", "http://source")
    monkeypatch.setattr(module, "Suwayomi", lambda url: manager.source)
    app = create_app(manager.root / "gateway", "http://worker")
    ready = asyncio.Event()
    calls = 0

    async def response(request):
        nonlocal calls
        calls += 1
        if calls == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 2)
        return httpx.Response(200, content=png("gray"), headers={"X-Outcome": "translated"})

    async with app.router.lifespan_context(app):
        chapters = app.state.chapters
        chapters.set_option("paused", True)
        await app.state.client.aclose()
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        chapters.add(manager.source.meta)
        chapters.set_option("paused", False)
        # Let the real background scheduler drive this job.
        for _ in range(400):
            if chapters.get(1)["status"] == "ready":
                break
            await asyncio.sleep(0.01)
        assert chapters.get(1)["status"] == "ready"
        assert calls == 2
