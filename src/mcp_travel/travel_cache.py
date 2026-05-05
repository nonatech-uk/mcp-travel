"""Postgres-backed query cache for mcp-travel tools.

Schema: travel.query_cache(tool, args_hash, date_bucket, result jsonb,
ttl_seconds, created_at, accessed_at). Keyed on the tool name plus a
sha256 of the canonical-JSON kwargs, plus the depart-date bucket so that
the same query for two different dates doesn't collide.
"""

import hashlib
import json
from datetime import date

import asyncpg


def args_hash(args: dict) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def cache_get(
    pool: asyncpg.Pool, tool: str, args: dict, date_bucket: date
) -> dict | None:
    h = args_hash(args)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT result, created_at, ttl_seconds
              FROM query_cache
             WHERE tool = $1 AND args_hash = $2 AND date_bucket = $3
               AND created_at + (ttl_seconds || ' seconds')::interval > now()
            """,
            tool,
            h,
            date_bucket,
        )
        if not row:
            return None
        await conn.execute(
            "UPDATE query_cache SET accessed_at = now() "
            "WHERE tool = $1 AND args_hash = $2 AND date_bucket = $3",
            tool,
            h,
            date_bucket,
        )
        return json.loads(row["result"])


async def cache_set(
    pool: asyncpg.Pool,
    tool: str,
    args: dict,
    date_bucket: date,
    result: dict,
    ttl_seconds: int,
) -> None:
    h = args_hash(args)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO query_cache (tool, args_hash, date_bucket, result, ttl_seconds)
            VALUES ($1, $2, $3, $4::jsonb, $5)
            ON CONFLICT (tool, args_hash, date_bucket) DO UPDATE
              SET result = EXCLUDED.result,
                  ttl_seconds = EXCLUDED.ttl_seconds,
                  created_at = now(),
                  accessed_at = now()
            """,
            tool,
            h,
            date_bucket,
            json.dumps(result),
            ttl_seconds,
        )
