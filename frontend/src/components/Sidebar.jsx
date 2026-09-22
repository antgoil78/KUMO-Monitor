import { useEffect, useState } from 'react'

const navItems = [
  { key: 'dashboard', label: 'Dashboard', icon: '⌂' },
  { key: 'workflow', label: 'Workflow', icon: '◫', children: [
    { key: 'monitor', label: 'Monitor', icon: '◫' },
    { key: 'history', label: 'History', icon: '↺' },
    { key: 'dependencies', label: 'Dependencies', icon: '⌘' }
  ] },
  { key: 'lim', label: 'LIM', icon: '⇩', children: [
    { key: 'fileIngestion', label: 'Ingestion', icon: '⇩' },
    { key: 'limReload', label: 'Load / Reload', icon: '↻' }
  ] },
  { key: 'notifications', label: 'Notifications', icon: '✉' },
  { key: 'admin', label: 'Application Log', icon: '▤' },
  { key: 'settings', label: 'Settings', icon: '⚙' }
]

export default function Sidebar({ activePage, onNavigate, session, buildInfo, collapsed = false }) {
  const [openGroups, setOpenGroups] = useState(() => Object.fromEntries(
    navItems.filter(item => item.children).map(item => [item.key, item.children.some(child => child.key === activePage)])
  ))

  useEffect(() => {
    const activeGroup = navItems.find(item => item.children?.some(child => child.key === activePage))
    if (activeGroup) setOpenGroups(previous => ({ ...previous, [activeGroup.key]: true }))
  }, [activePage])

  return (
    <aside className={`sidebar ${collapsed ? 'collapsed' : ''}`}>
      <div className="brand">
        <img
          className="brand-lockup"
          src="/brand/kumo-monitor-logotype.png"
          alt="KUMO Monitor"
        />
        <span className="brand-icon" aria-hidden="true">
          <img src="/brand/kumo-monitor-logotype.png" alt="" />
        </span>
      </div>
      <div className="sidebar-user" title={`${session?.displayName || session?.userName || 'KUMO user'} · ${session?.roleName || 'Unknown role'}`}>
        <span>{String(session?.displayName || session?.userName || 'K').slice(0, 1).toUpperCase()}</span>
        <div><strong>{session?.displayName || session?.userName || 'KUMO user'}</strong><small>{session?.roleName || 'Snowflake operations'}</small></div>
      </div>
      <div className="nav-label">Navigation</div>
      <nav className="nav-list" aria-label="Main navigation">
        {navItems.map(item => item.children ? (() => {
          const groupOpen = Boolean(openGroups[item.key])
          const groupActive = item.children.some(child => child.key === activePage)
          return (
          <div className={`nav-group ${groupOpen ? 'open' : ''}`} key={item.key}>
            <button
              type="button"
              className={`nav-item nav-group-toggle ${groupActive ? 'active' : ''}`}
              aria-expanded={groupOpen}
              onClick={() => setOpenGroups(previous => ({ ...previous, [item.key]: !previous[item.key] }))}
            >
              <span className="nav-icon" aria-hidden="true">{item.icon}</span>
              <span>{item.label}</span>
              <span className="nav-chevron" aria-hidden="true">›</span>
            </button>
            {groupOpen && <div className="nav-submenu">
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
          )
        })() : (
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

      <div
        className="sidebar-version"
        title={`Image ${buildInfo?.imageVersion || 'latest'} · build ${buildInfo?.buildSha || 'local'}`}
      >
        <span>Version</span>
        <strong>{buildInfo?.imageVersion || 'latest'}</strong>
      </div>

      <div className="sidebar-help">
        <strong>KUMO Monitor</strong>
        <span>Snowflake workflow and ingestion operations.</span>
        <div className="help-orb" />
      </div>
    </aside>
  )
}
