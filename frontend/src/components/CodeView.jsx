import React, { useState, useEffect } from 'react';
import { Icon } from './Icon';

export function CodeView({ sessionId, navigate }) {
  const [scope, setScope] = useState('all');
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    fetch(`/api/session/${sessionId}/code?scope=${scope}`)
      .then(async (res) => {
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || err.detail || 'Failed to load generated PyTest code');
        }
        return res.json();
      })
      .then((payload) => {
        if (active) {
          setData(payload);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (active) {
          setError(err.message);
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [sessionId, scope]);

  const handleCopy = async () => {
    if (!data?.code) return;
    try {
      await navigator.clipboard.writeText(data.code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch (err) {
      console.error('Copy failed:', err);
    }
  };

  const handleDownload = async (downloadScope) => {
    setDownloading(true);
    try {
      const response = await fetch(`/download_tests/${sessionId}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: downloadScope })
      });
      if (!response.ok) {
        const errPayload = await response.json().catch(() => ({}));
        throw new Error(errPayload.detail || 'Unable to download test file.');
      }
      const blob = await response.blob();
      const disposition = response.headers.get('Content-Disposition') || '';
      const filenameMatch = disposition.match(/filename="?([^"]+)"?/i);
      const filename = filenameMatch
        ? filenameMatch[1]
        : downloadScope === 'successful'
        ? `test_api_journeys_successful_${sessionId.slice(0, 8)}.py`
        : `test_api_journeys_${sessionId.slice(0, 8)}.py`;
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      alert(err.message || 'Unable to download test file.');
    } finally {
      setDownloading(false);
    }
  };

  const lines = data?.code ? data.code.split('\n') : [];
  const noSuccessful = scope === 'successful' && data && data.total_passed_steps === 0;

  return (
    <div>
      <div className="page-head">
        <div>
          <span className="eyebrow">
            <Icon name="code" /> Standalone PyTest suite
          </span>
          <h1 style={{ marginTop: 6 }}>{data?.filename || 'test_suite.py'}</h1>
          <p>
            Self-contained Python test module — ready for CI. Switch scope to export only the steps that passed in the
            dashboard.
          </p>
        </div>

        <div className="page-head__actions">
          <div className="segmented" role="tablist" aria-label="Test scope">
            <button type="button" role="tab" aria-selected={scope === 'all'} className={`segmented__btn ${scope === 'all' ? 'is-active' : ''}`} onClick={() => setScope('all')}>
              <Icon name="layers" /> All tests
              {typeof data?.total_all_steps === 'number' && <code style={{ fontSize: 11 }}>{data.total_all_steps}</code>}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={scope === 'successful'}
              className={`segmented__btn ${scope === 'successful' ? 'is-active' : ''}`}
              onClick={() => setScope('successful')}
            >
              <Icon name="check-circle" /> Successful
              {typeof data?.total_passed_steps === 'number' && <code style={{ fontSize: 11 }}>{data.total_passed_steps}</code>}
            </button>
          </div>

          <button type="button" className="btn btn-secondary btn-sm" onClick={handleCopy} disabled={!data?.code}>
            <Icon name={copied ? 'check' : 'copy'} />
            {copied ? 'Copied' : 'Copy'}
          </button>

          <button type="button" className="btn btn-primary btn-sm" onClick={() => handleDownload(scope)} disabled={downloading || loading || !!error || noSuccessful}>
            {downloading ? <span className="spinner" /> : <Icon name="download" />}
            Download .py
          </button>
        </div>
      </div>

      {loading && (
        <div className="card loading-state">
          <span className="spinner" /> Loading generated PyTest suite…
        </div>
      )}

      {!loading && error && (
        <div className="card empty">
          <div className="empty__icon">
            <Icon name="alert" />
          </div>
          <h2>{error}</h2>
          <p>Generated artifacts were not found for this session.</p>
          <div className="empty__actions">
            <button type="button" className="btn btn-primary" onClick={() => navigate(`/execution_dashboard/${sessionId}`)}>
              <Icon name="play" /> Go to Execution Dashboard
            </button>
            <button type="button" className="btn btn-secondary" onClick={() => navigate('/')}>
              <Icon name="arrow-left" /> Generator
            </button>
          </div>
        </div>
      )}

      {!loading && !error && noSuccessful && (
        <div className="alert alert-note" style={{ marginBottom: 14 }}>
          <Icon name="lightbulb" />
          <div>
            <strong>No successful tests recorded yet.</strong> Run journeys in the Execution Dashboard first, or switch back to{' '}
            <button type="button" className="btn btn-ghost btn-xs" onClick={() => setScope('all')} style={{ verticalAlign: 'middle' }}>
              All tests
            </button>
            .
          </div>
        </div>
      )}

      {!loading && !error && data?.code && (
        <div className="code-frame">
          <div className="code-frame__bar">
            <span style={{ display: 'inline-flex', alignItems: 'center' }}>
              <span className="code-frame__dots" aria-hidden="true">
                <i />
                <i />
                <i />
              </span>
              <code style={{ background: 'transparent', border: 0, padding: 0, color: 'var(--text-2)' }}>{data.filename}</code>
            </span>
            <span className="tabular">
              {lines.length} lines · Python · {scope === 'successful' ? `${data.total_passed_steps} passed steps` : `${data.total_all_steps} steps`}
            </span>
          </div>
          <div className="code-view">
            <table>
              <tbody>
                {lines.map((line, idx) => (
                  <tr key={idx}>
                    <td className="ln">{idx + 1}</td>
                    <td className="cl">{line || ' '}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
