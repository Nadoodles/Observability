import { useState } from 'react'

function relTime(iso) {
  if (!iso) return ''
  const diff = Date.now() - new Date(iso).getTime()
  const m = Math.floor(diff / 60_000)
  if (m < 1)  return 'just now'
  if (m < 60) return `${m}m ago`
  return `${Math.floor(m / 60)}h ago`
}

export default function AlertBanner({ alerts, totalCount }) {
  const [open, setOpen] = useState(true)

  return (
    <div className="alert-banner">
      <div className="alert-banner-header" onClick={() => setOpen(o => !o)}>
        <div className="alert-banner-left">
          {totalCount === 0 ? (
            <span className="alert-count all-clear-text">
              <span style={{ fontSize: 13 }}>✓</span> All systems operational
            </span>
          ) : (
            <span className="alert-count alerts-text">
              <span style={{
                display: 'inline-block',
                width: 7, height: 7,
                borderRadius: '50%',
                background: 'var(--red)',
                animation: 'pulse-r 1s ease-in-out infinite',
                marginRight: 6,
                verticalAlign: 'middle',
              }} />
              {totalCount} active alert{totalCount > 1 ? 's' : ''}
            </span>
          )}
        </div>
        <span className={`chevron ${open ? 'open' : ''}`}>▼</span>
      </div>

      {open && totalCount > 0 && (
        <div className="alert-list">
          {alerts.map((a, i) => (
            <div key={i} className="alert-row">
              <span className={`severity-pill ${a.severity}`}>{a.severity}</span>
              <div className="alert-body">
                <div className="alert-svc">{a.service_name}</div>
                <div>{a.message}</div>
              </div>
              <span className="alert-time">{relTime(a.triggered_at)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
