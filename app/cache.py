import hashlib
import time
from threading import Lock


class InMemoryTTLCache:
    def __init__(self, ttl_seconds: int = 300, max_items: int = 256) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._store: dict[str, tuple[str, float]] = {}
        self._lock = Lock()

    def get(self, key: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if now >= expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: str) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._store) >= self.max_items and key not in self._store:
                oldest_key = next(iter(self._store))
                del self._store[oldest_key]
            self._store[key] = (value, now + self.ttl_seconds)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


ai_response_cache = InMemoryTTLCache(ttl_seconds=300, max_items=256)

NON_SENSITIVE_FAQ = {
    "what is fastapi": "FastAPI is a Python web framework for building APIs with type hints and automatic validation.",
    "what is sqlalchemy": "SQLAlchemy is a Python SQL toolkit and ORM for interacting with relational databases.",
    "what is jwt": "JWT is a signed compact token format used to transmit verified claims such as user identity.",
}


def generate_assistant_reply(prompt: str) -> tuple[str, bool]:
    normalized = " ".join(prompt.strip().lower().rstrip("?!.").split())
    cache_key = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    if normalized in NON_SENSITIVE_FAQ:
        cached_value = ai_response_cache.get(cache_key)
        if cached_value is not None:
            return cached_value, True
        response = NON_SENSITIVE_FAQ[normalized]
        ai_response_cache.set(cache_key, response)
        return response, False

    return f"Assistant response to: {prompt.strip()}", False
