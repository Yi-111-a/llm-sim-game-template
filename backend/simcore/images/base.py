from __future__ import annotations

from typing import Protocol


class ImageProvider(Protocol):
    async def generate(self, prompt: str, size: tuple[int, int], seed: int | None = None) -> bytes: ...


class MockImageProvider:
    async def generate(self, prompt: str, size: tuple[int, int], seed: int | None = None) -> bytes:
        from simcore.images.placeholder import placeholder_svg

        return placeholder_svg("生成预览", "mock", prompt[:80], size).encode("utf-8")
