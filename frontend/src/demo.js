/**
 * Demo mode — generates realistic synthetic metrics so the dashboard is
 * fully interactive on GitHub Pages without a running backend.
 *
 * Each service has a distinct "personality" (baseline latency, volatility,
 * error rate) to make the demo visually interesting. Values drift slowly
 * over time and occasionally spike to exercise the alert pipeline.
 */

const SERVICES = ['ad-serving', 'bidding-engine', 'click-tracker']

// Per-service baseline characteristics
const PROFILES = {
  'ad-serving':     { baseLat: 72,  jitter: 18, baseErr: 0.008, rps: 22 },
  'bidding-engine': { baseLat: 110, jitter: 35, baseErr: 0.015, rps: 17 },
  'click-tracker':  { baseLat: 58,  jitter: 12, baseErr: 0.006, rps: 30 },
}

// Deterministic-ish noise using a simple LCG so history looks smooth
function noise(seed) {
  let s = seed
  return () => {
    s = (s * 1664525 + 1013904223) & 0xffffffff
    return (s >>> 0) / 0xffffffff
  }
}

function makeWindow(serviceName, minutesAgo, spikeChance = 0.08) {
  const p = PROFILES[serviceName]
  const rng = noise(Date.now() - minutesAgo * 60_000 + serviceName.charCodeAt(0) * 999)

  const spike = rng() < spikeChance
  const latMult = spike ? 2.8 + rng() * 1.5 : 1.0

  const avgLat = p.baseLat * latMult + (rng() - 0.5) * p.jitter
  const p95Lat = avgLat * (1.4 + rng() * 0.4)
  const p99Lat = p95Lat * (1.2 + rng() * 0.3)

  const errSpike = rng() < 0.06
  const errRate = errSpike
    ? p.baseErr * (4 + rng() * 6)
    : p.baseErr * (0.5 + rng())

  const rps = p.rps * (0.8 + rng() * 0.5)

  const windowEnd = new Date(Date.now() - minutesAgo * 60_000)
  const windowStart = new Date(windowEnd.getTime() - 30_000)

  return {
    avg_latency_ms: +avgLat.toFixed(1),
    p95_latency_ms: +p95Lat.toFixed(1),
    p99_latency_ms: +p99Lat.toFixed(1),
    error_rate:     +errRate.toFixed(4),
    throughput_rps: +rps.toFixed(1),
    request_count:  Math.round(rps * 30),
    window_start:   windowStart.toISOString(),
    window_end:     windowEnd.toISOString(),
    label:          windowStart.toTimeString().slice(0, 8),
  }
}

export function generateDemoSummary() {
  const services = {}
  const allAlerts = []

  SERVICES.forEach(svc => {
    const latest = makeWindow(svc, 0, 0.12)
    const alerts = []

    if (latest.avg_latency_ms > 300) {
      alerts.push({
        alert_type:   'HIGH_LATENCY',
        severity:     'CRITICAL',
        message:      `${svc} avg latency ${latest.avg_latency_ms.toFixed(1)}ms exceeds CRITICAL threshold 300ms`,
        metric_value: latest.avg_latency_ms,
        triggered_at: new Date(Date.now() - 90_000).toISOString(),
      })
    } else if (latest.avg_latency_ms > 150) {
      alerts.push({
        alert_type:   'HIGH_LATENCY',
        severity:     'WARNING',
        message:      `${svc} avg latency ${latest.avg_latency_ms.toFixed(1)}ms exceeds WARNING threshold 150ms`,
        metric_value: latest.avg_latency_ms,
        triggered_at: new Date(Date.now() - 45_000).toISOString(),
      })
    }

    if (latest.error_rate > 0.02) {
      alerts.push({
        alert_type:   'HIGH_ERROR_RATE',
        severity:     'CRITICAL',
        message:      `${svc} error rate ${(latest.error_rate*100).toFixed(2)}% exceeds CRITICAL threshold 2.0%`,
        metric_value: latest.error_rate,
        triggered_at: new Date(Date.now() - 60_000).toISOString(),
      })
    } else if (latest.error_rate > 0.005) {
      alerts.push({
        alert_type:   'HIGH_ERROR_RATE',
        severity:     'WARNING',
        message:      `${svc} error rate ${(latest.error_rate*100).toFixed(2)}% exceeds WARNING threshold 0.5%`,
        metric_value: latest.error_rate,
        triggered_at: new Date(Date.now() - 30_000).toISOString(),
      })
    }

    let status = 'healthy'
    if (alerts.some(a => a.severity === 'CRITICAL')) status = 'critical'
    else if (alerts.some(a => a.severity === 'WARNING')) status = 'warning'

    services[svc] = { status, metrics: latest, active_alerts: alerts }
    allAlerts.push(...alerts.map(a => ({ ...a, service_name: svc })))
  })

  return {
    timestamp:           new Date().toISOString(),
    services,
    total_active_alerts: allAlerts.length,
    _demo:               true,
  }
}

export function generateDemoHistory(limit = 20) {
  const history = {}
  SERVICES.forEach(svc => {
    history[svc] = Array.from({ length: limit }, (_, i) =>
      makeWindow(svc, (limit - i) * 0.5)
    )
  })
  return history
}
