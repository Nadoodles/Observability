"""
Alert Service  (port 8003)

Responsibilities:
  1. Poll PostgreSQL every POLL_INTERVAL seconds for the latest metrics window
     per service and evaluate SLA thresholds.
  2. Manage alert lifecycle: create new alerts (deduped), auto-resolve alerts
     when metrics return to normal.
  3. Serve the React dashboard's primary data API:
       GET /metrics/summary   — latest per-service status + active alerts
       GET /metrics/history   — last N windows per service (for charts)
       GET /alerts            — alert list with optional active-only filter

Architecture — alert deduplication:
  A service with a sustained latency spike should produce ONE alert, not one
  per poll cycle. We enforce this with a unique partial index on
  (service_id, alert_type) WHERE is_active=TRUE, plus a SELECT-before-INSERT
  guard in upsert_alert(). When the metric recovers, resolve_stale_alerts()
  closes any alert types that are no longer firing.

DISTINCT ON pattern:
  "Latest metrics window per service" is expressed as:
    SELECT DISTINCT ON (service_id) ... ORDER BY service_id, window_start DESC
  This is a PostgreSQL idiom for efficient first-row-per-group — cheaper than
  a subquery with MAX(window_start) + JOIN because the planner can use the
  (service_id, window_start DESC) index directly without a hash join.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import asyncpg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("alerts")

# ── Config ────────────────────────────────────────────────────────────────────
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://obs_user:obs_pass@localhost:5432/observability",
)
POLL_INTERVAL    = int(os.getenv("POLL_INTERVAL",    "30"))
LATENCY_WARN_MS  = float(os.getenv("LATENCY_WARN_MS",  "150"))
LATENCY_CRIT_MS  = float(os.getenv("LATENCY_CRIT_MS",  "300"))
ERROR_RATE_WARN  = float(os.getenv("ERROR_RATE_WARN",  "0.005"))  # 0.5%
ERROR_RATE_CRIT  = float(os.getenv("ERROR_RATE_CRIT",  "0.02"))   # 2.0%

# ── State ─────────────────────────────────────────────────────────────────────
db_pool: asyncpg.Pool | None = None
stats = {
    "poll_count":      0,
    "alerts_opened":   0,
    "alerts_resolved": 0,
    "started_at":      time.time(),
}


# ── Threshold evaluation ──────────────────────────────────────────────────────
def evaluate_thresholds(service_name: str, m: dict) -> list[dict]:
    """
    Return a list of threshold violations for a single service's latest window.

    Each violation dict contains the fields needed for an alerts table INSERT.
    Returns [] when the service is fully within SLA.
    """
    violations: list[dict] = []
    lat = m["avg_latency_ms"]
    err = m["error_rate"]

    # ── Latency ───────────────────────────────────────────────────────────────
    if lat > LATENCY_CRIT_MS:
        violations.append({
            "alert_type":      "HIGH_LATENCY",
            "severity":        "CRITICAL",
            "message":         (
                f"{service_name} avg latency {lat:.1f}ms "
                f"exceeds CRITICAL threshold {LATENCY_CRIT_MS:.0f}ms"
            ),
            "metric_value":    lat,
            "threshold_value": LATENCY_CRIT_MS,
        })
    elif lat > LATENCY_WARN_MS:
        violations.append({
            "alert_type":      "HIGH_LATENCY",
            "severity":        "WARNING",
            "message":         (
                f"{service_name} avg latency {lat:.1f}ms "
                f"exceeds WARNING threshold {LATENCY_WARN_MS:.0f}ms"
            ),
            "metric_value":    lat,
            "threshold_value": LATENCY_WARN_MS,
        })

    # ── Error rate ────────────────────────────────────────────────────────────
    if err > ERROR_RATE_CRIT:
        violations.append({
            "alert_type":      "HIGH_ERROR_RATE",
            "severity":        "CRITICAL",
            "message":         (
                f"{service_name} error rate {err*100:.2f}% "
                f"exceeds CRITICAL threshold {ERROR_RATE_CRIT*100:.1f}%"
            ),
            "metric_value":    err,
            "threshold_value": ERROR_RATE_CRIT,
        })
    elif err > ERROR_RATE_WARN:
        violations.append({
            "alert_type":      "HIGH_ERROR_RATE",
            "severity":        "WARNING",
            "message":         (
                f"{service_name} error rate {err*100:.2f}% "
                f"exceeds WARNING threshold {ERROR_RATE_WARN*100:.1f}%"
            ),
            "metric_value":    err,
            "threshold_value": ERROR_RATE_WARN,
        })

    return violations


# ── Alert lifecycle helpers ───────────────────────────────────────────────────
async def upsert_alert(conn: asyncpg.Connection, service_id: int, v: dict) -> None:
    """
    Insert a new alert only if no active alert of the same type exists.
    The SELECT-before-INSERT guard prevents duplicates even without a DB
    unique constraint (which would require a partial unique index upgrade).
    """
    existing = await conn.fetchrow(
        """
        SELECT id FROM alerts
        WHERE service_id = $1 AND alert_type = $2 AND is_active = TRUE
        LIMIT 1
        """,
        service_id, v["alert_type"],
    )
    if existing:
        return  # already firing — don't create a duplicate

    await conn.execute(
        """
        INSERT INTO alerts
            (service_id, alert_type, severity, message, metric_value, threshold_value)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        service_id,
        v["alert_type"],
        v["severity"],
        v["message"],
        v["metric_value"],
        v["threshold_value"],
    )
    stats["alerts_opened"] += 1
    logger.warning("ALERT [%s] %s", v["severity"], v["message"])


async def resolve_stale_alerts(
    conn: asyncpg.Connection,
    service_id: int,
    still_firing: set[str],
) -> None:
    """
    Close any active alerts for this service whose type is no longer in
    `still_firing`. This auto-resolves alerts when metrics return to normal.

    Two cases handled:
      - still_firing is non-empty: resolve only the types not currently firing.
      - still_firing is empty (service healthy): resolve ALL active alerts.
    """
    if still_firing:
        # Resolve only the types that have cleared
        resolved = await conn.execute(
            """
            UPDATE alerts
            SET    is_active = FALSE, resolved_at = NOW()
            WHERE  service_id = $1
              AND  is_active  = TRUE
              AND  alert_type != ALL($2::text[])
            """,
            service_id, list(still_firing),
        )
    else:
        resolved = await conn.execute(
            """
            UPDATE alerts
            SET    is_active = FALSE, resolved_at = NOW()
            WHERE  service_id = $1 AND is_active = TRUE
            """,
            service_id,
        )

    # asyncpg returns "UPDATE n" as the status string
    count = int(resolved.split()[-1])
    if count:
        stats["alerts_resolved"] += count
        logger.info("Resolved %d alert(s) for service_id=%d", count, service_id)


# ── Poll loop ─────────────────────────────────────────────────────────────────
async def poll_loop() -> None:
    logger.info("Alert poller started — interval=%ds", POLL_INTERVAL)
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)
            await evaluate_all_services()
        except asyncio.CancelledError:
            logger.info("Poller shutting down")
            break
        except Exception as exc:
            logger.error("Poll cycle error: %s", exc)


async def evaluate_all_services() -> None:
    """
    Fetch the most recent metric window per service, evaluate thresholds,
    and update alert state in a single DB transaction per call.
    """
    if not db_pool:
        return

    async with db_pool.acquire() as conn:
        # DISTINCT ON gives the latest window per service in one pass.
        # NULLS LAST handles services that have no metrics yet (no rows written).
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (s.id)
                s.id            AS service_id,
                s.name          AS service_name,
                m.avg_latency_ms,
                m.p95_latency_ms,
                m.p99_latency_ms,
                m.error_rate,
                m.throughput_rps,
                m.request_count,
                m.window_start,
                m.window_end
            FROM   services s
            LEFT JOIN metrics m ON m.service_id = s.id
            ORDER  BY s.id, m.window_start DESC NULLS LAST
            """
        )
        stats["poll_count"] += 1

        for row in rows:
            if row["avg_latency_ms"] is None:
                continue  # no data yet for this service

            m = dict(row)
            violations   = evaluate_thresholds(m["service_name"], m)
            still_firing = {v["alert_type"] for v in violations}

            for v in violations:
                await upsert_alert(conn, m["service_id"], v)

            await resolve_stale_alerts(conn, m["service_id"], still_firing)


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    logger.info("Alert service starting up")

    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    logger.info("PostgreSQL connection pool established")

    poll_task = asyncio.create_task(poll_loop())

    yield

    logger.info("Alert service shutting down")
    poll_task.cancel()
    await asyncio.gather(poll_task, return_exceptions=True)
    await db_pool.close()


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Alert Service", version="1.0.0", lifespan=lifespan)

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


@app.get("/alerts")
async def list_alerts(active_only: bool = True):
    """
    Return the alert list. Set ?active_only=false to include resolved alerts.
    """
    async with db_pool.acquire() as conn:
        q = """
            SELECT a.id, s.name AS service_name, a.alert_type, a.severity,
                   a.message, a.metric_value, a.threshold_value,
                   a.is_active, a.triggered_at, a.resolved_at
            FROM   alerts a
            JOIN   services s ON s.id = a.service_id
        """
        if active_only:
            q += " WHERE a.is_active = TRUE"
        q += " ORDER BY a.triggered_at DESC LIMIT 200"

        rows = await conn.fetch(q)

    return [
        {**dict(r),
         "triggered_at": r["triggered_at"].isoformat() if r["triggered_at"] else None,
         "resolved_at":  r["resolved_at"].isoformat()  if r["resolved_at"]  else None}
        for r in rows
    ]


@app.get("/metrics/summary")
async def metrics_summary():
    """
    Primary dashboard endpoint — polled by the React frontend every 5 seconds.

    Returns per-service status (healthy / warning / critical / no_data),
    the latest metric values, active alert details, and a total alert count.
    """
    async with db_pool.acquire() as conn:
        metric_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (s.id)
                s.name          AS service_name,
                m.avg_latency_ms,
                m.p95_latency_ms,
                m.p99_latency_ms,
                m.error_rate,
                m.throughput_rps,
                m.request_count,
                m.window_start,
                m.window_end
            FROM   services s
            LEFT JOIN metrics m ON m.service_id = s.id
            ORDER  BY s.id, m.window_start DESC NULLS LAST
            """
        )
        alert_rows = await conn.fetch(
            """
            SELECT s.name AS service_name,
                   a.alert_type, a.severity, a.message,
                   a.metric_value, a.triggered_at
            FROM   alerts a
            JOIN   services s ON s.id = a.service_id
            WHERE  a.is_active = TRUE
            ORDER  BY a.triggered_at DESC
            """
        )

    # Build an index of active alerts keyed by service name
    alerts_by_service: dict[str, list[dict]] = {}
    for a in alert_rows:
        alerts_by_service.setdefault(a["service_name"], []).append({
            "alert_type":   a["alert_type"],
            "severity":     a["severity"],
            "message":      a["message"],
            "metric_value": a["metric_value"],
            "triggered_at": a["triggered_at"].isoformat() if a["triggered_at"] else None,
        })

    services_out: dict[str, dict] = {}
    for row in metric_rows:
        svc           = row["service_name"]
        active_alerts = alerts_by_service.get(svc, [])
        has_data      = row["avg_latency_ms"] is not None

        if not has_data:
            status = "no_data"
        elif any(a["severity"] == "CRITICAL" for a in active_alerts):
            status = "critical"
        elif any(a["severity"] == "WARNING"  for a in active_alerts):
            status = "warning"
        else:
            status = "healthy"

        services_out[svc] = {
            "status": status,
            "metrics": {
                "avg_latency_ms": row["avg_latency_ms"],
                "p95_latency_ms": row["p95_latency_ms"],
                "p99_latency_ms": row["p99_latency_ms"],
                "error_rate":     row["error_rate"],
                "throughput_rps": row["throughput_rps"],
                "request_count":  row["request_count"],
                "window_start":   row["window_start"].isoformat() if row["window_start"] else None,
                "window_end":     row["window_end"].isoformat()   if row["window_end"]   else None,
            } if has_data else None,
            "active_alerts": active_alerts,
        }

    return {
        "timestamp":           datetime.now(timezone.utc).isoformat(),
        "services":            services_out,
        "total_active_alerts": len(alert_rows),
    }


@app.get("/metrics/history")
async def metrics_history(limit: int = 30):
    """
    Return the last `limit` aggregated windows per service in ascending time
    order — used to populate the time-series charts in the React dashboard.

    We fetch `limit * 3` rows total (3 services), then group and trim.
    """
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.name AS service_name,
                   m.avg_latency_ms, m.p95_latency_ms, m.p99_latency_ms,
                   m.error_rate, m.throughput_rps, m.request_count,
                   m.window_start, m.window_end
            FROM   metrics m
            JOIN   services s ON s.id = m.service_id
            ORDER  BY m.window_start DESC
            LIMIT  $1
            """,
            limit * 3,
        )

    history: dict[str, list[dict]] = {}
    for row in rows:
        svc = row["service_name"]
        history.setdefault(svc, []).append({
            "avg_latency_ms": row["avg_latency_ms"],
            "p95_latency_ms": row["p95_latency_ms"],
            "p99_latency_ms": row["p99_latency_ms"],
            "error_rate":     row["error_rate"],
            "throughput_rps": row["throughput_rps"],
            "request_count":  row["request_count"],
            "window_start":   row["window_start"].isoformat(),
            "window_end":     row["window_end"].isoformat(),
            # Derived display label: window midpoint time "HH:MM:SS"
            "label":          row["window_start"].strftime("%H:%M:%S"),
        })

    # Rows arrived newest-first; reverse each list for ascending chart order
    for svc in history:
        history[svc] = list(reversed(history[svc][:limit]))

    return history


@app.get("/status")
async def status():
    return {
        "poll_count":      stats["poll_count"],
        "alerts_opened":   stats["alerts_opened"],
        "alerts_resolved": stats["alerts_resolved"],
        "uptime_seconds":  round(time.time() - stats["started_at"], 1),
    }
