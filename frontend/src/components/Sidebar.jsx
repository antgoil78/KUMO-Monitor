import { useState } from 'react'

const navItems = [
  { key: 'dashboard', label: 'Dashboard', icon: '⌂' },
  { key: 'monitor', label: 'Workflow Monitor', icon: '◫' },
  { key: 'history', label: 'History', icon: '↺' },
  { key: 'lim', label: 'LIM', icon: '⇩', children: [
    { key: 'fileIngestion', label: 'Ingestion', icon: '⇩' },
    { key: 'limReload', label: 'Load / Reload', icon: '↻' }
  ] },
  { key: 'notifications', label: 'Notifications', icon: '✉' },
  { key: 'admin', label: 'Application Log', icon: '▤' },
  { key: 'settings', label: 'Settings', icon: '⚙' }
]

export default function Sidebar({ activePage, onNavigate, session }) {
  const ingestionActive = activePage === 'fileIngestion' || activePage === 'limReload'
  const [ingestionOpen, setIngestionOpen] = useState(ingestionActive)

  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark"><span className="brand-dot">◆</span></div>
        <div>
          <div className="brand-kumo">KUMO</div>
          <div className="brand-subtitle">Monitor</div>
        </div>
      </div>
      <div className="sidebar-user" title={`${session?.displayName || session?.userName || 'KUMO user'} · ${session?.roleName || 'Unknown role'}`}>
        <span>{String(session?.displayName || session?.userName || 'K').slice(0, 1).toUpperCase()}</span>
        <div><strong>{session?.displayName || session?.userName || 'KUMO user'}</strong><small>{session?.roleName || 'Snowflake operations'}</small></div>
      </div>
      <div className="nav-label">Navigation</div>
      <nav className="nav-list" aria-label="Main navigation">
        {navItems.map(item => item.children ? (
          <div className={`nav-group ${ingestionOpen ? 'open' : ''}`} key={item.key}>
            <button
              type="button"
              className={`nav-item nav-group-toggle ${ingestionActive ? 'active' : ''}`}
              aria-expanded={ingestionOpen}
              onClick={() => setIngestionOpen(value => !value)}
            >
              <span className="nav-icon" aria-hidden="true">{item.icon}</span>
              <span>{item.label}</span>
              <span className="nav-chevron" aria-hidden="true">›</span>
            </button>
            {ingestionOpen && <div className="nav-submenu">
              {item.children.map(child => (
                <button
                  key={child.key}
                  type="button"
                  className={`nav-item nav-item-child ${activePage === child.key ? 'active' : ''}`}
                  onClick={() => onNavigate(child.key)}
                >
                  <span className="nav-submenu-line" aria-hidden="true" />
                  <span>{child.label}</span>
                </button>
              ))}
            </div>}
          </div>
        ) : (
          <button
            key={item.key}
            type="button"
            className={`nav-item ${activePage === item.key ? 'active' : ''}`}
            onClick={() => onNavigate(item.key)}
          >
            <span className="nav-icon" aria-hidden="true">{item.icon}</span>
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-help">
        <strong>KUMO Monitor</strong>
        <span>Snowflake workflow and ingestion operations.</span>
        <div className="help-orb" />
      </div>
    </aside>
  )
}
