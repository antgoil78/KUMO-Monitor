import { useState } from 'react'

import Sidebar from './components/Sidebar.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Monitor from './pages/Monitor.jsx'
import History from './pages/History.jsx'
import Notifications from './pages/Notifications.jsx'
import FileIngestion from './pages/FileIngestion.jsx'
import LimReload from './pages/LimReload.jsx'
import DagView from './pages/DagView.jsx'

const pages = {
  dashboard: Dashboard,
  monitor: Monitor,
  history: History,
  notifications: Notifications,
  fileIngestion: FileIngestion,
  limReload: LimReload,
  dag: DagView
}

export default function App() {
  const [page, setPage] = useState('dashboard')
  const [pageContext, setPageContext] = useState({})
  const Page = pages[page] || Dashboard

  function navigate(nextPage, context = {}) {
    setPageContext(context)
    setPage(nextPage)
  }

  return (
    <div className="app-shell">
      <Sidebar activePage={page} onNavigate={navigate} />
      <main className="main-content">
        <Page {...pageContext} onNavigate={navigate} />
      </main>
    </div>
  )
}
