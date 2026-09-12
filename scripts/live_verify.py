"""Exercise a deployed service; inputs stay outside the source repository."""

import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("pages", nargs="+")
p.add_argument("--base", default="http://127.0.0.1:18440")
a = p.parse_args()
token = os.environ["ADMIN_TOKEN"]
headers = {"X-Admin-Token": token}
for name in a.pages:
    page = Path(name)
    boundary = "suwayomi-translation-verification"
    data = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="page.png"\r\n'
            "Content-Type: image/png\r\n\r\n"
        ).encode()
        + page.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(
        a.base + "/admin/api/upload",
        data=data,
        headers={**headers, "Content-Type": "multipart/form-data; boundary=" + boundary},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        key = json.load(r)["id"]
    print("Submitted", page.name, key[:8], flush=True)
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        with urllib.request.urlopen(
            urllib.request.Request(a.base + "/admin/api/status", headers=headers)
        ) as r:
            job = next(j for j in json.load(r)["jobs"] if j["id"] == key)
        if job["status"] not in ["queued", "processing"]:
            print(json.dumps({"page": page.name, **job}), flush=True)
            if job["status"] in ["translated", "skipped"]:
                with urllib.request.urlopen(
                    urllib.request.Request(
                        a.base + "/admin/api/jobs/" + key + "/result", headers=headers
                    )
                ) as r:
                    page.with_name(page.stem + "-result.png").write_bytes(r.read())
            break
        time.sleep(2)
    else:
        raise RuntimeError("Verification timed out")
