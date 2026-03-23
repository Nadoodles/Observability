import {
  LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, ResponsiveContainer,
} from 'recharts'

const C = {
  avg:        '#3b82f6',
  p95:        '#8b5cf6',
  error:      '#ef4444',
  throughput: '#10b981',
}

function tooltipStyle() {
  return {
    contentStyle: {
      background:   '#1a2235',
      border:       '1px solid #1f2d45',
      borderRadius: 6,
      fontSize:     11,
      fontFamily:   'monospace',
      padding:      '8px 12px',
    },
    labelStyle:   { color: '#64748b', marginBottom: 4, fontSize: 10 },
    itemStyle:    { padding: '1px 0' },
  }
}

export default function MetricsChart({ name, data, metric }) {
  const isLatency = metric === 'latency'

  const chartData = isLatency
    ? data.map(d => ({
        t:   d.label,
        avg: d.avg_latency_ms != null ? +Number(d.avg_latency_ms).toFixed(1) : null,
        p95: d.p95_latency_ms != null ? +Number(d.p95_latency_ms).toFixed(1) : null,
      }))
    : data.map(d => ({
        t:      d.label,
        'err%': d.error_rate != null ? +((d.error_rate * 100).toFixed(3)) : null,
        rps:    d.throughput_rps != null ? +Number(d.throughput_rps).toFixed(1) : null,
      }))

  const isEmpty = !data || data.length === 0

  return (
    <div className="chart-card">
      <div className="chart-header">
        <span className="chart-title">{name}</span>
        <div className="chart-legend">
          {isLatency ? (
            <>
              <span className="legend-item">
                <span className="legend-dot" style={{ background: C.avg }} /> avg
              </span>
              <span className="legend-item">
                <span className="legend-dot" style={{ background: C.p95 }} /> p95
              </span>
            </>
          ) : (
            <>
              <span className="legend-item">
                <span className="legend-dot" style={{ background: C.error }} /> err%
              </span>
              <span className="legend-item">
                <span className="legend-dot" style={{ background: C.throughput }} /> rps
              </span>
            </>
          )}
        </div>
      </div>

      {isEmpty ? (
        <div style={{ height: 140, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
          <span style={{ color: 'var(--muted)', fontSize: 12 }}>No history yet</span>
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={140}>
          <LineChart data={chartData} margin={{ top: 4, right: 4, bottom: 0, left: -10 }}>
            <CartesianGrid
              strokeDasharray="3 3"
              stroke="rgba(255,255,255,0.04)"
              vertical={false}
            />
            <XAxis
              dataKey="t"
              tick={{ fill: '#64748b', fontSize: 9, fontFamily: 'monospace' }}
              axisLine={false}
              tickLine={false}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fill: '#64748b', fontSize: 9, fontFamily: 'monospace' }}
              axisLine={false}
              tickLine={false}
              width={32}
              tickFormatter={v => isLatency ? `${v}` : `${v}%`}
            />
            {!isLatency && (
              <YAxis
                yAxisId="rps"
                orientation="right"
                tick={{ fill: '#64748b', fontSize: 9, fontFamily: 'monospace' }}
                axisLine={false}
                tickLine={false}
                width={28}
              />
            )}
            <Tooltip
              formatter={(v, n) => [
                isLatency ? `${v}ms` : n === 'rps' ? `${v} rps` : `${v}%`,
                n,
              ]}
              {...tooltipStyle()}
            />
            {isLatency ? (
              <>
                <Line type="monotone" dataKey="avg" stroke={C.avg}
                  strokeWidth={1.5} dot={false} activeDot={{ r: 3, fill: C.avg }} connectNulls />
                <Line type="monotone" dataKey="p95" stroke={C.p95}
                  strokeWidth={1.5} strokeDasharray="4 2"
                  dot={false} activeDot={{ r: 3, fill: C.p95 }} connectNulls />
              </>
            ) : (
              <>
                <Line type="monotone" dataKey="err%" stroke={C.error}
                  strokeWidth={1.5} dot={false} activeDot={{ r: 3, fill: C.error }} connectNulls />
                <Line type="monotone" dataKey="rps" yAxisId="rps" stroke={C.throughput}
                  strokeWidth={1.5} dot={false} activeDot={{ r: 3, fill: C.throughput }} connectNulls />
              </>
            )}
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  )
}
