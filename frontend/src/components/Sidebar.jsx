const navItems = [
  { key: 'dashboard', label: 'Dashboard', icon: '⌂' },
  { key: 'monitor', label: 'Workflow Monitor', icon: '◫' },
  { key: 'history', label: 'History', icon: '↺' },
  { key: 'fileIngestion', label: 'LIM Ingestion', icon: '⇩' },
  { key: 'limReload', label: 'Load / Reload', icon: '↻', child: true },
  { key: 'notifications', label: 'Notifications', icon: '✉' }
]

export default function Sidebar({ activePage, onNavigate }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark"><span className="brand-dot">◆</span></div>
        <div>
          <div className="brand-kumo">KUMO</div>
          <div className="brand-subtitle">Workflow Manager</div>
        </div>
      </div>

      <div className="nav-label">Navigation</div>
      <nav className="nav-list" aria-label="Main navigation">
        {navItems.map(item => (
          <button
            key={item.key}
            type="button"
            className={`nav-item ${item.child ? 'nav-item-child' : ''} ${activePage === item.key ? 'active' : ''}`}
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
