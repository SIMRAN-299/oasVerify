import React, { useState } from 'react';
import { Icon } from './Icon';

const VIEW_META = {
  home: { title: 'Generator', crumb: 'Generate a test suite' },
  dashboard: { title: 'Execution Dashboard', crumb: 'Run journeys with live self-healing' },
  visualize: { title: 'Visualize', crumb: 'Provenance & journey flowcharts' },
  code: { title: 'PyTest Code', crumb: 'Standalone test suite export' },
  guide: { title: 'User guide', crumb: 'How to use OASVerify' }
};

export function Topbar({ view, sessionId, sessionName, onOpenMenu, onNavigate, resolvedTheme, onToggleTheme, user }) {
  const meta = VIEW_META[view] || VIEW_META.home;
  const [copied, setCopied] = useState(false);
  const displayName = (user && String(user.username || '').trim()) || 'User';
  const initials = initialsFor(displayName);

  const copySession = async () => {
    if (!sessionId) return;
    try {
      await navigator.clipboard.writeText(sessionId);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch (err) {
      console.error('Copy failed:', err);
    }
  };

  const nextThemeLabel = resolvedTheme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme';

  return (
    <header className="topbar glass">
      <div className="topbar__lead">
        <button type="button" className="btn-icon topbar__menu" onClick={onOpenMenu} aria-label="Open navigation">
          <Icon name="menu" />
        </button>
        <div className="topbar__title">
          <div className="topbar__crumbs">
            <span>OASVerify</span>
            <Icon name="chevron-right" />
            <span>{meta.title}</span>
          </div>
          <h1>{meta.crumb}</h1>
        </div>
      </div>

      <div className="topbar__actions">
        {sessionId && (
          <div className="session-chip">
            <Icon name="layers" />
            <span className="session-chip__name">{sessionName || 'Session'}</span>
            <button
              type="button"
              className="btn-icon"
              onClick={copySession}
              aria-label="Copy session ID for API use"
              title="Copy session ID (for API calls)"
            >
              <Icon name={copied ? 'check' : 'copy'} />
            </button>
          </div>
        )}

        <button
          type="button"
          className="btn-icon btn-icon--outline"
          onClick={onToggleTheme}
          aria-label={nextThemeLabel}
          title={nextThemeLabel}
          id="theme-toggle"
        >
          <Icon name={resolvedTheme === 'dark' ? 'sun' : 'moon'} />
        </button>

        {view !== 'home' ? (
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => onNavigate('/')}>
            <Icon name="plus" />
            <span className="topbar__hide-sm">New run</span>
          </button>
        ) : (
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => onNavigate('/guide')}>
            <Icon name="help-circle" />
            <span className="topbar__hide-sm">User guide</span>
          </button>
        )}

        <div
          className="user-chip"
          title={user?.authenticated && user.email ? `Signed in as ${user.email}` : displayName}
          aria-label={`Signed in as ${displayName}`}
        >
          <span className="avatar" aria-hidden="true">
            {initials}
          </span>
          <span className="user-chip__name topbar__hide-sm">{displayName}</span>
        </div>
      </div>
    </header>
  );
}

/** "simran.singh" → "SS", "jdoe" → "JD", "User" → "U" */
function initialsFor(name) {
  const clean = String(name || '').trim();
  if (!clean) return 'U';
  const parts = clean.split(/[.\s_\-]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return clean.slice(0, 2).toUpperCase();
}
