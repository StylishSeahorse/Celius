"""Optional embeddings. Everything works without them (keyword search); with them,
retrieval ranks by meaning as well as words."""

import logging
import math

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

# Tests point this at a fake server; production leaves it None.
TRANSPORT: httpx.AsyncBaseTransport | None = None
MAX_BATCH = 32


class Embedder:
    def __init__(self, base_url: str, model: str | None, api_key: str | None):
        self.model = model or "default"
        self._model_param = model
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=60, transport=TRANSPORT
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "Embedder | None":
        if not settings.model_enable_embeddings:
            return None
        key = settings.embedding_api_key or settings.model_api_key
        return cls(
            settings.embedding_base_url or settings.model_base_url,
            settings.embedding_model,
            key.get_secret_value() if key else None,
        )

    async def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Vectors for `texts`, or None if the embedding server is unavailable."""
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), MAX_BATCH):
                body: dict = {"input": texts[start : start + MAX_BATCH]}
                if self._model_param:
                    body["model"] = self._model_param
                response = await self._client.post("/embeddings", json=body)
                response.raise_for_status()
                data = sorted(response.json()["data"], key=lambda d: d.get("index", 0))
                vectors += [_normalise(d["embedding"]) for d in data]
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            log.warning("Embeddings unavailable (%s); falling back to keyword search", exc)
            return None
        return vectors if len(vectors) == len(texts) else None

    async def aclose(self) -> None:
        await self._client.aclose()


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [float(x) / norm for x in vector]


def cosine(a, b) -> float:
    """Vectors are stored normalised, so this is a dot product."""
    if a is None or b is None or len(a) != len(b):
        return 0.0
    return float(sum(x * y for x, y in zip(a, b, strict=True)))
