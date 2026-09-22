import { Link, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import ExperimentsPage from './ExperimentsPage'
import TracesPage from './TracesPage'
import TracePage from './TracePage'
import ReviewPage from './ReviewPage'
import ComparePage from './ComparePage'

function LegacyRoute() {
  const location = useLocation()
  const path = location.pathname.replace(/^\/acciowork(?=\/|$)/, '') || '/experiments'
  return <Navigate to={`${path}${location.search}${location.hash}`} replace />
}

export default function App() {
  return <div className="workbench">
    <a className="skip-link" href="#main">跳到内容</a>
    <header className="workbench-header">
      <Link className="workbench-brand" to="/experiments" aria-label="Open-Acciowork 评测工作台首页">
        <span className="workbench-monogram" aria-hidden="true">A</span>
        <span>Open-Acciowork<small>评测工作台</small></span>
      </Link>
      <nav aria-label="评测导航">
        {[
          ['/experiments', '实验'], ['/traces', 'Trace'], ['/review', '人工复核'], ['/compare', '实验对比'],
        ].map(([to, label]) => <NavLink key={to} to={to}>{label}</NavLink>)}
      </nav>
    </header>
    <main id="main" className="workbench-main">
      <Routes>
        <Route path="/" element={<Navigate to="/experiments" replace />} />
        <Route path="/experiments" element={<ExperimentsPage />} />
        <Route path="/experiments/:id" element={<ExperimentsPage />} />
        <Route path="/traces" element={<TracesPage />} />
        <Route path="/traces/:id" element={<TracePage />} />
        <Route path="/review" element={<ReviewPage />} />
        <Route path="/compare" element={<ComparePage />} />
        <Route path="/acciowork/*" element={<LegacyRoute />} />
        <Route path="*" element={<div className="accio accio-panel"><h1>页面不存在</h1><Link to="/experiments">返回实验列表</Link></div>} />
      </Routes>
    </main>
  </div>
}
