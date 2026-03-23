const LATENCY_WARN = 150
const LATENCY_CRIT = 300
const ERROR_WARN   = 0.005
const ERROR_CRIT   = 0.02

function latClass(v) {
  if (v == null) return ''
  if (v > LATENCY_CRIT) return 'crit'
  if (v > LATENCY_WARN) return 'warn'
  return ''
}

function errClass(v) {
  if (v == null) return ''
  if (v > ERROR_CRIT) return 'crit'
  if (v > ERROR_WARN) return 'warn'
  return ''
}

function fmt(v, d = 1) {
  return v != null ? Number(v).toFixed(d) : '—'
}

const STATUS_LABEL = {
  healthy:  'HEALTHY',
  warning:  'WARNING',
  critical: 'CRITICAL',
  no_data:  'NO DATA',
}

export default function ServiceCard({ name, data }) {
  const status  = data?.status  ?? 'no_data'
  const metrics = data?.metrics ?? null
  const alerts  = data?.active_alerts ?? []

  return (
    <div className={`service-card status-${status}`}>

      <div className="card-header">
        <span className="card-name">{name}</span>
        <span className={`status-badge ${status}`}>
          <span className={`status-dot ${status}`} />
          {STATUS_LABEL[status] ?? status.toUpperCase()}
        </span>
      </div>

      {metrics ? (
        <div className="metrics-grid">

          <div className="metric-tile">
            <span className="metric-label">Avg Latency</span>
            <div className="metric-value">
              <span className={latClass(metrics.avg_latency_ms)}>
                {fmt(metrics.avg_latency_ms)}
              </span>
              <span className="metric-unit">ms</span>
            </div>
            <span className="metric-sub">p95 {fmt(metrics.p95_latency_ms)}ms</span>
          </div>

          <div className="metric-tile">
            <span className="metric-label">P99 Latency</span>
            <div className="metric-value">
              <span className={latClass(metrics.p99_latency_ms)}>
                {fmt(metrics.p99_latency_ms)}
              </span>
              <span className="metric-unit">ms</span>
            </div>
            <span className="metric-sub">tail</span>
          </div>

          <div className="metric-tile">
            <span className="metric-label">Error Rate</span>
            <div className="metric-value">
              <span className={errClass(metrics.error_rate)}>
                {fmt((metrics.error_rate ?? 0) * 100, 2)}
              </span>
              <span className="metric-unit">%</span>
            </div>
            <span className="metric-sub">
              of {(metrics.request_count ?? 0).toLocaleString()} reqs
            </span>
          </div>

          <div className="metric-tile">
            <span className="metric-label">Throughput</span>
            <div className="metric-value">
              {fmt(metrics.throughput_rps)}
              <span className="metric-unit">rps</span>
            </div>
            <span className="metric-sub">
              {metrics.window_start
                ? new Date(metrics.window_start).toLocaleTimeString()
                : '—'}
            </span>
          </div>

        </div>
      ) : (
        <div className="no-data-state">
          <div className="spinner" />
          Waiting for first metrics window…
        </div>
      )}

      {alerts.length > 0 && (
        <div style={{
          fontSize: 11,
          fontFamily: 'var(--font-mono)',
          color: alerts.some(a => a.severity === 'CRITICAL') ? 'var(--red)' : 'var(--yellow)',
          borderTop: '1px solid var(--border)',
          paddingTop: 10,
        }}>
          {alerts.length} active alert{alerts.length > 1 ? 's' : ''} · {alerts[0].severity}
        </div>
      )}

    </div>
  )
}
