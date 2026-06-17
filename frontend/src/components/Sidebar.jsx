const items = [
  { key: 'dashboard', label: 'Dashboard', icon: '⌂' },
  { key: 'monitor', label: 'Monitor', icon: '●' },
  { key: 'history', label: 'History', icon: '◷' },
  { key: 'notifications', label: 'Notifications', icon: '✉' }
]

export default function Sidebar({ activePage, onNavigate }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          <span className="brand-dot">雲</span>
        </div>
        <div>
          <div className="brand-kumo">KUMO</div>
          <div className="brand-subtitle">Monitor</div>
        </div>
      </div>

      <div className="nav-label">Main</div>
      <nav className="nav-list">
        {items.map(item => (
          <button
            key={item.key}
            className={`nav-item ${activePage === item.key ? 'active' : ''}`}
            onClick={() => onNavigate(item.key)}
          >
            <span className="nav-icon">{item.icon}</span>
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-help">
        <div className="help-orb" />
        <strong>Need help?</strong>
        <span>Check workflow health and recent runs from the dashboard.</span>
      </div>
    </aside>
  )
}
