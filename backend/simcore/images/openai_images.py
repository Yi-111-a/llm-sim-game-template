from __future__ import annotations

import base64

import httpx


class OpenAIImageProvider:
    def __init__(self, base_url: str, api_key: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def generate(self, prompt: str, size: tuple[int, int], seed: int | None = None) -> bytes:
        width, height = size
        body = {"model": self.model, "prompt": prompt, "size": f"{width}x{height}", "n": 1}
        if seed is not None:
            body["seed"] = seed
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{self.base_url}/images/generations",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            )
            response.raise_for_status()
        payload = response.json()
        if payload.get("data", [{}])[0].get("b64_json"):
            return base64.b64decode(payload["data"][0]["b64_json"])
        image_url = payload["data"][0]["url"]
        async with httpx.AsyncClient(timeout=60) as client:
            image_response = await client.get(image_url)
            image_response.raise_for_status()
            return image_response.content
