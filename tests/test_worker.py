import httpx
import pytest

from suwayomi_translator.worker import app, device


async def test_worker_rejects_unauthenticated_requests_before_parsing(monkeypatch):
    monkeypatch.setenv("WORKER_TOKEN", "private-worker-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        for headers in ({}, {"X-Worker-Token": "wrong"}):
            r = await client.post("/process", content=b"invalid body", headers=headers)
            assert r.status_code == 401
        r = await client.post("/process", headers={"X-Worker-Token": "private-worker-token"})
        assert r.status_code == 422  # Authorized; request validation now runs.
        monkeypatch.delenv("WORKER_TOKEN")
        assert (await client.post("/process")).status_code == 401


def test_explicit_device_selection(monkeypatch):
    monkeypatch.delenv("WORKER_DEVICE", raising=False)
    assert device() == "cpu"
    monkeypatch.setenv("WORKER_DEVICE", "cuda")
    assert device() == "cuda"
    monkeypatch.setenv("WORKER_DEVICE", "invalid")
    with pytest.raises(ValueError):
        device()


def test_config_upgrade_preserves_api_credentials_and_worker_token(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "init_config.py"
    worker = tmp_path / "worker.env"
    gateway = tmp_path / "gateway.env"
    worker.write_text("LLM_API_KEY=existing-private-key\n")
    gateway.write_text("ADMIN_TOKEN=existing-admin\n")
    first = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True)
    assert first.returncode == 0
    token = next(
        line for line in worker.read_text().splitlines() if line.startswith("WORKER_TOKEN=")
    )
    assert len(token) > 40 and token in gateway.read_text().splitlines()
    assert "LLM_API_KEY=existing-private-key" in worker.read_text()
    assert "ADMIN_TOKEN=existing-admin" in gateway.read_text()
    before = (worker.read_bytes(), gateway.read_bytes())
    second = subprocess.run([sys.executable, str(script)], cwd=tmp_path, capture_output=True)
    assert second.returncode == 0 and before == (worker.read_bytes(), gateway.read_bytes())
    assert b"existing-private-key" not in first.stdout + second.stdout
    assert worker.stat().st_mode & 0o777 == gateway.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("blocked,expected", [(True, 70), (False, 0)])
def test_inference_watchdog_recycles_stalls_but_cancels_after_success(blocked, expected):
    import os
    import subprocess
    import sys

    code = (
        "import time\nfrom suwayomi_translator.worker import inference_deadline\n"
        f"with inference_deadline():\n    time.sleep({1 if blocked else 0})\n"
        "time.sleep(0.2)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "WORKER_PAGE_TIMEOUT": "0.1"},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == expected
    if blocked:
        assert b"Inference deadline exceeded" in result.stderr
