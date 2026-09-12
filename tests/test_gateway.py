import asyncio
import io

import httpx
import pytest
from PIL import Image

from suwayomi_translator.app import Store, cache_key, create_app
from suwayomi_translator.provider import Decision, should_translate, validate_decisions


def png(color="white"):
    out = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
async def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESSOR_TOKEN", "test-processor")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin")
    app = create_app(tmp_path, "http://worker")
    async with app.router.lifespan_context(app):
        await app.state.client.aclose()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield app, client, tmp_path
        await app.state.client.aclose()


def backend(app, handler):
    app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_deduplicates_concurrent_pages_and_caches(setup):
    app, client, root = setup
    calls = 0

    async def worker(request):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return httpx.Response(
            200, content=png("gray"), headers={"X-Outcome": "translated", "X-Regions": "2"}
        )

    backend(app, worker)

    async def request():
        return await client.post(
            "/convert", headers={"X-Processor-Token": "test-processor"}, files={"image": png()}
        )

    one, two = await asyncio.gather(request(), request())
    assert calls == 1
    assert one.content == two.content == png("gray")
    three = await request()
    assert three.headers["x-cache"] == "HIT" and calls == 1
    assert app.state.store.recent()[0]["regions"] == 2


async def test_chinese_skip_preserves_exact_original_bytes(setup):
    app, client, _ = setup
    backend(
        app, lambda r: httpx.Response(200, content=png("gray"), headers={"X-Outcome": "skipped"})
    )
    original = png()
    r = await client.post(
        "/convert", headers={"X-Processor-Token": "test-processor"}, files={"image": original}
    )
    assert r.content == original
    assert r.headers["x-outcome"] == "skipped"


async def test_failure_returns_original_and_does_not_cache_success(setup):
    app, client, _ = setup
    backend(app, lambda r: httpx.Response(500, text="private upstream error"))
    r = await client.post(
        "/convert", headers={"X-Processor-Token": "test-processor"}, files={"image": png()}
    )
    assert r.content == png()
    assert r.headers["x-outcome"] == "fallback"
    row = app.state.store.recent()[0]
    assert row["status"] == "failed" and "private" not in row["error"]


async def test_rejects_html_worker_output(setup):
    app, client, _ = setup
    backend(
        app,
        lambda r: httpx.Response(
            200, text="<html>error</html>", headers={"X-Outcome": "translated"}
        ),
    )
    r = await client.post(
        "/convert", headers={"X-Processor-Token": "test-processor"}, files={"image": png()}
    )
    assert r.content == png() and r.headers["x-outcome"] == "fallback"


async def test_timeout_keeps_background_job(setup, monkeypatch):
    app, client, _ = setup
    monkeypatch.setenv("READER_WAIT", "0.005")
    monkeypatch.setenv("STREAM_AFTER", "0.001")

    async def worker(request):
        await asyncio.sleep(0.03)
        return httpx.Response(200, content=png("gray"), headers={"X-Outcome": "translated"})

    backend(app, worker)
    r = await client.post(
        "/convert", headers={"X-Processor-Token": "test-processor"}, files={"image": png()}
    )
    assert r.headers["x-outcome"] == "streamed"
    assert (
        Image.open(io.BytesIO(r.content)).convert("RGB").tobytes()
        == Image.open(io.BytesIO(png())).tobytes()
    )
    await asyncio.sleep(0.04)
    assert app.state.store.recent()[0]["status"] == "translated"


async def test_auth_validation_pause_and_admin(setup):
    app, client, _ = setup
    assert (await client.get("/admin/api/status")).status_code == 401
    assert (await client.post("/convert", files={"image": png()})).status_code == 401
    auth = {"X-Admin-Token": "test-admin"}
    assert (
        await client.post("/admin/api/enabled", headers=auth, json={"enabled": "false"})
    ).status_code == 400
    assert (
        await client.post("/admin/api/enabled", headers=auth, json={"enabled": False})
    ).status_code == 200
    r = await client.post(
        "/convert", headers={"X-Processor-Token": "test-processor"}, files={"image": png()}
    )
    assert r.content == png() and r.headers["x-outcome"] == "disabled"
    r = await client.post(
        "/convert", headers={"X-Processor-Token": "test-processor"}, files={"image": b"junk"}
    )
    assert r.status_code == 400


def test_cache_varies_with_model_language_and_force():
    assert len({cache_key(png(), "a"), cache_key(png(), "b"), cache_key(png(), "a", True)}) == 3


def test_chinese_and_uncertain_text_are_excluded():
    assert not should_translate(
        Decision(id=0, language="zh", confident=True, translation="你好"), "您好", True
    )
    assert not should_translate(
        Decision(id=0, language="ja", confident=False, translation="东京"), "東京"
    )
    assert should_translate(
        Decision(id=0, language="ja", confident=True, translation="早上好"), "おはよう"
    )
    assert not should_translate(
        Decision(id=0, language="und", confident=False, translation="x"), "???", True
    )


def test_provider_missing_and_duplicate_ids_are_rejected():
    with pytest.raises(ValueError):
        validate_decisions('{"regions":[]}', 1)
    with pytest.raises(ValueError):
        validate_decisions(
            '{"regions":[{"id":0,"language":"ja","confident":true,"translation":"x"},'
            '{"id":0,"language":"ja","confident":true,"translation":"y"}]}',
            2,
        )


def test_restart_marks_pending_jobs_failed_and_prunes(tmp_path):
    store = Store(tmp_path)
    store.put("a", png(), "image/png", False)
    store.db.close()
    store = Store(tmp_path)
    assert store.get("a")["status"] == "failed"
    store.prune(0)
    assert store.get("a") is None and not (tmp_path / "a.original").exists()
    store.db.close()


async def test_png_stream_waits_and_delivers_valid_translated_pixels(setup, monkeypatch):
    app, client, _ = setup
    monkeypatch.setenv("STREAM_AFTER", "0.005")

    async def worker(request):
        await asyncio.sleep(0.03)
        return httpx.Response(200, content=png("gray"), headers={"X-Outcome": "translated"})

    backend(app, worker)
    response = await client.post(
        "/convert", headers={"X-Processor-Token": "test-processor"}, files={"image": png()}
    )
    assert response.headers["x-outcome"] == "streamed"
    image = Image.open(io.BytesIO(response.content))
    image.load()
    assert image.size == (64, 64) and image.convert("RGB").getpixel((30, 30)) == (128, 128, 128)


async def test_slow_failure_streams_original_pixels(setup, monkeypatch):
    app, client, _ = setup
    monkeypatch.setenv("STREAM_AFTER", "0.005")

    async def worker(request):
        await asyncio.sleep(0.03)
        return httpx.Response(503)

    backend(app, worker)
    response = await client.post(
        "/convert", headers={"X-Processor-Token": "test-processor"}, files={"image": png()}
    )
    image = Image.open(io.BytesIO(response.content))
    image.load()
    assert image.convert("RGB").tobytes() == Image.open(io.BytesIO(png())).tobytes()


def test_heartbeat_chunks_do_not_change_pixels():
    from suwayomi_translator.pngstream import finish, heartbeat, prefix

    data = png("red")
    start, size = prefix(data)
    result = start + heartbeat() * 20 + finish(data, size)
    image = Image.open(io.BytesIO(result))
    image.load()
    assert image.convert("RGB").tobytes() == Image.open(io.BytesIO(data)).tobytes()


def test_erased_edge_text_gets_fallback_without_changing_other_regions():
    # Pillow includes DejaVuSans on the tested platforms; locate it through its loader.
    from PIL import ImageFont

    from suwayomi_translator.render_guard import ensure_rendered

    font = ImageFont.truetype("DejaVuSans.ttf", 12).path
    base = Image.new("RGB", (200, 200), "white")
    result, count = ensure_rendered(base, base, [((10, -20, 100, 100), "Edge text")], font)
    assert count == 1
    assert result.crop((10, 0, 100, 100)).tobytes() != base.crop((10, 0, 100, 100)).tobytes()
    assert result.crop((101, 0, 200, 200)).tobytes() == base.crop((101, 0, 200, 200)).tobytes()
