import json

import pytest
from test_chapters import Source, archive_bytes, png

from suwayomi_translator.chapters import ChapterManager
from suwayomi_translator.language import empty_evidence, parse_evidence, summarize_languages
from suwayomi_translator.provider import Decision


def chinese():
    return {**empty_evidence(), "zh_regions": 3, "zh_chars": 40}


@pytest.fixture
async def manager(tmp_path):
    source = Source(tmp_path / "source.cbz", [png("white"), png("black"), png("blue")])

    async def translate(data):
        return data, "image/png", "skipped", 0, chinese()

    manager = ChapterManager(tmp_path / "prepared", source, translate, "test")
    yield manager
    await manager.close()


async def test_confirmed_chinese_book_bypasses_future_chapters_and_survives_restart(manager):
    manager.add(manager.source.meta)
    await manager.process(1)
    assert manager.policy(2)["detected"] == "chinese"
    source_original = manager.source.path.read_bytes()
    manager.source.path.write_bytes(archive_bytes([png("red"), png("green"), png("yellow")]))

    async def offline(data):
        raise AssertionError("Known Chinese manga must not call a model")

    manager.translate = offline
    manager.add(await manager.source.chapter(2))
    await manager.process(2)
    assert manager.get(2)["status"] == "original"
    assert not (manager.root / "2/translated.cbz").exists()
    assert not (manager.root / "2/original.cbz").exists()
    assert manager.lookup(png("red"))[0] == png("red")
    assert (manager.root / "1/original.cbz").read_bytes() == source_original
    manager.db.close()
    reopened = ChapterManager(manager.root, manager.source, offline, "test")
    manager.db = reopened.db
    assert reopened.bypass(2)
    assert reopened.lookup(png("red"))[0] == png("red")
    assert reopened.summary()["jobs"][0]["uses_original"]


@pytest.mark.parametrize("kind", ["blank", "cover", "uncertain", "mixed", "legacy", "duplicate"])
async def test_insufficient_or_mixed_evidence_never_skips_entire_book(manager, kind):
    if kind == "duplicate":
        manager.source.path.write_bytes(archive_bytes([png("white")] * 3))
    calls = 0

    async def translate(data):
        nonlocal calls
        calls += 1
        evidence = chinese()
        if kind == "blank" or kind == "cover" and calls > 1:
            evidence = empty_evidence()
        if kind == "uncertain":
            evidence["uncertain_regions"] = 1
        if kind == "mixed" and calls == 3:
            evidence["foreign_regions"] = 1
        if kind == "legacy":
            return data, "image/png", "skipped", 0
        return data, "image/png", "skipped", 0, evidence

    manager.translate = translate
    manager.add(manager.source.meta)
    await manager.process(1)
    assert not manager.bypass(2)
    assert manager.policy(2)["detected"] == "unknown"


async def test_prior_foreign_chapter_blocks_later_chinese_only_chapter(manager):
    original_translate = manager.translate

    async def foreign(data):
        return png("gray"), "image/png", "translated", 1, {**empty_evidence(), "foreign_regions": 1}

    manager.translate = foreign
    manager.add(manager.source.meta)
    await manager.process(1)
    manager.translate = original_translate
    manager.add(await manager.source.chapter(2))
    await manager.process(2)
    assert not manager.bypass(2)
    assert manager.policy(2)["foreign_seen"]
    manager.set_policy(2, "auto")
    assert manager.policy(2)["foreign_seen"]


async def test_manual_original_override_and_restore_preserve_ready_translation(manager):
    async def foreign(data):
        return png("gray"), "image/png", "translated", 1

    manager.translate = foreign
    manager.add(manager.source.meta)
    await manager.process(1)
    assert manager.lookup(png("white"))[0] == png("gray")
    manager.set_policy(2, "original")
    assert manager.lookup(png("white"))[0] == png("white")
    manager.set_policy(2, "translate")
    assert manager.lookup(png("white"))[0] == png("gray")
    await manager.process(1)
    assert not manager.bypass(2)


async def test_manual_override_during_active_page_stops_further_inference(manager):
    calls = 0

    async def translate(data):
        nonlocal calls
        calls += 1
        manager.set_policy(2, "original")
        return data, "image/png", "skipped", 0, chinese()

    manager.translate = translate
    manager.add(manager.source.meta)
    await manager.process(1)
    assert calls == 1
    assert manager.get(1)["status"] == "original"
    assert manager.lookup(png("blue"))[0] == png("blue")
    manager.set_policy(2, "auto")
    assert manager.get(1)["status"] == "available"
    assert not manager.bypass(2)


async def test_chapter_failure_does_not_establish_chinese_language(manager):
    manager.source.path.write_bytes(
        archive_bytes([png(c) for c in ["white", "black", "blue", "red"]])
    )
    manager.source.meta["pageCount"] = 4

    async def translate(data):
        if data == png("red"):
            raise RuntimeError("Missing final page")
        return data, "image/png", "skipped", 0, chinese()

    manager.translate = translate
    manager.add(manager.source.meta)
    with pytest.raises(RuntimeError):
        await manager.process(1)
    assert manager.get(1)["completed"] == 3
    assert not manager.bypass(2)


async def test_rule_is_scoped_to_source_manga_id_not_title(manager):
    manager.add(manager.source.meta)
    await manager.process(1)
    other = {**manager.source.meta, "id": 2, "mangaId": 99}
    manager.add(other)
    assert manager.bypass(2)
    assert not manager.bypass(99)


async def test_always_process_prevents_auto_book_skip(manager):
    manager.add(manager.source.meta)
    manager.set_policy(2, "translate")
    await manager.process(1)
    assert not manager.bypass(2)


def test_language_evidence_retains_counts_not_dialogue():
    texts = ["这是中文对白，需要保留原文。", "こんにちは", "東京", "..."]
    decisions = [
        Decision(id=i, language=lang, confident=confident, translation="")
        for i, (lang, confident) in enumerate(
            [("zh", True), ("ja", True), ("zh", False), ("und", False)]
        )
    ]
    evidence = summarize_languages(texts, decisions)
    assert evidence["zh_regions"] == 1 and evidence["zh_chars"] > 10
    assert evidence["foreign_regions"] == 1 and evidence["uncertain_regions"] == 1
    assert all(text not in json.dumps(evidence, ensure_ascii=False) for text in texts)
    assert parse_evidence(json.dumps(evidence)) == evidence
    assert parse_evidence('{"version":1}') == {}
    assert parse_evidence({**evidence, "foreign_regions": -1}) == {}


async def test_original_policy_still_downloads_native_chapter_without_models(manager):
    manager.source.meta["manga"]["sourceId"] = "123"
    manager.add(manager.source.meta)
    manager.set_policy(2, "original")

    async def forbidden(data):
        raise AssertionError("Original-only chapter must not call worker")

    manager.translate = forbidden
    await manager.process(1)
    assert manager.source.enqueued == [1]
    assert manager.get(1)["status"] == "downloading"
    manager.source.meta["isDownloaded"] = True
    await manager.process(1)
    assert manager.get(1)["status"] == "original"
    assert manager.source.path.exists()


async def test_worker_evidence_reaches_policy_and_api_override_bypasses_online(
    tmp_path, monkeypatch
):
    import asyncio
    import importlib

    import httpx

    from suwayomi_translator.app import create_app

    module = importlib.import_module("suwayomi_translator.app")
    source = Source(tmp_path / "source.cbz", [png("white"), png("black"), png("blue")])
    monkeypatch.setenv("SUWAYOMI_URL", "http://source")
    monkeypatch.setenv("ADMIN_TOKEN", "admin")
    monkeypatch.setenv("PROCESSOR_TOKEN", "processor")
    monkeypatch.setenv("TRANSLATION_MODE", "online")
    monkeypatch.setattr(module, "Suwayomi", lambda base: source)
    calls = 0

    def worker(request):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            content=png("gray"),
            headers={"X-Outcome": "skipped", "X-Language-Evidence": json.dumps(chinese())},
        )

    app = create_app(tmp_path / "data", "http://worker")
    async with app.router.lifespan_context(app):
        await app.state.client.aclose()
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(worker))
        manager = app.state.chapters
        manager.add(source.meta)
        async with asyncio.timeout(5):
            while manager.get(1)["status"] != "ready":
                await asyncio.sleep(0.01)
        assert calls == 3 and manager.bypass(2)
        auth = {"X-Admin-Token": "admin"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            assert (
                await c.post("/admin/api/manga/2/policy", json={"mode": "original"})
            ).status_code == 401
            assert (
                await c.post("/admin/api/manga/2/policy", headers=auth, json={"mode": "bad"})
            ).status_code == 400
            assert (
                await c.post(
                    "/admin/api/manga/2/policy",
                    headers={**auth, "Origin": "http://evil"},
                    json={"mode": "original"},
                )
            ).status_code == 403
            assert (
                await c.post("/admin/api/manga/2/policy", headers=auth, json={"mode": "original"})
            ).status_code == 200
            r = await c.post(
                "/convert",
                headers={"X-Processor-Token": "processor"},
                files={"image": png("white")},
            )
            assert r.content == png("white") and calls == 3
            assert (await c.get("/admin/api/chapters/1/export", headers=auth)).status_code == 409
            r = await c.post("/admin/api/manga/2/policy", headers=auth, json={"mode": "auto"})
            assert r.json()["detected"] == "unknown"
