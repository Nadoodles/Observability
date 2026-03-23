-- ── Observability Platform — PostgreSQL Schema ───────────────────────────────
--
-- Design notes:
--   - metrics is a time-series append-only table. We never UPDATE rows;
--     each flush from the processing service adds new window rows.
--   - DISTINCT ON (service_id) ORDER BY window_start DESC is the hot query
--     pattern — the compound index on (service_id, window_start DESC) covers it.
--   - alerts uses a deduplication pattern: one active row per (service, type).
--     The alert service auto-resolves rows by setting is_active=FALSE.
--   - Double-precision floats are used throughout instead of NUMERIC to avoid
--     the overhead of exact decimal arithmetic for monitoring data.
-- ─────────────────────────────────────────────────────────────────────────────


-- ── Service registry ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS services (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(100) UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Pre-seed the three simulated ad-platform services.
-- ON CONFLICT DO NOTHING makes this idempotent on restart.
INSERT INTO services (name)
VALUES ('ad-serving'), ('bidding-engine'), ('click-tracker')
ON CONFLICT (name) DO NOTHING;


-- ── Time-series metrics ───────────────────────────────────────────────────────
-- Each row is one aggregated window (default: 30 seconds) for one service.
CREATE TABLE IF NOT EXISTS metrics (
    id              BIGSERIAL PRIMARY KEY,
    service_id      INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    avg_latency_ms  DOUBLE PRECISION NOT NULL,
    p95_latency_ms  DOUBLE PRECISION NOT NULL,
    p99_latency_ms  DOUBLE PRECISION NOT NULL,
    error_rate      DOUBLE PRECISION NOT NULL,  -- 0.0 .. 1.0
    throughput_rps  DOUBLE PRECISION NOT NULL,
    request_count   INTEGER NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Hot query: "latest window per service" — used by DISTINCT ON pattern
CREATE INDEX IF NOT EXISTS idx_metrics_service_window
    ON metrics (service_id, window_start DESC);

-- Hot query: dashboard history — service + recency range scan
CREATE INDEX IF NOT EXISTS idx_metrics_window_start
    ON metrics (window_start DESC);


-- ── Alerts ────────────────────────────────────────────────────────────────────
-- One active row maximum per (service_id, alert_type).
-- Resolved alerts are kept for history (is_active=FALSE, resolved_at set).
CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    service_id      INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,

    -- HIGH_LATENCY | HIGH_ERROR_RATE  (extensible)
    alert_type      VARCHAR(50) NOT NULL,

    -- WARNING | CRITICAL
    severity        VARCHAR(20) NOT NULL,

    message         TEXT NOT NULL,
    metric_value    DOUBLE PRECISION NOT NULL,
    threshold_value DOUBLE PRECISION NOT NULL,

    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    triggered_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ
);

-- Lookup: "all active alerts for a service"
CREATE INDEX IF NOT EXISTS idx_alerts_service_active
    ON alerts (service_id, is_active, triggered_at DESC);

-- Lookup: deduplication check — "is this alert type already firing?"
CREATE INDEX IF NOT EXISTS idx_alerts_dedup
    ON alerts (service_id, alert_type, is_active)
    WHERE is_active = TRUE;
