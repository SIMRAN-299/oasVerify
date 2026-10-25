import React, { useState, useEffect, useRef } from 'react';
import { Icon } from './Icon';
import { MAX_RECENT } from '../sessions';

const NAV_ITEMS = [
  { view: 'home', label: 'Generator', icon: 'home', path: () => '/' },
  { view: 'dashboard', label: 'Execution Dashboard', icon: 'activity', path: (sid) => `/execution_dashboard/${sid}`, needsSession: true },
  { view: 'visualize', label: 'Visualize', icon: 'compass', path: (sid) => `/visualize/${sid}`, needsSession: true },
  { view: 'code', label: 'PyTest Code', icon: 'code', path: (sid) => `/tests/${sid}`, needsSession: true }
];

const THEME_OPTIONS = [
  { value: 'system', label: 'System', icon: 'monitor' },
  { value: 'light', label: 'Light', icon: 'sun' },
  { value: 'dark', label: 'Dark', icon: 'moon' }
];

function statusBadge(summary) {
  if (!summary) return null;
  const st = String(summary.status || '').toLowerCase();
  if (st === 'completed' || st === 'success') {
    return (
      <span className="badge badge-success">
        <Icon name="check" /> Ready
      </span>
    );
  }
  if (st === 'failed' || st === 'error') {
    return (
      <span className="badge badge-danger">
        <Icon name="alert" /> Failed
      </span>
    );
  }
  return (
    <span className="badge badge-running">
      <span className="dot" /> Generating
    </span>
  );
}

function SessionName({ session, onRename }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(session?.name || '');
  const inputRef = useRef(null);

  useEffect(() => {
    if (!editing) setDraft(session?.name || '');
  }, [session?.name, editing]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  if (!session) return null;

  const save = (e) => {
    if (e) e.preventDefault();
    const clean = draft.trim();
    if (clean && clean !== session.name) onRename(session.id, clean);
    setEditing(false);
  };

  if (editing) {
    return (
      <form className="session-rename" onSubmit={save}>
        <input
          ref={inputRef}
          className="input"
          value={draft}
          maxLength={48}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') {
              e.preventDefault();
              setEditing(false);
            }
          }}
          aria-label="Session name"
        />
        <button type="submit" className="btn-icon btn-icon--xs" aria-label="Save name" title="Save">
          <Icon name="check" />
        </button>
        <button type="button" className="btn-icon btn-icon--xs" onClick={() => setEditing(false)} aria-label="Cancel" title="Cancel">
          <Icon name="x" />
        </button>
      </form>
    );
  }

  return (
    <div className="sidebar__session-name-row">
      <span className="sidebar__session-name" title={session.name}>
        {session.name}
      </span>
      <button type="button" className="btn-icon btn-icon--xs" onClick={() => setEditing(true)} aria-label="Rename session" title="Rename">
        <Icon name="pencil" />
      </button>
    </div>
  );
}

export function Sidebar({
  activeView,
  activeSessionId,
  activeSession,
  recentSessions = [],
  sessionSummary,
  collapsed,
  onToggleCollapse,
  onNavigate,
  onRenameSession,
  themePreference,
  onChooseTheme
}) {
  const result = sessionSummary?.result || {};
  const apiTitle = result.api_title || activeSession?.apiTitle || '';
  const testCount = result.test_count ?? activeSession?.testCount;
  const recent = recentSessions.slice(0, MAX_RECENT - 1);

  return (
    <aside className="sidebar glass" aria-label="Primary navigation">
      <div
        className="sidebar__brand"
        onClick={() => onNavigate('/')}
        role="link"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') onNavigate('/');
        }}
        title="OASVerify"
      >
        <div className="brand-mark" aria-hidden="true">
          <Icon name="sparkles" />
        </div>
        <div className="brand-text">
          <span className="brand-title">OASVerify</span>
          <span className="brand-subtitle">Autonomous API QA Agent</span>
        </div>
      </div>

      <div className="sidebar__scroll">
        <div className="sidebar__section">
          <div className="sidebar__label">Workspace</div>
          <nav className="sidebar__nav" aria-label="Workspace">
            {NAV_ITEMS.map((item) => {
              const disabled = item.needsSession && !activeSessionId;
              const active = activeView === item.view;
              return (
                <button
                  key={item.view}
                  type="button"
                  className={`nav-item ${active ? 'is-active' : ''}`}
                  aria-current={active ? 'page' : undefined}
                  disabled={disabled}
                  title={disabled ? `${item.label} — generate a suite first` : item.label}
                  onClick={() => onNavigate(item.path(activeSessionId))}
                >
                  <Icon name={item.icon} />
                  <span className="nav-item__label">{item.label}</span>
                </button>
              );
            })}
          </nav>
        </div>

        <div className="sidebar__section">
          <div className="sidebar__label">Help</div>
          <nav className="sidebar__nav" aria-label="Help">
            <button
              type="button"
              className={`nav-item ${activeView === 'guide' ? 'is-active' : ''}`}
              aria-current={activeView === 'guide' ? 'page' : undefined}
              title="User guide"
              onClick={() => onNavigate('/guide')}
            >
              <Icon name="book-open" />
              <span className="nav-item__label">User guide</span>
            </button>
            <a className="nav-item" href="/docs" target="_blank" rel="noreferrer" title="Backend API reference (FastAPI docs)">
              <Icon name="book" />
              <span className="nav-item__label">API reference</span>
              <Icon name="arrow-up-right" className="nav-item__hint" />
            </a>
          </nav>
        </div>

        <div className="sidebar__section">
          <div className="sidebar__label">Active session</div>
          <div className="sidebar__session">
            {activeSessionId ? (
              <>
                <SessionName session={activeSession || { id: activeSessionId, name: 'Session' }} onRename={onRenameSession} />
                <div className="sidebar__session-api" title={apiTitle || 'Generated test suite'}>
                  {apiTitle || 'Generated test suite'}
                </div>
                <div className="sidebar__session-meta">
                  {statusBadge(sessionSummary)}
                  {typeof testCount === 'number' && <span className="tabular">{testCount} tests</span>}
                </div>
              </>
            ) : (
              <div className="sidebar__session-empty">
                No active session yet. Generate a suite from the Generator to unlock the dashboard, visualizer, and code export.
              </div>
            )}
          </div>

          {recent.length > 0 && (
            <div className="sidebar__recent" aria-label="Recent sessions">
              <div className="sidebar__recent-label">
                <Icon name="history" /> Recent
              </div>
              {recent.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className="recent-item"
                  onClick={() => onNavigate(`/execution_dashboard/${s.id}`)}
                  title={s.apiTitle ? `${s.name} · ${s.apiTitle}` : s.name}
                >
                  <span className="recent-item__name">{s.name}</span>
                  {s.apiTitle && <span className="recent-item__meta">{s.apiTitle}</span>}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="sidebar__spacer" />
      </div>

      <div className="sidebar__footer">
        <div className="theme-switch" role="radiogroup" aria-label="Theme">
          {THEME_OPTIONS.map((opt) => {
            const active = themePreference === opt.value;
            return (
              <button
                key={opt.value}
                type="button"
                role="radio"
                aria-checked={active}
                className={`theme-switch__btn ${active ? 'is-active' : ''}`}
                onClick={(e) => onChooseTheme(opt.value, e)}
                title={`${opt.label} theme`}
              >
                <Icon name={opt.icon} />
                <span>{opt.label}</span>
              </button>
            );
          })}
        </div>

        <button
          type="button"
          className="nav-item sidebar__collapse"
          onClick={onToggleCollapse}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <Icon name={collapsed ? 'chevrons-right' : 'chevrons-left'} />
          <span className="nav-item__label">Collapse</span>
        </button>
      </div>
    </aside>
  );
}
