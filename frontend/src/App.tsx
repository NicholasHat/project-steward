import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AnimatePresence, MotionConfig } from 'framer-motion'
import { AuthProvider } from '@/hooks/useAuth'
import { ProtectedRoute } from '@/components/layout/ProtectedRoute'
import { AppShell } from '@/components/layout/AppShell'
import { ProjectShell } from '@/components/layout/ProjectShell'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'

import { LoginPage } from '@/pages/auth/LoginPage'
import { RegisterPage } from '@/pages/auth/RegisterPage'
import { ProjectsListPage } from '@/pages/projects/ProjectsListPage'
import { PipelinePage } from '@/pages/project/PipelinePage'
import { ReportPage } from '@/pages/project/ReportPage'
import { ArtifactsPage } from '@/pages/project/ArtifactsPage'
import { TimelinePage } from '@/pages/project/TimelinePage'
import { DirectionPage } from '@/pages/project/DirectionPage'
import { GapsPage } from '@/pages/project/GapsPage'

function AnimatedRoutes() {
  const location = useLocation()
  return (
    <AnimatePresence mode="wait" initial={false}>
      <Routes location={location} key={location.pathname}>
        <Route path="/login" element={<LoginPage />} />
        <Route path="/register" element={<RegisterPage />} />

        <Route element={<ProtectedRoute />}>
          <Route element={<AppShell />}>
            <Route path="/" element={<ProjectsListPage />} />
          </Route>

          <Route path="/projects/:projectId" element={<ProjectShell />}>
            <Route index element={<Navigate to="report" replace />} />
            <Route path="report" element={<ReportPage />} />
            <Route path="artifacts" element={<ArtifactsPage />} />
            <Route path="artifacts/:artifactId" element={<ArtifactsPage />} />
            <Route path="timeline" element={<TimelinePage />} />
            <Route path="direction" element={<DirectionPage />} />
            <Route path="gaps" element={<GapsPage />} />
            <Route path="pipeline" element={<PipelinePage />} />
          </Route>
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </AnimatePresence>
  )
}

function App() {
  return (
    // reducedMotion="user" makes every Framer Motion animation in the app
    // honor the OS-level prefers-reduced-motion setting automatically,
    // rather than requiring each component to check it individually.
    <MotionConfig reducedMotion="user">
      <AuthProvider>
        <TooltipProvider delayDuration={200}>
          <AnimatedRoutes />
          <Toaster />
        </TooltipProvider>
      </AuthProvider>
    </MotionConfig>
  )
}

export default App
