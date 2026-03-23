import { useState, useEffect, useCallback, useRef } from 'react'
import ServiceCard from './components/ServiceCard'
import MetricsChart from './components/MetricsChart'
import AlertBanner from './components/AlertBanner'
import { generateDemoSummary, generateDemoHistory } from './demo'

// ── Config ─────────────────────────────────────────────────────────────────
// Override with VITE_ALERTS_URL env var for production deployments.
const ALERTS_BASE = import.meta.env.VITE_ALERTS_URL ?? 'http://localhost:8003'
const POLL_MS     = 5_000
const SERVICE_ORDER = ['ad-serving', 'bidding-engine', 'click-tracker']

// After this many consecutive fetch failures, fall back to demo mode
const DEMO_THRESHOLD = 2

function formatTime(d) {
  if (!d) return '—'
  return new Date(d).toLocaleTimeString()
}

function totalEvents(summary) {
  if (!summary?.services) return 0
  return Object.values(summary.services).reduce(
    (acc, s) => acc + (s?.metrics?.request_count ?? 0), 0
  )
}

function avgRps(summary) {
  if (!summary?.services) return 0
  const vals = Object.values(summary.services)
    .map(s => s?.metrics?.throughput_rps ?? 0)
  return (vals.reduce((a, b) => a + b, 0) / vals.length) || 0
}

export default function App() {
  const [summary,    setSummary]    = useState(null)
  const [history,    setHistory]    = useState({})
  const [lastUpdate, setLastUpdate] = useState(null)
  const [isDemo,     setIsDemo]     = useState(false)
  const [error,      setError]      = useState(null)
  const [loading,    setLoading]    = useState(true)

  const failCount = useRef(0)
  const fetching  = useRef(false)

  const fetchData = useCallback(async () => {
    if (fetching.current) return
    fetching.current = true
    try {
      const [sr, hr] = await Promise.all([
        fetch(`${ALERTS_BASE}/metrics/summary`),
        fetch(`${ALERTS_BASE}/metrics/history?limit=20`),
      ])
      if (!sr.ok || !hr.ok) throw new Error(`API error ${sr.status}`)

      const [s, h] = await Promise.all([sr.json(), hr.json()])
      setSummary(s)
      setHistory(h)
      setLastUpdate(new Date())
      setError(null)
      setIsDemo(false)
      failCount.current = 0
    } catch (err) {
      failCount.current++
      if (failCount.current >= DEMO_THRESHOLD) {
        // Backend unreachable — show demo data so the page isn't blank
        setSummary(generateDemoSummary())
        setHistory(generateDemoHistory())
        setLastUpdate(new Date())
        setIsDemo(true)
        setError(null)
      } else {
        setError(err.message)
      }
    } finally {
      setLoading(false)
      fetching.current = false
    }
  }, [])

  useEffect(() => {
    fetchData()
    const id = setInterval(fetchData, POLL_MS)
    return () => clearInterval(id)
  }, [fetchData])

  if (loading) {
    return (
      <div className="loading-screen">
        <div className="spinner" />
        <span>Connecting to observability platform…</span>
      </div>
    )
  }

  const services     = summary?.services ?? {}
  const totalAlerts  = summary?.total_active_alerts ?? 0
  const allAlerts    = SERVICE_ORDER.flatMap(svc =>
    (services[svc]?.active_alerts ?? []).map(a => ({ ...a, service_name: svc }))
  )

  const healthyCount  = SERVICE_ORDER.filter(s => services[s]?.status === 'healthy').length
  const totalWindowRps = avgRps(summary).toFixed(1)

  return (
    <div className="dashboard">

      {/* ── Hero ──────────────────────────────────────────────────────── */}
      <header className="hero">
        <div className="hero-left">
          <div className="hero-title">
            <div className="hero-title-icon">⬡</div>
            Distributed Microservices Observability
          </div>
          <div className="hero-desc">
            Real-time SLA monitoring across a simulated ads platform.
            Events flow through Redis pub/sub → metrics aggregation → PostgreSQL →
            threshold evaluation with auto-alert lifecycle management.
          </div>
          <div className="hero-tags">
            <span className="tag">FastAPI</span>
            <span className="tag">Redis pub/sub</span>
            <span className="tag">PostgreSQL</span>
            <span className="tag">Docker Compose</span>
            <span className="tag">asyncio</span>
            <span className="tag">React</span>
          </div>
        </div>

        <div className="hero-right">
          {isDemo ? (
            <div className="demo-badge">◎ DEMO MODE</div>
          ) : (
            <div className="live-badge">● LIVE</div>
          )}
          <div className="update-time">
            {error
              ? <span style={{ color: 'var(--red)' }}>⚠ {error}</span>
              : `Updated ${formatTime(lastUpdate)}`
            }
          </div>
          {isDemo && (
            <div style={{ fontSize: 11, color: 'var(--muted)', textAlign: 'right', maxWidth: 200 }}>
              Backend offline — showing synthetic data.
              Run <code style={{ fontFamily: 'var(--font-mono)', color: 'var(--muted-2)' }}>docker compose up</code> for live metrics.
            </div>
          )}
        </div>
      </header>

      {/* ── Stats strip ───────────────────────────────────────────────── */}
      <div className="stats-strip">
        <div className="stat-pill">
          <span className="stat-label">Services</span>
          <span className="stat-value">{SERVICE_ORDER.length}</span>
        </div>
        <div className="stat-pill">
          <span className="stat-label">Healthy</span>
          <span className={`stat-value ${healthyCount === SERVICE_ORDER.length ? 'green' : 'yellow'}`}>
            {healthyCount}/{SERVICE_ORDER.length}
          </span>
        </div>
        <div className="stat-pill">
          <span className="stat-label">Active Alerts</span>
          <span className={`stat-value ${totalAlerts > 0 ? (totalAlerts > 2 ? 'red' : 'yellow') : 'green'}`}>
            {totalAlerts}
          </span>
        </div>
        <div className="stat-pill">
          <span className="stat-label">Avg Throughput</span>
          <span className="stat-value">{totalWindowRps} rps</span>
        </div>
        <div className="stat-pill">
          <span className="stat-label">Poll Interval</span>
          <span className="stat-value">{POLL_MS / 1000}s</span>
        </div>
      </div>

      {/* ── Main ──────────────────────────────────────────────────────── */}
      <main className="dashboard-main">

        {/* Alerts */}
        <AlertBanner alerts={allAlerts} totalCount={totalAlerts} />

        {/* Service health cards */}
        <div>
          <div className="section-header">
            <span className="section-title">Service Health</span>
            <div className="section-line" />
          </div>
          <div className="services-grid">
            {SERVICE_ORDER.map(svc => (
              <ServiceCard key={svc} name={svc} data={services[svc] ?? null} />
            ))}
          </div>
        </div>

        {/* Latency charts */}
        <div>
          <div className="section-header">
            <span className="section-title">Latency — avg &amp; p95 (ms)</span>
            <div className="section-line" />
          </div>
          <div className="charts-grid">
            {SERVICE_ORDER.map(svc => (
              <MetricsChart key={svc} name={svc} data={history[svc] ?? []} metric="latency" />
            ))}
          </div>
        </div>

        {/* Error rate + throughput */}
        <div>
          <div className="section-header">
            <span className="section-title">Error Rate &amp; Throughput</span>
            <div className="section-line" />
          </div>
          <div className="charts-grid">
            {SERVICE_ORDER.map(svc => (
              <MetricsChart key={svc} name={svc} data={history[svc] ?? []} metric="error_throughput" />
            ))}
          </div>
        </div>

      </main>

      {/* ── Footer ────────────────────────────────────────────────────── */}
      <footer className="footer">
        <div className="footer-left">
          <div className={`footer-dot ${error ? 'err' : ''}`} />
          <span>{isDemo ? 'Demo mode — synthetic data' : 'Live — polling backend'}</span>
          <span style={{ color: 'var(--border-2)' }}>·</span>
          <span>{ALERTS_BASE}</span>
        </div>
        <div className="footer-right">
          <span>Alerts service</span>
          <span style={{ color: 'var(--border-2)' }}>·</span>
          <span>Refresh {POLL_MS / 1000}s</span>
        </div>
      </footer>

    </div>
  )
}
