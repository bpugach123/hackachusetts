"""Content cache for fetched payloads (Req 2.2, 2.3, 2.4).

A fetch result is stored as raw bytes under
``data/cache/{source}/{key}.bin`` with a sibling ``{key}.meta.json`` holding the
fetch date, URL, and params. The key is
``sha256(source + url + canonical_json(params))`` so the same request always maps
to the same files.

The client uses this as:

1. ``read`` on a cache hit; if the bytes parse, return them with no network call.
2. On a parse failure, ``evict`` the entry and fetch live.
3. ``write`` the live payload after a successful fetch.

Only bytes live here. Parsing (JSON, GeoJSON, netCDF, CSV) is the caller's job, so
the cache never needs to know a payload's shape.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def canonical_json(params: Any) -> str:
    """Stable JSON for hashing: sorted keys, no insignificant whitespace."""
    return json.dumps(params, sort_keys=True, separators=(",", ":"), default=str)


def cache_key(source: str, url: str, params: Any) -> str:
    """``sha256(source + url + canonical_json(params))`` as a hex digest."""
    h = hashlib.sha256()
    h.update(source.encode("utf-8"))
    h.update(url.encode("utf-8"))
    h.update(canonical_json(params).encode("utf-8"))
    return h.hexdigest()


@dataclass(frozen=True)
class CacheEntry:
    payload: bytes
    meta: dict[str, Any]


class Cache:
    """Keyed byte store rooted at ``root`` (typically ``data/cache``)."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _dir(self, source: str) -> Path:
        return self.root / source

    def _paths(self, source: str, key: str) -> tuple[Path, Path]:
        d = self._dir(source)
        return d / f"{key}.bin", d / f"{key}.meta.json"

    def read(self, source: str, key: str) -> CacheEntry | None:
        """Return the cached entry, or ``None`` if either file is missing/unreadable."""
        bin_path, meta_path = self._paths(source, key)
        if not bin_path.exists() or not meta_path.exists():
            return None
        try:
            payload = bin_path.read_bytes()
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return CacheEntry(payload=payload, meta=meta)

    def write(
        self, source: str, key: str, payload: bytes, url: str, params: Any
    ) -> None:
        """Store ``payload`` plus a meta sidecar (fetch date, url, params)."""
        bin_path, meta_path = self._paths(source, key)
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "source": source,
            "url": url,
            "params": params,
            "fetched_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "fetch_date": datetime.now(UTC).strftime("%Y-%m-%d"),
            "bytes": len(payload),
        }
        # Write bin first, then meta: a present meta implies a present payload.
        bin_path.write_bytes(payload)
        meta_path.write_text(json.dumps(meta, default=str, indent=2), encoding="utf-8")

    def evict(self, source: str, key: str) -> None:
        """Delete both files for a key; missing files are ignored (Req 2.4)."""
        for path in self._paths(source, key):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
