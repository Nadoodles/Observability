"""
Data Ingestion Service  (port 8001)

Responsibilities:
  1. Accept batches of ad click events via POST /ingest/events and publish
     each event to the Redis 'ad_events' pub/sub channel.
  2. Run a built-in traffic simulator that auto-generates realistic fake events
     so the platform is self-driving — no external load generator needed.

Architecture note on the simulator:
  The simulator runs as a single asyncio background task. It generates events
  at SIMULATOR_RATE events/sec using a simple token-bucket timing loop.
  Latency values follow a log-normal distribution (median ~80ms, long tail),
  which closely mirrors real web service tail-latency profiles.
  Error injection is ~2% baseline, keeping the alert service mostly quiet
  during normal operation but exercising the full alert pipeline.

Redis publishing:
  We use a single long-lived async Redis connection (not a pool) because
  PUBLISH is a fire-and-forget command with no reads — one connection is
  enough and avoids the overhead of connection acquisition on every event.
"""

import asyncio
import json
import logging
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ingestion")

# ── Config ────────────────────────────────────────────────────────────────────
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_CHANNEL = os.getenv("REDIS_CHANNEL", "ad_events")
SIMULATOR_RATE = float(os.getenv("SIMULATOR_RATE", "20"))  # events/sec
SIMULATE = os.getenv("SIMULATE", "true").lower() == "true"

# Simulated microservices in the ads platform
AD_SERVICES = ["ad-serving", "bidding-engine", "click-tracker"]
ENDPOINTS: dict[str, list[str]] = {
    "ad-serving":     ["/serve", "/render", "/track"],
    "bidding-engine": ["/bid", "/auction", "/win-notify"],
    "click-tracker":  ["/click", "/conversion", "/impression"],
}

# ── Module-level state ────────────────────────────────────────────────────────
_redis: aioredis.Redis | None = None
stats = {"events_published": 0, "publish_errors": 0, "started_at": time.time()}


# ── Pydantic models ───────────────────────────────────────────────────────────
class AdEvent(BaseModel):
    service_name: str
    endpoint: str
    latency_ms: float
    status_code: int
    error: bool = False


class EventBatch(BaseModel):
    events: list[AdEvent]


# ── Redis helpers ─────────────────────────────────────────────────────────────
async def get_redis() -> aioredis.Redis:
    global _redis
    if _redis is None:
        _redis = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=True
        )
    return _redis


async def publish(event: dict) -> None:
    r = await get_redis()
    await r.publish(REDIS_CHANNEL, json.dumps(event))
    stats["events_published"] += 1


# ── Event generator ───────────────────────────────────────────────────────────
def make_event(service_name: str | None = None) -> dict:
    """
    Generate a single realistic fake ad click event.

    Latency: log-normal distribution, median ≈ 80ms, long tail to ~2s.
    Errors:  ~2% baseline rate with uniformly random HTTP 5xx codes.
    """
    svc = service_name or random.choice(AD_SERVICES)
    endpoint = random.choice(ENDPOINTS[svc])

    # lognormvariate(mu, sigma): mu=ln(80)≈4.38, sigma=0.6 gives realistic tail
    latency_ms = round(min(random.lognormvariate(4.38, 0.6), 2000.0), 2)
    is_error = random.random() < 0.02
    status_code = random.choice([500, 502, 503]) if is_error else 200

    return {
        "event_id":    str(uuid.uuid4()),
        "service_name": svc,
        "endpoint":    endpoint,
        "latency_ms":  latency_ms,
        "status_code": status_code,
        "error":       is_error,
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }


# ── Traffic simulator ─────────────────────────────────────────────────────────
async def run_simulator() -> None:
    """
    Background task: publish fake events at ~SIMULATOR_RATE events/sec.

    Uses a fixed sleep interval between publishes rather than a token bucket,
    which is accurate enough at the rates we care about (<= 100 events/sec).
    On Redis errors, backs off 1 second and retries rather than crashing.
    """
    interval = 1.0 / SIMULATOR_RATE
    logger.info(f"Simulator started — {SIMULATOR_RATE} events/sec on '{REDIS_CHANNEL}'")

    while True:
        try:
            await publish(make_event())
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("Simulator task cancelled, shutting down cleanly")
            break
        except Exception as exc:
            stats["publish_errors"] += 1
            logger.error(f"Simulator publish failed: {exc}")
            await asyncio.sleep(1.0)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Ingestion service starting up")
    await get_redis()  # warm up the connection before accepting traffic
    logger.info(f"Connected to Redis at {REDIS_URL}")

    sim_task: asyncio.Task | None = None
    if SIMULATE:
        sim_task = asyncio.create_task(run_simulator())

    yield

    logger.info("Ingestion service shutting down")
    if sim_task:
        sim_task.cancel()
        await asyncio.gather(sim_task, return_exceptions=True)
    if _redis:
        await _redis.aclose()


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Ad Event Ingestion Service", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    """Liveness + Redis connectivity check. Used by Docker healthcheck."""
    redis_ok = False
    try:
        r = await get_redis()
        await r.ping()
        redis_ok = True
    except Exception:
        pass

    return {
        "status":         "healthy" if redis_ok else "degraded",
        "redis":          "connected" if redis_ok else "disconnected",
        "uptime_seconds": round(time.time() - stats["started_at"], 1),
    }


@app.get("/status")
async def status():
    """Operational counters — useful for debugging simulator throughput."""
    return {
        "events_published":  stats["events_published"],
        "publish_errors":    stats["publish_errors"],
        "simulator_enabled": SIMULATE,
        "simulator_rate":    SIMULATOR_RATE,
        "redis_channel":     REDIS_CHANNEL,
        "uptime_seconds":    round(time.time() - stats["started_at"], 1),
    }


@app.post("/ingest/events", status_code=202)
async def ingest_events(batch: EventBatch):
    """
    Accept a batch of ad events from external producers.

    Returns 202 Accepted immediately — we fire-and-forget to Redis without
    waiting for downstream acknowledgement. Callers should treat this as
    best-effort delivery (at-most-once semantics with Redis pub/sub).
    """
    if not batch.events:
        raise HTTPException(status_code=400, detail="Event batch cannot be empty")

    published = 0
    for ev in batch.events:
        payload = {
            "event_id":    str(uuid.uuid4()),
            "service_name": ev.service_name,
            "endpoint":    ev.endpoint,
            "latency_ms":  ev.latency_ms,
            "status_code": ev.status_code,
            "error":       ev.error,
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        }
        await publish(payload)
        published += 1

    return {"accepted": published}
