"""
Load Testing Script — Distributed Observability Platform

Hammers all 3 services concurrently with configurable concurrency and duration.
Reports per-endpoint latency percentiles, error rates, and throughput.

Usage:
    pip install httpx
    python load_test.py                          # defaults
    python load_test.py --duration 60 --concurrency 50
    python load_test.py --host http://myhost.com --duration 30

Architecture:
    Each "worker" is an asyncio task that loops continuously, picking a random
    target endpoint, firing a request, and recording the result. A shared
    asyncio.Semaphore caps the number of in-flight requests to `concurrency`.
    Progress is printed every 5 seconds. Final stats are computed after the
    workers are cancelled at `duration` seconds.
"""

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

# ── Endpoint definitions ───────────────────────────────────────────────────────
def build_targets(host: str) -> list[dict]:
    """
    Returns the list of endpoints to hammer. Each entry has:
      - url:     full request URL
      - method:  HTTP method
      - payload: callable() → dict (re-called per request for freshness)
      - name:    short label used in the report
    """
    ingestion  = f"{host}:8001"
    processing = f"{host}:8012"
    alerts     = f"{host}:8003"

    ad_services = ["ad-serving", "bidding-engine", "click-tracker"]
    endpoints_map = {
        "ad-serving":     ["/serve", "/render", "/track"],
        "bidding-engine": ["/bid", "/auction", "/win-notify"],
        "click-tracker":  ["/click", "/conversion", "/impression"],
    }

    def random_event_batch():
        svc      = random.choice(ad_services)
        endpoint = random.choice(endpoints_map[svc])
        return {
            "events": [
                {
                    "service_name": svc,
                    "endpoint":     endpoint,
                    "latency_ms":   round(random.lognormvariate(4.38, 0.6), 2),
                    "status_code":  random.choice([500, 502]) if random.random() < 0.02 else 200,
                    "error":        random.random() < 0.02,
                }
                for _ in range(random.randint(1, 10))
            ]
        }

    return [
        # Ingestion — primary write path
        {
            "name":    "POST /ingest/events",
            "url":     f"{ingestion}/ingest/events",
            "method":  "POST",
            "payload": random_event_batch,
        },
        # Health checks — lightweight read path
        {
            "name":    "GET  /health (ingestion)",
            "url":     f"{ingestion}/health",
            "method":  "GET",
            "payload": None,
        },
        {
            "name":    "GET  /health (processing)",
            "url":     f"{processing}/health",
            "method":  "GET",
            "payload": None,
        },
        {
            "name":    "GET  /health (alerts)",
            "url":     f"{alerts}/health",
            "method":  "GET",
            "payload": None,
        },
        # Dashboard API — read-heavy, polled by the frontend
        {
            "name":    "GET  /metrics/summary",
            "url":     f"{alerts}/metrics/summary",
            "method":  "GET",
            "payload": None,
        },
        {
            "name":    "GET  /metrics/history",
            "url":     f"{alerts}/metrics/history?limit=20",
            "method":  "GET",
            "payload": None,
        },
        # Processing status
        {
            "name":    "GET  /status (processing)",
            "url":     f"{processing}/status",
            "method":  "GET",
            "payload": None,
        },
    ]


# ── Result accumulation ───────────────────────────────────────────────────────
@dataclass
class EndpointResult:
    name:        str
    latencies:   list[float] = field(default_factory=list)
    error_count: int = 0
    status_codes: dict[int, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.latencies) + self.error_count

    @property
    def success_count(self) -> int:
        return len(self.latencies)


def percentile(sorted_data: list[float], p: float) -> float:
    n = len(sorted_data)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_data[0]
    idx = (n - 1) * p / 100.0
    lo  = int(idx)
    hi  = min(lo + 1, n - 1)
    return sorted_data[lo] * (1 - (idx - lo)) + sorted_data[hi] * (idx - lo)


# ── Worker loop ───────────────────────────────────────────────────────────────
async def worker(
    client:     httpx.AsyncClient,
    targets:    list[dict],
    results:    dict[str, EndpointResult],
    sem:        asyncio.Semaphore,
    stop_event: asyncio.Event,
) -> None:
    """
    Single load-test worker: loops until stop_event is set.
    Picks a random target on each iteration to spread load across all endpoints.
    """
    while not stop_event.is_set():
        target = random.choice(targets)
        name   = target["name"]

        payload = target["payload"]() if callable(target["payload"]) else None
        kwargs  = {"json": payload} if payload else {}

        async with sem:
            t0 = time.perf_counter()
            try:
                if target["method"] == "POST":
                    resp = await client.post(target["url"], **kwargs)
                else:
                    resp = await client.get(target["url"])

                elapsed_ms = (time.perf_counter() - t0) * 1000
                code = resp.status_code

                results[name].status_codes[code] = (
                    results[name].status_codes.get(code, 0) + 1
                )

                if 200 <= code < 300:
                    results[name].latencies.append(elapsed_ms)
                else:
                    results[name].error_count += 1

            except Exception:
                results[name].error_count += 1


# ── Progress printer ──────────────────────────────────────────────────────────
async def progress_printer(
    results:    dict[str, EndpointResult],
    stop_event: asyncio.Event,
    start_time: float,
) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(5)
        elapsed = time.time() - start_time
        total   = sum(r.total for r in results.values())
        rps     = total / elapsed if elapsed > 0 else 0
        print(f"  [{elapsed:5.0f}s]  total={total:,}  rps={rps:.1f}", flush=True)


# ── Connectivity pre-check ────────────────────────────────────────────────────
async def connectivity_check(host: str, client: httpx.AsyncClient) -> bool:
    endpoints = [
        f"{host}:8001/health",
        f"{host}:8002/health",
        f"{host}:8003/health",
    ]
    print("\nConnectivity check:")
    all_ok = True
    for url in endpoints:
        try:
            r = await client.get(url, timeout=3)
            status = "✓" if r.status_code == 200 else f"✗ HTTP {r.status_code}"
            print(f"  {status}  {url}")
            if r.status_code != 200:
                all_ok = False
        except Exception as exc:
            print(f"  ✗  {url}  ({exc})")
            all_ok = False
    return all_ok


# ── Report ────────────────────────────────────────────────────────────────────
def print_report(results: dict[str, EndpointResult], duration: float) -> None:
    total_reqs = sum(r.total for r in results.values())
    total_errs = sum(r.error_count for r in results.values())
    global_rps = total_reqs / duration if duration > 0 else 0

    sep = "─" * 92

    print(f"\n{sep}")
    print(f"  LOAD TEST RESULTS  —  {duration:.0f}s  "
          f"total={total_reqs:,}  errors={total_errs:,}  "
          f"err_rate={total_errs/total_reqs*100:.2f}%  "
          f"rps={global_rps:.1f}")
    print(sep)
    print(f"  {'ENDPOINT':<38}  {'N':>6}  {'ERR%':>5}  "
          f"{'p50':>7}  {'p95':>7}  {'p99':>7}  {'MAX':>7}")
    print(sep)

    for name, r in sorted(results.items(), key=lambda x: x[0]):
        if r.total == 0:
            continue
        err_pct = r.error_count / r.total * 100
        sorted_lats = sorted(r.latencies)

        p50 = f"{percentile(sorted_lats, 50):6.1f}ms" if sorted_lats else "    —"
        p95 = f"{percentile(sorted_lats, 95):6.1f}ms" if sorted_lats else "    —"
        p99 = f"{percentile(sorted_lats, 99):6.1f}ms" if sorted_lats else "    —"
        mx  = f"{max(sorted_lats):6.1f}ms"            if sorted_lats else "    —"

        flag = "  ⚠" if err_pct > 5 else ""
        print(f"  {name:<38}  {r.total:>6,}  {err_pct:>4.1f}%  "
              f"{p50}  {p95}  {p99}  {mx}{flag}")

    print(sep)
    print(f"  Overall throughput: {global_rps:.1f} req/s")
    print(sep)


# ── Entry point ───────────────────────────────────────────────────────────────
async def main(host: str, duration: int, concurrency: int) -> None:
    targets = build_targets(host)
    results = {t["name"]: EndpointResult(name=t["name"]) for t in targets}
    sem     = asyncio.Semaphore(concurrency)
    stop    = asyncio.Event()

    limits  = httpx.Limits(max_connections=concurrency + 10,
                           max_keepalive_connections=concurrency)
    timeout = httpx.Timeout(10.0)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        ok = await connectivity_check(host, client)
        if not ok:
            print("\n⚠  One or more services are unreachable. "
                  "Start the stack with: docker compose up --build\n")
            return

        print(f"\nStarting load test — duration={duration}s  "
              f"concurrency={concurrency}  targets={len(targets)}")
        print("Progress (every 5s):")

        start = time.time()

        workers   = [asyncio.create_task(worker(client, targets, results, sem, stop))
                     for _ in range(concurrency)]
        progress  = asyncio.create_task(progress_printer(results, stop, start))

        await asyncio.sleep(duration)
        stop.set()
        progress.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await asyncio.gather(progress, return_exceptions=True)

        elapsed = time.time() - start

    print_report(results, elapsed)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Load test the Observability Platform",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--host",        default="http://localhost",
                   help="Base host (no port — ports are appended per service)")
    p.add_argument("--duration",    type=int, default=60,
                   help="Test duration in seconds")
    p.add_argument("--concurrency", type=int, default=20,
                   help="Max concurrent in-flight requests")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        asyncio.run(main(args.host, args.duration, args.concurrency))
    except KeyboardInterrupt:
        print("\nInterrupted.")
