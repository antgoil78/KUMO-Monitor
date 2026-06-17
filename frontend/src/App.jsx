import { useState } from 'react'
import Sidebar from './components/Sidebar.jsx'
import Monitor from './pages/Monitor.jsx'
import History from './pages/History.jsx'
import Notifications from './pages/Notifications.jsx'

const pages = {
  monitor: Monitor,
  history: History,
  notifications: Notifications
}

export default function App() {
  const [page, setPage] = useState('monitor')
  const Page = pages[page] || Monitor

  return (
    <div className="app-shell">
      <Sidebar activePage={page} onNavigate={setPage} />
      <main className="main-content">
        <Page />
      </main>
    </div>
  )
}
