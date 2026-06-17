const items = [
  { key: 'monitor', label: 'Monitor', icon: '●' },
  { key: 'history', label: 'History', icon: '◷' },
  { key: 'notifications', label: 'Notifications', icon: '✉' }
]

export default function Sidebar({ activePage, onNavigate }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-kumo">KUMO</div>
        <div className="brand-cloud">雲</div>
        <div className="brand-subtitle">Monitor</div>
      </div>

      <div className="nav-label">Navigation</div>
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
    </aside>
  )
}
