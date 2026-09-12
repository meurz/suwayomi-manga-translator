"""Use Suwayomi's trusted internal API without passing user-provided URLs through."""

import os
from pathlib import Path

import httpx

CHAPTER_FIELDS = "id mangaId name sourceOrder pageCount isDownloaded url manga { title sourceId }"


class Suwayomi:
    def __init__(self, base):
        headers = {}
        if os.getenv("SUWAYOMI_AUTHORIZATION"):
            headers["Authorization"] = os.environ["SUWAYOMI_AUTHORIZATION"]
        self.client = httpx.AsyncClient(
            base_url=base.rstrip("/"), headers=headers, timeout=60, trust_env=False
        )

    async def query(self, query, variables=None):
        response = await self.client.post(
            "/api/graphql", json={"query": query, "variables": variables or {}}
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            raise RuntimeError("Suwayomi rejected the chapter query")
        return data["data"]

    async def chapter(self, chapter_id):
        data = await self.query(
            "query($id:Int!){chapter(id:$id){" + CHAPTER_FIELDS + "}}", {"id": chapter_id}
        )
        chapter = data["chapter"]
        if chapter["pageCount"] < 1:
            response = await self.client.get(
                f"/api/v1/manga/{chapter['mangaId']}/chapter/{chapter['sourceOrder']}"
            )
            response.raise_for_status()
            data = await self.query(
                "query($id:Int!){chapter(id:$id){" + CHAPTER_FIELDS + "}}", {"id": chapter_id}
            )
            chapter = data["chapter"]
        return chapter

    async def downloaded(self):
        result = []
        offset = 0
        while True:
            data = await self.query(
                "query($offset:Int!){chapters(condition:{isDownloaded:true},first:200,"
                "offset:$offset,order:[{by:ID,byType:ASC}]){nodes{"
                + CHAPTER_FIELDS
                + "} totalCount}}",
                {"offset": offset},
            )
            page = data["chapters"]["nodes"]
            result.extend(page)
            offset += len(page)
            if not page or offset >= data["chapters"]["totalCount"]:
                return result

    async def enqueue_download(self, chapter_id):
        response = await self.client.post(
            "/api/v1/download/batch", json={"chapterIds": [chapter_id]}
        )
        response.raise_for_status()

    async def manga(self, manga_id):
        response = await self.client.get(f"/api/v1/manga/{manga_id}")
        response.raise_for_status()
        manga = response.json()
        response = await self.client.get(f"/api/v1/manga/{manga_id}/chapters")
        response.raise_for_status()
        return {"id": manga_id, "title": manga["title"], "chapters": response.json()}

    def local_archive(self, chapter):
        root = Path(os.environ.get("SUWAYOMI_LOCAL_DIR", "/suwayomi-local")).resolve()
        path = (root / chapter["url"]).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() not in {".cbz", ".zip"}:
            raise ValueError(
                "Only CBZ/ZIP chapters inside the configured Local source are supported"
            )
        if not path.is_file():
            raise ValueError("Local chapter archive is unavailable; check the read-only mount")
        return path
