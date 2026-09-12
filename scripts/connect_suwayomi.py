"""Configure the image hook through Suwayomi's API, preserving a rollback file."""

import argparse
import json
import os
import urllib.request
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("--server", required=True, help="Trusted Suwayomi base URL")
p.add_argument("--processor", default="http://suwayomi-translator:8000/convert")
p.add_argument("--backup", default="serve-conversions-before.json")
p.add_argument("--restore", action="store_true")
a = p.parse_args()


def query(text, variables=None):
    request = urllib.request.Request(
        a.server.rstrip("/") + "/api/graphql",
        data=json.dumps({"query": text, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.load(response)
    if data.get("errors"):
        # Do not echo mutation variables, which contain a processor credential.
        raise RuntimeError("Suwayomi rejected the settings operation")
    return data["data"]


selection = "mimeType target compressionLevel callTimeout connectTimeout headers { name value }"
current = query("{settings {serveConversions {" + selection + "}}}")["settings"]["serveConversions"]
backup = Path(a.backup)
if a.restore:
    desired = json.loads(backup.read_text())
else:
    if not backup.exists():
        with open(backup, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
            json.dump(current, file)
    desired = [
        {
            "mimeType": "default",
            "target": a.processor,
            "callTimeout": "120s",
            "connectTimeout": "10s",
            "headers": [{"name": "X-Processor-Token", "value": os.environ["PROCESSOR_TOKEN"]}],
        }
    ]
query(
    "mutation($input:SetSettingsInput!){setSettings(input:$input){__typename}}",
    {"input": {"settings": {"serveConversions": desired}}},
)
actual = query("{settings {serveConversions {" + selection + "}}}")["settings"]["serveConversions"]
assert len(actual) == len(desired)
if desired:
    assert actual[0]["target"] == desired[0]["target"]
print("Suwayomi image processing configuration updated and read back successfully.")
