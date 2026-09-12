"""Create local configuration without printing credentials or overwriting files."""

import os
import secrets
from pathlib import Path

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
        print(name + ": kept existing file")
        continue
    with open(name, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
        file.write(content)
    print(name + ": created (0600)")
print("Set the API endpoint, key and model in worker.env before starting the worker.")
