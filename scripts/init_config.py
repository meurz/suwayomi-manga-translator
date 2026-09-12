"""Create local configuration without printing credentials or overwriting files."""

import os
import secrets
from pathlib import Path

tokens = set()
for name in ("worker.env", "gateway.env"):
    if Path(name).exists():
        for line in Path(name).read_text().splitlines():
            if line == "WORKER_TOKEN=":
                raise SystemExit(
                    name + ": remove the empty WORKER_TOKEN line before initialization."
                )
            if line.startswith("WORKER_TOKEN=") and line.removeprefix("WORKER_TOKEN="):
                tokens.add(line.removeprefix("WORKER_TOKEN="))
if len(tokens) > 1:
    raise SystemExit("WORKER_TOKEN differs between worker.env and gateway.env; reconcile it first.")
worker_token = next(iter(tokens), secrets.token_urlsafe(32))

files = {
    "worker.env": "LLM_BASE_URL=https://api.openai.com/v1\nLLM_API_KEY=\nLLM_MODEL=\nLLM_API=responses\n",
    "gateway.env": "PROCESSOR_TOKEN="
    + secrets.token_urlsafe(32)
    + "\nADMIN_TOKEN="
    + secrets.token_urlsafe(32)
    + "\nCACHE_PROFILE=chs-v1\nPUBLIC_MODEL=custom\n",
}
for name, content in files.items():
    if Path(name).exists():
        path = Path(name)
        existing = path.read_text()
        if not any(line.startswith("WORKER_TOKEN=") for line in existing.splitlines()):
            with path.open("a") as file:
                file.write("\nWORKER_TOKEN=" + worker_token + "\n")
            path.chmod(0o600)
            print(name + ": added worker authentication (0600)")
        else:
            print(name + ": kept existing file")
        continue
    with open(name, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
        file.write(content + "WORKER_TOKEN=" + worker_token + "\n")
    print(name + ": created (0600)")
print("Set the API endpoint, key and model in worker.env before starting the worker.")
