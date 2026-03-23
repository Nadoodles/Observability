"""
Metrics Processing Service  (port 8002)

Responsibilities:
  1. Subscribe to the Redis 'ad_events' pub/sub channel and buffer incoming
     events in memory, keyed by service name.
  2. Every FLUSH_INTERVAL seconds, aggregate the buffered events into
     per-service metric windows (avg/p95/p99 latency, error rate, throughput)
     and write them to PostgreSQL.

Architecture — double-buffer pattern:
  The subscriber task continuously appends events to `buffer` (dict: service ->
  list[event]). The flusher task wakes every FLUSH_INTERVAL seconds, atomically
  swaps the buffer for a fresh empty dict under a lock, then processes the
  snapshot. The subscriber is never blocked during the (potentially slow)
  database write — the lock is held only for the microsecond swap, not the
  full flush.

Percentile computation:
  We avoid numpy/scipy to keep the image small. stdlib-only linear interpolation
  is accurate enough for monitoring (vs. scientific computing) and runs in
  O(n log n) on each flush, which is fine at our event volumes.

Database:
  asyncpg is used directly (no ORM) for maximum throughput. The flush uses
  executemany so all service rows hit Postgres in a single round-trip.
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("processing")

# ── Config ────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_CHANNEL = os.getenv("REDIS_CHANNEL", "ad_events")
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://obs_user:obs_pass@localhost:5432/observability",
)
FLUSH_INTERVAL = int(os.getenv("FLUSH_INTERVAL", "30"))

# ── Shared state ──────────────────────────────────────────────────────────────
# buffer is the live accumulator; accessed by both subscriber and flusher tasks.
buffer: dict[str, list[dict]] = defaultdict(list)
buffer_lock = asyncio.Lock()

db_pool: asyncpg.Pool | None = None

stats = {
    "events_received": 0,
    "events_dropped":  0,
    "flush_count":     0,
    "flush_errors":    0,
    "started_at":      time.time(),
}


# ── Percentile helper ─────────────────────────────────────────────────────────
def percentile(sorted_data: list[float], p: float) -> float:
    """
    Linear interpolation percentile on a pre-sorted list. O(1) after sort.

    Example: percentile([1,2,3,4,5], 95) ≈ 4.8
    Edge cases: empty list → 0.0, single element → that element.
    """
    n = len(sorted_data)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_data[0]
    idx = (n - 1) * p / 100.0
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_data[lo] * (1.0 - frac) + sorted_data[hi] * frac


# ── Aggregation ───────────────────────────────────────────────────────────────
def aggregate_window(
    events: list[dict],
    window_start: datetime,
    window_end: datetime,
) -> dict:
    """
    Reduce a raw event list for one service into a single metrics row.

    Throughput is computed from window duration, not a running counter, so it
    accurately reflects RPS even when the buffer is partially filled (e.g.,
    on the first flush after startup).
    """
    latencies = sorted(e["latency_ms"] for e in events)
    error_count = sum(1 for e in events if e.get("error", False))
    n = len(events)
    duration_s = (window_end - window_start).total_seconds()

    return {
        "avg_latency_ms": sum(latencies) / n,
        "p95_latency_ms": percentile(latencies, 95),
        "p99_latency_ms": percentile(latencies, 99),
        "error_rate":     error_count / n,          # 0.0 .. 1.0
        "throughput_rps": n / duration_s if duration_s > 0 else 0.0,
        "request_count":  n,
        "window_start":   window_start,
        "window_end":     window_end,
    }


# ── Database flush ────────────────────────────────────────────────────────────
async def flush_to_db(
    snapshot: dict[str, list[dict]],
    window_start: datetime,
    window_end: datetime,
) -> None:
    """
    Aggregate the snapshot and write one row per service to PostgreSQL.

    Uses executemany so all inserts are sent in a single round-trip.
    The INSERT uses a subselect to resolve service_name → service_id,
    which is cleaner than pre-loading the ID map and avoids a race if a
    new service name ever appears.
    """
    if not db_pool:
        logger.warning("DB pool not ready — skipping flush")
        return

    rows: list[tuple] = []
    for service_name, events in snapshot.items():
        if not events:
            continue
        agg = aggregate_window(events, window_start, window_end)
        rows.append((
            service_name,
            agg["window_start"],
            agg["window_end"],
            agg["avg_latency_ms"],
            agg["p95_latency_ms"],
            agg["p99_latency_ms"],
            agg["error_rate"],
            agg["throughput_rps"],
            agg["request_count"],
        ))
        logger.info(
            "  %-20s n=%-5d avg_lat=%-7.1fms p95=%-7.1fms err=%.2f%%  rps=%.1f",
            service_name,
            agg["request_count"],
            agg["avg_latency_ms"],
            agg["p95_latency_ms"],
            agg["error_rate"] * 100,
            agg["throughput_rps"],
        )

    if not rows:
        return

    async with db_pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO metrics
                (service_id, window_start, window_end,
                 avg_latency_ms, p95_latency_ms, p99_latency_ms,
                 error_rate, throughput_rps, request_count)
            SELECT s.id, $2, $3, $4, $5, $6, $7, $8, $9
            FROM   services s
            WHERE  s.name = $1
            """,
            rows,
        )

    stats["flush_count"] += 1
    logger.info("Flushed %d service(s) → PostgreSQL", len(rows))


# ── Background tasks ──────────────────────────────────────────────────────────
async def subscriber_task() -> None:
    """
    Long-running task: subscribe to Redis and append events to the buffer.

    On connection loss, backs off 5 seconds and reconnects rather than
    propagating an exception to the event loop. A new Redis client is
    created on each reconnect attempt to avoid using a stale connection.
    """
    while True:
        try:
            r = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
            pubsub = r.pubsub()
            await pubsub.subscribe(REDIS_CHANNEL)
            logger.info("Subscribed to Redis channel: %s", REDIS_CHANNEL)

            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    event = json.loads(message["data"])
                    async with buffer_lock:
                        buffer[event["service_name"]].append(event)
                    stats["events_received"] += 1
                except (json.JSONDecodeError, KeyError) as exc:
                    stats["events_dropped"] += 1
                    logger.warning("Malformed event dropped: %s", exc)

        except asyncio.CancelledError:
            logger.info("Subscriber task shutting down")
            break
        except Exception as exc:
            logger.error("Redis subscriber error: %s — reconnecting in 5s", exc)
            await asyncio.sleep(5)


async def flusher_task() -> None:
    """
    Background task: every FLUSH_INTERVAL seconds, atomically swap the buffer
    and flush the captured snapshot to PostgreSQL.

    The atomic swap (under lock) is the key correctness property: events that
    arrive during the DB write go into the NEW buffer and will be included in
    the next flush, with no possibility of them being lost or double-counted.
    """
    logger.info("Flusher started — window=%ds", FLUSH_INTERVAL)

    while True:
        try:
            await asyncio.sleep(FLUSH_INTERVAL)

            window_end = datetime.now(timezone.utc)
            window_start = datetime.fromtimestamp(
                window_end.timestamp() - FLUSH_INTERVAL, tz=timezone.utc
            )

            # Atomic swap: subscribers write to the new empty buffer immediately
            async with buffer_lock:
                snapshot = dict(buffer)
                buffer.clear()

            total = sum(len(v) for v in snapshot.values())
            logger.info(
                "Flush: %d events across %d service(s) [%s → %s]",
                total, len(snapshot),
                window_start.strftime("%H:%M:%S"),
                window_end.strftime("%H:%M:%S"),
            )

            if total > 0:
                try:
                    await flush_to_db(snapshot, window_start, window_end)
                except Exception as exc:
                    stats["flush_errors"] += 1
                    logger.error("DB flush failed: %s", exc)

        except asyncio.CancelledError:
            logger.info("Flusher task shutting down")
            break


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    logger.info("Processing service starting up")

    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    logger.info("PostgreSQL connection pool established")

    sub  = asyncio.create_task(subscriber_task())
    flush = asyncio.create_task(flusher_task())

    yield

    logger.info("Processing service shutting down")
    sub.cancel()
    flush.cancel()
    await asyncio.gather(sub, flush, return_exceptions=True)
    await db_pool.close()


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Metrics Processing Service", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    db_ok = False
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.execute("SELECT 1")
            db_ok = True
        except Exception:
            pass

    return {
        "status":         "healthy" if db_ok else "degraded",
        "database":       "connected" if db_ok else "disconnected",
        "uptime_seconds": round(time.time() - stats["started_at"], 1),
    }


@app.get("/status")
async def status():
    """Live buffer snapshot + operational counters. Useful for debugging."""
    async with buffer_lock:
        buffer_state = {svc: len(evts) for svc, evts in buffer.items()}

    return {
        "events_received":  stats["events_received"],
        "events_dropped":   stats["events_dropped"],
        "flush_count":      stats["flush_count"],
        "flush_errors":     stats["flush_errors"],
        "buffer_pending":   buffer_state,
        "flush_interval_s": FLUSH_INTERVAL,
        "uptime_seconds":   round(time.time() - stats["started_at"], 1),
    }
