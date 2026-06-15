"""Redis-backed cache for Parquet row-group metadata + file listings.

Planning re-reads footers to get min/max stats; on a bucket with thousands of
files that adds up. We cache per-file metadata keyed by (path, size) so warm
queries skip the footer reads entirely. The engine never imports this — it
accepts a `metadata_fn` and the Django layer injects this cached version.
"""

from __future__ import annotations

import pickle

from django.conf import settings

from apps.engine.parquet_reader import (
    ParquetFileInfo,
    read_row_group_metadata,
)

_redis = None


def _client():
    global _redis
    if _redis is not None:
        return _redis
    url = getattr(settings, "STATS_CACHE_URL", "")
    if not url:
        return None
    try:
        import redis

        _redis = redis.Redis.from_url(url)
        _redis.ping()
    except Exception:
        _redis = None
    return _redis


def cached_row_group_metadata(fs, file: ParquetFileInfo):
    client = _client()
    if client is None:
        return read_row_group_metadata(fs, file)

    key = f"rgmeta:{file.path}:{file.size}"
    blob = client.get(key)
    if blob is not None:
        try:
            return pickle.loads(blob)
        except Exception:
            pass
    meta = read_row_group_metadata(fs, file)
    try:
        client.set(key, pickle.dumps(meta), ex=3600)
    except Exception:
        pass
    return meta
