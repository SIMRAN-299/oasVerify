import React, { useState, useEffect, useCallback } from 'react';
import { Sidebar } from './components/Sidebar';
import { Topbar } from './components/Topbar';
import { HomeView } from './components/HomeView';
import { DashboardView } from './components/DashboardView';
import { CodeView } from './components/CodeView';
import { VisualizeView } from './components/VisualizeView';
import { GuideView } from './components/GuideView';
import { useTheme } from './theme';
import { SESSIONS_EVENT, getSessions, ensureSession, updateSession, renameSession, removeSession } from './sessions';

export const ACTIVE_SESSION_STORAGE_KEY = 'oas_active_session_id';
const SIDEBAR_COLLAPSED_KEY = 'oas_sidebar_collapsed';
export const SESSION_EVENT = 'oas:session-changed';

function parseRoute(pathname) {
  const clean = pathname.replace(/\/+$/, '') || '/';
  const dashMatch = clean.match(/^\/execution_dashboard\/([^/]+)$/);
  if (dashMatch) return { view: 'dashboard', sessionId: dashMatch[1] };
  const codeMatch = clean.match(/^\/tests\/([^/]+)$/);
  if (codeMatch) return { view: 'code', sessionId: codeMatch[1] };
  const visMatch = clean.match(/^\/visualize\/([^/]+)$/);
  if (visMatch) return { view: 'visualize', sessionId: visMatch[1] };
  if (clean === '/guide') return { view: 'guide', sessionId: '' };
  return { view: 'home', sessionId: '' };
}

function readStoredSession() {
  try {
    return localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY) || '';
  } catch (err) {
    return '';
  }
}

export default function App() {
  const [route, setRoute] = useState(() => parseRoute(window.location.pathname));
  const [activeSessionId, setActiveSessionId] = useState(
    () => parseRoute(window.location.pathname).sessionId || readStoredSession()
  );
  const [sessionSummary, setSessionSummary] = useState(null);
  const [sessions, setSessions] = useState(getSessions);
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1';
    } catch (err) {
      return false;
    }
  });
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [user, setUser] = useState({ username: 'User', email: '', authenticated: false });
  const theme = useTheme();

  // Signed-in identity (Cloud Run + IAP headers → /api/user-info). Falls back to "User".
  useEffect(() => {
    let cancelled = false;
    fetch('/api/user-info', { cache: 'no-store' })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (cancelled || !data || !data.username) return;
        setUser({
          username: String(data.username).trim() || 'User',
          email: data.email || '',
          authenticated: !!data.authenticated
        });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  // Browser back/forward
  useEffect(() => {
    const onPopState = () => {
      const next = parseRoute(window.location.pathname);
      setRoute(next);
      if (next.sessionId) setActiveSessionId(next.sessionId);
    };
    window.addEventListener('popstate', onPopState);
    return () => window.removeEventListener('popstate', onPopState);
  }, []);

  // Views broadcast session changes (new upload, completion) so the shell can react
  useEffect(() => {
    const onSessionChanged = () => {
      const sid = readStoredSession();
      if (sid) setActiveSessionId(sid);
      else {
        setActiveSessionId('');
        setSessionSummary(null);
      }
    };
    window.addEventListener(SESSION_EVENT, onSessionChanged);
    return () => window.removeEventListener(SESSION_EVENT, onSessionChanged);
  }, []);

  // Keep the friendly-name registry in sync (renames, new sessions, removals)
  useEffect(() => {
    const onSessionsChanged = () => setSessions(getSessions());
    window.addEventListener(SESSIONS_EVENT, onSessionsChanged);
    return () => window.removeEventListener(SESSIONS_EVENT, onSessionsChanged);
  }, []);

  // Any session we become aware of gets a stable friendly name
  useEffect(() => {
    if (activeSessionId) ensureSession(activeSessionId);
  }, [activeSessionId]);

  // Lightweight session summary for the sidebar card
  useEffect(() => {
    if (!activeSessionId) {
      setSessionSummary(null);
      return undefined;
    }
    let cancelled = false;
    let timer = null;

    const load = async () => {
      try {
        const res = await fetch(`/status/${encodeURIComponent(activeSessionId)}`);
        const data = await res.json().catch(() => null);
        if (cancelled) return;
        if (!res.ok || !data || data.status === 'not_found') {
          setSessionSummary(null);
          if (res.status === 404 || (data && data.status === 'not_found')) removeSession(activeSessionId);
          return;
        }
        setSessionSummary(data);
        const result = data.result || {};
        updateSession(activeSessionId, {
          apiTitle: result.api_title,
          testCount: typeof result.test_count === 'number' ? result.test_count : undefined,
          specFile: data.spec_file || result.spec_file,
          status: data.status
        });
        const st = String(data.status || '').toLowerCase();
        const terminal = st === 'completed' || st === 'success' || st === 'failed' || st === 'error';
        if (!terminal) timer = setTimeout(load, 3000);
      } catch (err) {
        if (!cancelled) setSessionSummary(null);
      }
    };
    load();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [activeSessionId]);

  // Close the mobile drawer with Escape
  useEffect(() => {
    if (!drawerOpen) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') setDrawerOpen(false);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [drawerOpen]);

  const navigate = useCallback((path) => {
    const [pathname, hash] = path.split('#');
    if (pathname !== window.location.pathname) {
      window.history.pushState({}, '', path);
    } else if (hash) {
      window.history.replaceState({}, '', path);
    }
    const next = parseRoute(pathname);
    setRoute(next);
    if (next.sessionId) {
      setActiveSessionId(next.sessionId);
      try {
        localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, next.sessionId);
      } catch (err) {
        /* ignore */
      }
    }
    setDrawerOpen(false);
    if (hash) {
      setTimeout(() => {
        const el = document.getElementById(hash);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 60);
    } else {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }, []);

  const toggleCollapse = () => {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? '1' : '0');
      } catch (err) {
        /* ignore */
      }
      return next;
    });
  };

  const handleRenameSession = useCallback((id, name) => {
    renameSession(id, name);
  }, []);

  const activeSession = sessions.find((s) => s.id === activeSessionId) || null;
  const recentSessions = sessions.filter((s) => s.id !== activeSessionId);
  const topbarSessionId = route.sessionId || activeSessionId;
  const topbarSession = sessions.find((s) => s.id === topbarSessionId) || null;

  const shellClass = ['app-shell', collapsed ? 'is-collapsed' : '', drawerOpen ? 'is-drawer-open' : '']
    .filter(Boolean)
    .join(' ');

  const viewKey = `${route.view}:${route.sessionId || ''}`;

  return (
    <>
      <div className="aurora" aria-hidden="true">
        <span className="aurora__orb aurora__orb--a" />
        <span className="aurora__orb aurora__orb--b" />
        <span className="aurora__orb aurora__orb--c" />
      </div>

      <div className={shellClass}>
        <div className={`sidebar-backdrop ${drawerOpen ? 'is-visible' : ''}`} onClick={() => setDrawerOpen(false)} aria-hidden="true" />

        <Sidebar
          activeView={route.view}
          activeSessionId={activeSessionId}
          activeSession={activeSession}
          recentSessions={recentSessions}
          sessionSummary={sessionSummary}
          collapsed={collapsed}
          onToggleCollapse={toggleCollapse}
          onNavigate={navigate}
          onRenameSession={handleRenameSession}
          themePreference={theme.preference}
          onChooseTheme={theme.choose}
        />

        <div className="main">
          <Topbar
            view={route.view}
            sessionId={topbarSessionId}
            sessionName={topbarSession ? topbarSession.name : ''}
            onOpenMenu={() => setDrawerOpen(true)}
            onNavigate={navigate}
            resolvedTheme={theme.resolved}
            onToggleTheme={theme.toggle}
            user={user}
          />

          <main key={viewKey} className={`content view-enter ${route.view === 'home' ? 'content--narrow' : ''}`}>
            {route.view === 'home' && <HomeView navigate={navigate} />}
            {route.view === 'dashboard' && <DashboardView sessionId={route.sessionId} navigate={navigate} />}
            {route.view === 'code' && <CodeView sessionId={route.sessionId} navigate={navigate} />}
            {route.view === 'visualize' && <VisualizeView sessionId={route.sessionId} navigate={navigate} />}
            {route.view === 'guide' && <GuideView navigate={navigate} activeSessionId={activeSessionId} />}
          </main>
        </div>
      </div>
    </>
  );
}
