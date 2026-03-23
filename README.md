# Distributed Microservices Observability Platform

Real-time SLA monitoring across a simulated ads platform. Three FastAPI microservices communicate via Redis pub/sub, aggregate time-series metrics into PostgreSQL, and surface live health data through a React dashboard with auto-alerting.

Built to demonstrate production-grade observability infrastructure at scale — the kind of tooling that underlies ads platform reliability at companies like TikTok, Meta, and Google.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        React Dashboard                          │
│              (GitHub Pages · polls every 5s)                   │
└──────────────────────────┬──────────────────────────────────────┘
                           │ GET /metrics/summary
                           │ GET /metrics/history
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│               Service 3 · Alert Service  :8003                  │
│   Polls PostgreSQL · evaluates SLA thresholds · alert CRUD      │
└──────────────────────────┬───────────────────────────────────────┘
                           │ reads
                           ▼
                    ┌─────────────┐
                    │  PostgreSQL │  time-series metrics + alerts
                    └──────┬──────┘
                           │ writes
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│             Service 2 · Processing Service  :8002               │
│  Redis subscriber · 30s aggregation windows · p95/p99 latency   │
└──────────────────────────┬───────────────────────────────────────┘
                           │ subscribes
                           ▼
                    ┌─────────────┐
                    │    Redis    │  pub/sub channel: ad_events
                    └──────┬──────┘
                           │ publishes
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│             Service 1 · Ingestion Service  :8001                │
│     Accepts ad click events · built-in traffic simulator        │
└──────────────────────────────────────────────────────────────────┘
```

**Simulated services:** `ad-serving` · `bidding-engine` · `click-tracker`

---

## Tech Stack

| Layer | Technology |
|---|---|
| Microservices | Python 3.11 · FastAPI · uvicorn |
| Message broker | Redis 7 pub/sub |
| Database | PostgreSQL 15 (time-series schema) |
| Async DB driver | asyncpg |
| Orchestration | Docker Compose |
| Frontend | React 18 · Vite · Recharts |
| Load testing | Python asyncio · httpx |

---

## Features

- **Traffic simulator** — ingestion service auto-generates realistic ad click events at a configurable rate (log-normal latency distribution, 2% baseline error rate)
- **Streaming aggregation** — double-buffer pattern in the processing service aggregates events into 30-second windows without blocking the Redis subscriber
- **p95 / p99 latency** — percentile computation without NumPy using linear interpolation
- **Alert lifecycle** — alerts are deduplicated (no storm), auto-resolved when metrics recover
- **DISTINCT ON queries** — efficient "latest window per service" queries using PostgreSQL's `DISTINCT ON` with a compound index
- **Demo mode** — frontend falls back to synthetic data when the backend is offline, so the GitHub Pages deployment is always interactive

---

## Quick Start

### Prerequisites
- Docker + Docker Compose
- Node.js 18+ (frontend only)

### Run locally

```bash
# Clone and start the full stack
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>

docker compose up --build
```

Services start in dependency order (Redis and Postgres health-checked before app services). The ingestion service begins simulating traffic immediately.

| Service | URL |
|---|---|
| Ingestion | http://localhost:8001 |
| Processing | http://localhost:8012 |
| Alerts / Dashboard API | http://localhost:8003 |

### Run the frontend (dev)

```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

### Tear down

```bash
docker compose down        # stop + remove containers, keep DB volume
docker compose down -v     # also wipe the postgres_data volume
```

---

## Load Testing

```bash
pip install httpx
python load_test/load_test.py --duration 60 --concurrency 30
```

Sample output:
```
────────────────────────────────────────────────────────────────────────────────────────────
  LOAD TEST RESULTS  —  60s  total=42,318  errors=104  err_rate=0.25%  rps=705.3
────────────────────────────────────────────────────────────────────────────────────────────
  ENDPOINT                                N       ERR%     p50      p95      p99      MAX
────────────────────────────────────────────────────────────────────────────────────────────
  GET  /health (alerts)               6,044    0.0%     2.1ms    4.8ms    7.2ms   18.3ms
  GET  /health (ingestion)            6,102    0.0%     1.9ms    4.1ms    6.8ms   15.1ms
  GET  /health (processing)           6,018    0.0%     2.0ms    4.5ms    7.0ms   16.2ms
  GET  /metrics/history               6,031    0.5%     8.4ms   18.2ms   31.5ms   89.4ms
  GET  /metrics/summary               6,089    0.4%     7.1ms   15.9ms   28.3ms   74.2ms
  GET  /status (processing)           6,011    0.0%     2.3ms    5.1ms    8.4ms   22.1ms
  POST /ingest/events                 6,023    0.8%     3.2ms    7.4ms   12.1ms   41.8ms
```

---

## API Reference

### Ingestion Service `:8001`

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness + Redis connectivity |
| `GET` | `/status` | Events published, simulator stats |
| `POST` | `/ingest/events` | Accept event batch (202 Accepted) |

### Processing Service `:8012`

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness + DB connectivity |
| `GET` | `/status` | Buffer state, flush count |

### Alert Service `:8003`

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness + DB connectivity |
| `GET` | `/metrics/summary` | Latest metrics + active alerts per service |
| `GET` | `/metrics/history?limit=30` | Last N windows per service (for charts) |
| `GET` | `/alerts?active_only=true` | Alert list |

---

## Project Structure

```
.
├── docker-compose.yml
├── .env.example
├── db/
│   └── init.sql                  # schema, indexes, service seeding
├── services/
│   ├── ingestion/
│   │   ├── main.py               # simulator + Redis PUBLISH
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── processing/
│   │   ├── main.py               # double-buffer aggregation + Postgres flush
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   └── alerts/
│       ├── main.py               # SLA poller + alert lifecycle + dashboard API
│       ├── requirements.txt
│       └── Dockerfile
├── frontend/
│   ├── src/
│   │   ├── App.jsx               # polling logic, demo mode fallback
│   │   ├── demo.js               # synthetic data generator for GitHub Pages
│   │   └── components/
│   │       ├── ServiceCard.jsx
│   │       ├── MetricsChart.jsx
│   │       └── AlertBanner.jsx
│   ├── package.json
│   └── vite.config.js
└── load_test/
    └── load_test.py              # asyncio + httpx, p50/p95/p99 per endpoint
```

---

## Deploying the Frontend to GitHub Pages

1. Set your repo homepage in `frontend/package.json`:
```json
"homepage": "https://<your-username>.github.io/<repo-name>"
```

2. Deploy:
```bash
cd frontend
npm run deploy
```

The frontend shows **DEMO MODE** with synthetic live-looking data when the backend is offline — no blank page for GitHub Pages visitors.

To point it at a live backend, set the `VITE_ALERTS_URL` environment variable before building:
```bash
VITE_ALERTS_URL=https://your-alerts-service.onrender.com npm run build
```
