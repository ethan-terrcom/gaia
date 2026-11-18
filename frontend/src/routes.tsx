import { createBrowserRouter } from 'react-router'
import AppLayout from './layout/AppLayout'
import Approvals from './pages/Approvals'
import Dashboard from './pages/Dashboard'
import NotFound from './pages/NotFound'
import PullRequestDetail from './pages/PullRequestDetail'
import PullRequests from './pages/PullRequests'
import RunDetail from './pages/RunDetail'
import Runs from './pages/Runs'
import Settings from './pages/Settings'
import Tickets from './pages/Tickets'
import Worktrees from './pages/Worktrees'

export const router = createBrowserRouter([
  {
    element: <AppLayout />,
    children: [
      { path: '/', element: <Dashboard /> },
      { path: '/tickets', element: <Tickets /> },
      { path: '/runs', element: <Runs /> },
      { path: '/runs/:id', element: <RunDetail /> },
      { path: '/approvals', element: <Approvals /> },
      { path: '/prs', element: <PullRequests /> },
      { path: '/prs/:id', element: <PullRequestDetail /> },
      { path: '/worktrees', element: <Worktrees /> },
      { path: '/settings', element: <Settings /> },
      { path: '*', element: <NotFound /> },
    ],
  },
])
