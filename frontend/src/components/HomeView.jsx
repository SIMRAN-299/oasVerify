import React, { useState, useEffect, useRef } from 'react';
import { Icon } from './Icon';
import { ensureSession, sessionLabel } from '../sessions';

const ACTIVE_SESSION_STORAGE_KEY = 'oas_active_session_id';
const SESSION_EVENT = 'oas:session-changed';

const notifySessionChanged = () => {
  try {
    window.dispatchEvent(new Event(SESSION_EVENT));
  } catch (err) {
    /* ignore */
  }
};

const formatBytes = (bytes) => {
  if (!bytes && bytes !== 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

export function HomeView({ navigate }) {
  const [samples, setSamples] = useState([
    { filename: 'ecommerce.yaml', label: 'E-commerce', title: 'Cloud Run E-Commerce CRUD API', size_formatted: '51 KB', download_url: '/sample_oas/ecommerce.yaml' },
    { filename: 'weather.yaml', label: 'Weather', title: 'weather.gov API', size_formatted: '142 KB', download_url: '/sample_oas/weather.yaml' }
  ]);
  const [specFile, setSpecFile] = useState(null);
  const [selectedSample, setSelectedSample] = useState('ecommerce.yaml');
  const [configFile, setConfigFile] = useState(null);
  const [specDragOver, setSpecDragOver] = useState(false);
  const [configDragOver, setConfigDragOver] = useState(false);

  const [sessionId, setSessionId] = useState('');
  const [statusData, setStatusData] = useState(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [logsVisible, setLogsVisible] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');

  const specInputRef = useRef(null);
  const configInputRef = useRef(null);
  const logViewerRef = useRef(null);
  const pollIntervalRef = useRef(null);

  useEffect(() => {
    fetch('/api/samples')
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data && Array.isArray(data.samples) && data.samples.length > 0) {
          setSamples(data.samples);
          if (data.default_sample) {
            setSelectedSample((prev) => prev || data.default_sample);
          }
        }
      })
      .catch(() => {});

    const savedSessionId = localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY);
    if (savedSessionId) {
      resumeExistingSession(savedSessionId);
    }

    return () => {
      if (pollIntervalRef.current) clearInterval(pollIntervalRef.current);
    };
  }, []);

  useEffect(() => {
    if (logViewerRef.current && logsVisible) {
      logViewerRef.current.scrollTop = logViewerRef.current.scrollHeight;
    }
  }, [statusData?.logs, logsVisible]);

  const stopPolling = () => {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
  };

  const isTerminalSuccess = (st) => {
    const norm = String(st || '').trim().toLowerCase();
    return norm === 'completed' || norm === 'complete' || norm === 'success';
  };

  const isTerminalFailure = (st) => {
    const norm = String(st || '').trim().toLowerCase();
    return norm === 'failed' || norm === 'error' || norm === 'not_found' || norm.startsWith('failed') || norm.startsWith('error');
  };

  const pollSessionStatus = async (sid) => {
    try {
      const res = await fetch(`/status/${sid}`);
      if (res.status === 404) {
        stopPolling();
        localStorage.removeItem(ACTIVE_SESSION_STORAGE_KEY);
        notifySessionChanged();
        setIsGenerating(false);
        return;
      }
      const data = await res.json();
      if (data.status === 'not_found') {
        stopPolling();
        localStorage.removeItem(ACTIVE_SESSION_STORAGE_KEY);
        notifySessionChanged();
        setIsGenerating(false);
        return;
      }

      setStatusData(data);

      if (isTerminalSuccess(data.status)) {
        stopPolling();
        setIsGenerating(false);
        notifySessionChanged();
      } else if (isTerminalFailure(data.status)) {
        stopPolling();
        setIsGenerating(false);
        setLogsVisible(true);
        notifySessionChanged();
      }
    } catch (err) {
      console.error('Polling error:', err);
    }
  };

  const startStatusPolling = (sid) => {
    stopPolling();
    pollSessionStatus(sid);
    pollIntervalRef.current = setInterval(() => pollSessionStatus(sid), 1500);
  };

  const resumeExistingSession = async (sid) => {
    try {
      const res = await fetch(`/status/${sid}`);
      if (!res.ok) {
        localStorage.removeItem(ACTIVE_SESSION_STORAGE_KEY);
        notifySessionChanged();
        return;
      }
      const data = await res.json();
      if (data.status === 'not_found') {
        localStorage.removeItem(ACTIVE_SESSION_STORAGE_KEY);
        notifySessionChanged();
        return;
      }
      setSessionId(sid);
      setStatusData(data);
      if (!isTerminalSuccess(data.status) && !isTerminalFailure(data.status)) {
        setIsGenerating(true);
        startStatusPolling(sid);
      } else {
        setIsGenerating(false);
        if (isTerminalFailure(data.status)) setLogsVisible(true);
      }
    } catch (err) {
      console.error('Failed to resume session:', err);
    }
  };

  const handleSpecFileChange = (file) => {
    if (!file) return;
    setSpecFile(file);
    setSelectedSample('');
    setErrorMsg('');
  };

  const clearSpecFile = (e) => {
    if (e) e.stopPropagation();
    setSpecFile(null);
    setSelectedSample(samples[0]?.filename || 'ecommerce.yaml');
    if (specInputRef.current) specInputRef.current.value = '';
  };

  const handleSampleChange = (sampleName) => {
    setSelectedSample(sampleName);
    if (sampleName) {
      setSpecFile(null);
      if (specInputRef.current) specInputRef.current.value = '';
      setErrorMsg('');
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!specFile && !selectedSample) {
      setErrorMsg('Please select an OpenAPI specification file or choose a bundled sample first.');
      return;
    }
    setErrorMsg('');
    setIsGenerating(true);
    setLogsVisible(false);
    setStatusData({
      status: 'running',
      message: 'Submitting specification to the test generation pipeline…',
      logs: []
    });

    const formData = new FormData();
    if (specFile) formData.append('file', specFile);
    else if (selectedSample) formData.append('sample_spec', selectedSample);
    if (configFile) formData.append('config_file', configFile);

    try {
      const res = await fetch('/upload', { method: 'POST', body: formData });
      if (!res.ok) {
        const errPayload = await res.json().catch(() => ({}));
        throw new Error(errPayload.detail || 'Upload failed');
      }
      const payload = await res.json();
      const newSessionId = payload.session_id;
      ensureSession(newSessionId, { specFile: payload.spec_used || specFile?.name || selectedSample });
      setSessionId(newSessionId);
      localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, newSessionId);
      notifySessionChanged();
      startStatusPolling(newSessionId);
    } catch (err) {
      setIsGenerating(false);
      setStatusData({
        status: 'failed',
        message: `Error: ${err.message}`,
        logs: [`[ERROR] Failed to upload file: ${err.message}`]
      });
      setLogsVisible(true);
    }
  };

  const isCompleted = isTerminalSuccess(statusData?.status);
  const isFailed = isTerminalFailure(statusData?.status);
  const isRunning = !!statusData && !isCompleted && !isFailed;
  const resObj = statusData?.result || {};
  const currentSampleObj = samples.find((s) => (s.filename || s.name) === selectedSample);
  const logs = statusData?.logs || [];

  return (
    <div>
      {/* Hero */}
      <section className="hero glass">
        <div className="hero__kicker">
          <Icon name="shield" />
          <span>Autonomous API QA agent · Powered by Gemini</span>
        </div>
        <h1>Zero-script API QA from your OpenAPI spec</h1>
        <p>
          Upload an OpenAPI 3.x / Swagger 2.0 document or pick a bundled sample. OASVerify synthesizes dependency-ordered
          journeys, executes them live with AI self-healing, and exports a standalone PyTest suite.
        </p>
      </section>

      <div className="how">
        <div className="how__tile">
          <span className="how__num">1</span>
          <div>
            <div className="how__title">Generate</div>
            <div className="how__desc">Spec is parsed, clustered by domain, and turned into stateful CRUD journeys.</div>
          </div>
        </div>
        <div className="how__tile">
          <span className="how__num">2</span>
          <div>
            <div className="how__title">Execute &amp; heal</div>
            <div className="how__desc">Run journeys 4× in parallel; failures are diagnosed and patched in place.</div>
          </div>
        </div>
        <div className="how__tile">
          <span className="how__num">3</span>
          <div>
            <div className="how__title">Export</div>
            <div className="how__desc">Download the PyTest file or push it straight to a GitHub branch or PR.</div>
          </div>
        </div>
      </div>

      {/* Generator card */}
      <section className="card">
        <div className="card__header">
          <h2>
            <Icon name="rocket" /> New test generation
          </h2>
          {isRunning && (
            <span className="badge badge-running">
              <span className="dot" /> In progress
            </span>
          )}
        </div>

        <form onSubmit={handleSubmit} className="card__body" style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
          {/* 1. Spec */}
          <div>
            <label className="form-label">
              1. OpenAPI specification <small>(.yaml, .yml, .json)</small>
            </label>
            <div
              className={`dropzone ${specDragOver ? 'is-dragover' : ''} ${specFile ? 'has-file' : ''}`}
              onClick={() => specInputRef.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setSpecDragOver(true);
              }}
              onDragLeave={() => setSpecDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setSpecDragOver(false);
                if (e.dataTransfer.files?.length) handleSpecFileChange(e.dataTransfer.files[0]);
              }}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  specInputRef.current?.click();
                }
              }}
              aria-label="Upload OpenAPI specification"
            >
              <input
                ref={specInputRef}
                type="file"
                accept=".yaml,.yml,.json"
                style={{ display: 'none' }}
                onChange={(e) => {
                  if (e.target.files?.length) handleSpecFileChange(e.target.files[0]);
                }}
              />
              <div className="dropzone__icon">
                <Icon name={specFile ? 'check' : 'upload'} />
              </div>
              <div className="dropzone__text">
                <div className="dropzone__title">
                  {specFile ? 'Specification attached' : 'Drop your OpenAPI file here, or click to browse'}
                </div>
                {specFile ? (
                  <span className="dropzone__file">
                    {specFile.name} · {formatBytes(specFile.size)}
                  </span>
                ) : (
                  <span className="dropzone__hint">
                    {currentSampleObj
                      ? `Using bundled sample: ${currentSampleObj.title || currentSampleObj.label} (${currentSampleObj.filename})`
                      : 'No file selected'}
                  </span>
                )}
              </div>
              {specFile && (
                <button type="button" className="btn btn-ghost btn-sm dropzone__clear" onClick={clearSpecFile}>
                  <Icon name="x" /> Clear
                </button>
              )}
            </div>
          </div>

          {/* Samples */}
          <div className={`samples ${specFile ? 'is-disabled' : ''}`}>
            <div className="samples__label">
              {specFile ? 'Bundled samples are ignored while a file is attached' : 'Or try a bundled OpenAPI sample'}
            </div>
            <div className="samples__list">
              {samples.map((s) => {
                const val = s.filename || s.name;
                const isChecked = !specFile && selectedSample === val;
                return (
                  <div key={val} className={`sample ${isChecked ? 'is-selected' : ''}`}>
                    <label className="sample__pick">
                      <input
                        type="radio"
                        name="sample_spec"
                        value={val}
                        checked={isChecked}
                        disabled={!!specFile}
                        onChange={() => handleSampleChange(val)}
                      />
                      <span className="sample__name">{s.label}</span>
                      <span className="sample__title">{s.title}</span>
                    </label>
                    <a href={s.download_url} download={s.filename} className="sample__file" title={`Download ${s.filename}`}>
                      <Icon name="download" />
                      <code>{s.filename}</code>
                      {s.size_formatted && <span>{s.size_formatted}</span>}
                    </a>
                  </div>
                );
              })}
            </div>
          </div>

          {/* 2. Config */}
          <div>
            <label className="form-label">
              2. Execution config <small>(optional test_config.json — base URL, headers, auth)</small>
            </label>
            <div
              className={`dropzone dropzone--compact ${configDragOver ? 'is-dragover' : ''} ${configFile ? 'has-file' : ''}`}
              onClick={() => configInputRef.current?.click()}
              onDragOver={(e) => {
                e.preventDefault();
                setConfigDragOver(true);
              }}
              onDragLeave={() => setConfigDragOver(false)}
              onDrop={(e) => {
                e.preventDefault();
                setConfigDragOver(false);
                if (e.dataTransfer.files?.length) setConfigFile(e.dataTransfer.files[0]);
              }}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  configInputRef.current?.click();
                }
              }}
              aria-label="Upload execution config"
            >
              <input
                ref={configInputRef}
                type="file"
                accept=".json"
                style={{ display: 'none' }}
                onChange={(e) => {
                  if (e.target.files?.length) setConfigFile(e.target.files[0]);
                }}
              />
              <div className="dropzone__icon">
                <Icon name="file-json" />
              </div>
              <div className="dropzone__text">
                <div className="dropzone__title" style={{ fontSize: 14 }}>
                  {configFile ? 'Config attached' : 'No config selected'}
                </div>
                {configFile ? (
                  <span className="dropzone__file">
                    {configFile.name} · {formatBytes(configFile.size)}
                  </span>
                ) : (
                  <span className="dropzone__hint">Using defaults from the specification's servers block</span>
                )}
              </div>
              {configFile && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm dropzone__clear"
                  onClick={(e) => {
                    e.stopPropagation();
                    setConfigFile(null);
                    if (configInputRef.current) configInputRef.current.value = '';
                  }}
                >
                  <Icon name="x" /> Clear
                </button>
              )}
            </div>
          </div>

          {errorMsg && (
            <div className="alert alert-danger" role="alert">
              <Icon name="alert" />
              <div>{errorMsg}</div>
            </div>
          )}

          <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={isGenerating} id="generate-suite-btn">
            {isGenerating ? (
              <>
                <span className="spinner" />
                <span>Generating test suite…</span>
              </>
            ) : (
              <>
                <Icon name="rocket" />
                <span>Generate test suite</span>
              </>
            )}
          </button>
        </form>

        {/* Status */}
        {statusData && (
          <div className="card__footer" style={{ flexDirection: 'column', alignItems: 'stretch', gap: 14, padding: '18px 20px' }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, minWidth: 0 }}>
                {isCompleted && (
                  <span className="badge badge-success">
                    <Icon name="check" /> Completed
                  </span>
                )}
                {isFailed && (
                  <span className="badge badge-danger">
                    <Icon name="alert" /> Failed
                  </span>
                )}
                {isRunning && (
                  <span className="badge badge-running">
                    <span className="dot" /> Running
                  </span>
                )}
                <span style={{ fontWeight: 500, fontSize: 14 }}>
                  {statusData.message || (isCompleted ? 'Test suite generated successfully.' : statusData.status)}
                </span>
              </div>
              {logs.length > 0 && (
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setLogsVisible((v) => !v)}>
                  <Icon name="terminal" />
                  {logsVisible ? 'Hide generation logs' : 'Show generation logs'}
                </button>
              )}
            </div>

            {isRunning && (
              <div className="progress progress--indeterminate" aria-label="Generation in progress">
                <div className="progress__bar" />
              </div>
            )}

            {logsVisible && (
              <div className="console">
                <div className="console__header">
                  <span>Generation logs</span>
                  <span className="faint tabular">{logs.length} lines</span>
                </div>
                <div className="console__body" ref={logViewerRef}>
                  {logs.length > 0 ? (
                    logs.map((line, idx) => (
                      <span key={idx} className="log-line">
                        {line}
                      </span>
                    ))
                  ) : (
                    <span className="log-line log-line--muted">Waiting for logs…</span>
                  )}
                </div>
              </div>
            )}

            {isCompleted && sessionId && (
              <>
                <div className="kv">
                  <div>
                    <div className="kv__label">API</div>
                    <div className="kv__value">{resObj.api_title || 'OpenAPI service'}</div>
                  </div>
                  <div>
                    <div className="kv__label">Base URL</div>
                    <div className="kv__value mono" style={{ fontSize: 13 }}>
                      {resObj.base_url || 'N/A'}
                    </div>
                  </div>
                  <div>
                    <div className="kv__label">Tests generated</div>
                    <div className="kv__value tabular">{resObj.test_count ?? statusData.scenarios_count ?? '—'}</div>
                  </div>
                  <div>
                    <div className="kv__label">Session</div>
                    <div className="kv__value">{sessionLabel(sessionId)}</div>
                  </div>
                </div>

                <div className="alert alert-note">
                  <Icon name="lightbulb" />
                  <div>
                    <strong>Validate before you export.</strong> Run journeys in the Execution Dashboard so self-healing can fix
                    failures against the live API before you download or push the suite.
                  </div>
                </div>

                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10 }}>
                  <button type="button" className="btn btn-primary" onClick={() => navigate(`/execution_dashboard/${sessionId}`)}>
                    <Icon name="play" /> Open Execution Dashboard
                  </button>
                  <button type="button" className="btn btn-secondary" onClick={() => navigate(`/visualize/${sessionId}`)}>
                    <Icon name="compass" /> Visualize journeys
                  </button>
                  <button type="button" className="btn btn-secondary" onClick={() => navigate(`/tests/${sessionId}`)}>
                    <Icon name="code" /> View PyTest code
                  </button>
                </div>
              </>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
