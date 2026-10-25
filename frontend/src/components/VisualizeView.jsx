import React, { useState, useEffect, useRef } from 'react';
import { Icon } from './Icon';

/* --------------------------------------------------------------------------
   Mermaid: render one diagram at a time with a unique id. Rendering all
   journeys in one `mermaid.run()` call collides on the shared S0…Sn node ids.
   -------------------------------------------------------------------------- */
let mermaidReady = false;
let mermaidQueue = Promise.resolve();
let mermaidSeq = 0;

function ensureMermaid() {
  if (!window.mermaid) return false;
  if (!mermaidReady) {
    window.mermaid.initialize({
      startOnLoad: false,
      theme: 'neutral',
      securityLevel: 'strict',
      flowchart: { htmlLabels: true, curve: 'basis', padding: 10, useMaxWidth: true },
      themeVariables: {
        fontFamily: 'ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
        fontSize: '13px'
      }
    });
    mermaidReady = true;
  }
  return true;
}

function enqueueMermaidRender(id, chart) {
  const job = mermaidQueue.then(() => window.mermaid.render(id, chart));
  mermaidQueue = job.catch(() => {});
  return job;
}

function MermaidChart({ chart }) {
  const hostRef = useRef(null);
  const [state, setState] = useState('pending'); // pending | ready | offline | error

  useEffect(() => {
    let cancelled = false;
    const host = hostRef.current;
    if (!host || !chart) return undefined;

    if (!ensureMermaid()) {
      setState('offline');
      return undefined;
    }

    mermaidSeq += 1;
    const id = `oas-mmd-${mermaidSeq}`;
    setState('pending');

    enqueueMermaidRender(id, chart)
      .then(({ svg, bindFunctions }) => {
        if (cancelled || !hostRef.current) return;
        hostRef.current.innerHTML = svg;
        if (bindFunctions) bindFunctions(hostRef.current);
        setState('ready');
      })
      .catch((err) => {
        console.warn('Mermaid render warning:', err);
        if (!cancelled) setState('error');
      });

    return () => {
      cancelled = true;
    };
  }, [chart]);

  return (
    <div className="mermaid-paper">
      {state === 'pending' && <div className="skeleton" style={{ height: 120 }} aria-label="Rendering flowchart" />}
      {(state === 'offline' || state === 'error') && (
        <pre style={{ whiteSpace: 'pre-wrap', fontSize: 12, color: '#334155' }}>
          {state === 'offline'
            ? 'Flowchart renderer could not be loaded (offline?). Showing the graph definition instead.\n\n'
            : 'Flowchart could not be rendered. Showing the graph definition instead.\n\n'}
          {chart}
        </pre>
      )}
      <div ref={hostRef} className="mermaid" style={{ display: state === 'ready' ? 'flex' : 'none' }} />
    </div>
  );
}

export function VisualizeView({ sessionId, navigate }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');
    fetch(`/api/session/${sessionId}/visualize`)
      .then(async (res) => {
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || err.detail || 'Failed to load visualization data');
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
  }, [sessionId]);

  if (loading) {
    return (
      <div className="card loading-state">
        <span className="spinner" /> Loading provenance &amp; journey flowcharts…
      </div>
    );
  }

  if (error) {
    return (
      <div className="card empty">
        <div className="empty__icon">
          <Icon name="alert" />
        </div>
        <h2>{error}</h2>
        <p>Visualization data was not found for this session.</p>
        <div className="empty__actions">
          <button type="button" className="btn btn-primary" onClick={() => navigate('/')}>
            <Icon name="arrow-left" /> Back to Generator
          </button>
        </div>
      </div>
    );
  }

  const counts = data?.summary_counts || data?.summary_stats || {};
  const clusters = data?.generation_report?.clusters || data?.clusters || [];
  const journeys = data?.journeys || data?.scenarios || [];
  const baselineCount = (counts.fallback ?? 0) + (counts.template ?? 0) || (counts.baseline_count ?? 0);

  const originPill = (src) => {
    if (src === 'gemini' || src === 'ai') return 'pill--primary';
    if (src === 'fallback' || src === 'baseline') return 'pill--success';
    return 'pill--warning';
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <span className="eyebrow">
            <Icon name="compass" /> Provenance &amp; dependency DAG
          </span>
          <h1 style={{ marginTop: 6 }}>{data?.api_title || data?.title || 'API test suite'}</h1>
          <p>
            How Gemini synthesis, the deterministic baseline, guardrail passes and runtime self-healing shaped each
            journey. Flowcharts render the step dependency graph.
          </p>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn-primary btn-sm" onClick={() => navigate(`/execution_dashboard/${sessionId}`)}>
            <Icon name="play" /> Execution Dashboard
          </button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigate(`/tests/${sessionId}`)}>
            <Icon name="code" /> PyTest code
          </button>
        </div>
      </div>

      {/* KPIs */}
      <div className="stats" style={{ marginBottom: 16 }}>
        <div className="stat">
          <div className="stat__label">Journeys</div>
          <div className="stat__value">{counts.total_journeys ?? journeys.length}</div>
          <div className="stat__caption">Across {clusters.length || '—'} domain clusters</div>
        </div>
        <div className="stat stat--primary">
          <div className="stat__label">Designed by Gemini</div>
          <div className="stat__value">{counts.gemini ?? counts.ai_designed ?? 0}</div>
          <div className="stat__caption">LLM-synthesized flows</div>
        </div>
        <div className="stat stat--success">
          <div className="stat__label">Deterministic baseline</div>
          <div className="stat__value">{baselineCount}</div>
          <div className="stat__caption">Rule-based CRUD coverage</div>
        </div>
        <div className="stat stat--warning">
          <div className="stat__label">Guardrail adjustments</div>
          <div className="stat__value">{counts.adjustments_total ?? counts.guardrail_adjusted ?? 0}</div>
          <div className="stat__caption">Sanitizer &amp; validator edits</div>
        </div>
        <div className="stat stat--heal">
          <div className="stat__label">Runtime self-heals</div>
          <div className="stat__value">{counts.heals_total ?? counts.healed_journeys ?? 0}</div>
          <div className="stat__caption">{counts.gemini_heals ?? 0} applied by Gemini</div>
        </div>
      </div>

      {/* Domain cluster table */}
      {clusters.length > 0 && (
        <section className="card" style={{ marginBottom: 16 }}>
          <div className="card__header">
            <h2>
              <Icon name="layers" /> Domain cluster synthesis
            </h2>
            <span className="pill">{clusters.length} clusters</span>
          </div>
          <div style={{ overflowX: 'auto' }}>
            <table className="table">
              <thead>
                <tr>
                  <th>Domain</th>
                  <th>Operations</th>
                  <th>Deterministic baseline</th>
                  <th>Gemini raw</th>
                  <th>Final journeys</th>
                </tr>
              </thead>
              <tbody>
                {clusters.map((c, idx) => (
                  <tr key={idx}>
                    <td style={{ fontWeight: 600 }}>{c.domain || c.cluster_name}</td>
                    <td className="num">{c.operations_count ?? (c.operations || []).length}</td>
                    <td className="num">{(c.deterministic_baseline_titles || c.baseline_scenarios || []).length}</td>
                    <td className="num">{(c.llm_raw_titles || c.gemini_scenarios || []).length}</td>
                    <td className="num" style={{ color: 'var(--primary-text)' }}>
                      {(c.final_titles || c.final_scenarios || []).length}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}

      <div className="vz-legend" aria-label="Flowchart legend">
        <span>
          <i className="vz-swatch vz-swatch--gemini" /> Gemini-designed step
        </span>
        <span>
          <i className="vz-swatch vz-swatch--base" /> Baseline step
        </span>
        <span>
          <i className="vz-swatch vz-swatch--negative" /> Guardrail-adjusted
        </span>
        <span>
          <i className="vz-swatch vz-swatch--healed" /> Self-healed at runtime
        </span>
      </div>

      {/* Journeys */}
      <div className="journeys">
        {journeys.map((item, idx) => {
          const scen = item.scenario || item;
          const scenId = scen.scenario_id || scen.id || `journey-${idx + 1}`;
          const title = scen.name || scen.title || scenId;
          const desc = scen.description || '';
          const prov = item.provenance || {};
          const src = prov.source || item.origin_tone || 'gemini';
          const originLabel = prov.label || item.origin_label || (src === 'gemini' ? 'Designed by Gemini' : src === 'fallback' ? 'Deterministic baseline' : 'Template');
          const adjustments = prov.adjustments || item.guardrail_notes || [];
          const heals = item.healing_entries || item.healing_events || [];
          const mermaidChart = item.mermaid_graph || item.mermaid_chart || '';
          const steps = item.steps || scen.steps || [];
          const lastRun = item.last_run;
          const lastRunOk = lastRun && (lastRun.status === 'SUCCESS' || lastRun.passed === true);

          return (
            <section key={scenId} className="card journey" style={{ '--i': idx }}>
              <div className="journey__header">
                <div className="journey__lead">
                  <div style={{ minWidth: 0 }}>
                    <div className="journey__title">
                      <span className="journey__name">
                        {idx + 1}. {title}
                      </span>
                      <span className={`pill ${originPill(src)}`}>
                        <Icon name={src === 'gemini' ? 'sparkles' : 'cpu'} /> {originLabel}
                      </span>
                      {heals.length > 0 && (
                        <span className="pill pill--heal">
                          <Icon name="wand" /> Self-healed ×{heals.length}
                        </span>
                      )}
                      {lastRun && (
                        <span className={`badge ${lastRunOk ? 'badge-success' : 'badge-danger'}`}>
                          <Icon name={lastRunOk ? 'check' : 'x'} /> Last run {lastRunOk ? 'passed' : 'failed'}
                        </span>
                      )}
                    </div>
                    {desc && <div className="journey__desc">{desc}</div>}
                  </div>
                </div>
                <code className="faint" style={{ fontSize: 11.5, background: 'transparent', border: 0 }}>
                  {scenId}
                </code>
              </div>

              <div className="journey__body" style={{ padding: 16 }}>
                <div className="vz-journey__grid">
                  <div>
                    {mermaidChart ? (
                      <MermaidChart chart={mermaidChart} />
                    ) : (
                      <div className="panel-muted" style={{ padding: 20, textAlign: 'center' }}>
                        <span className="faint">No flowchart available for this journey.</span>
                      </div>
                    )}
                  </div>

                  <div>
                    {prov.reasoning && (
                      <div className="vz-panel vz-panel--gemini">
                        <div className="vz-panel__title">
                          <Icon name="sparkles" /> Design rationale
                        </div>
                        <p>{prov.reasoning}</p>
                      </div>
                    )}

                    {adjustments.length > 0 && (
                      <div className="vz-panel vz-panel--guard">
                        <div className="vz-panel__title">
                          <Icon name="shield" /> Guardrail adjustments
                        </div>
                        <ul className="vz-list">
                          {adjustments.map((adj, nIdx) => (
                            <li key={nIdx}>
                              {typeof adj === 'string' ? adj : `${adj.step_id ? `[${adj.step_id}] ` : ''}${adj.reason || adj.description || JSON.stringify(adj)}`}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {heals.length > 0 && (
                      <div className="vz-panel vz-panel--heal">
                        <div className="vz-panel__title">
                          <Icon name="wand" /> Runtime self-healing
                        </div>
                        <ul className="vz-list">
                          {heals.map((ev, eIdx) => (
                            <li key={eIdx}>
                              <code>{ev.step_id || ev.target_step}</code>{' '}
                              {ev.reasoning || ev.diagnosis || 'Adjusted step definition to match live server behavior'}
                              {ev.source && <span className="faint"> · via {ev.source}</span>}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    <div className="vz-panel">
                      <div className="vz-panel__title">
                        <Icon name="list" /> Steps ({steps.length})
                      </div>
                      <div className="vz-steps">
                        {steps.map((st, sIdx) => {
                          const pathStr = st.path_template || st.path || '/';
                          const expStatus = st.expected_status_code || st.expected_status || 200;
                          const healed = st.healed_by_ai || st.healed;
                          return (
                            <span key={st.step_id || sIdx} className={`vz-step ${healed ? 'is-healed' : ''}`}>
                              <span className="vz-step__idx">{sIdx + 1}</span>
                              <span className={`method method-${(st.method || 'GET').toUpperCase()}`}>{st.method || 'GET'}</span>
                              <code>{pathStr}</code>
                              <span className="vz-step__exp">→ {expStatus}</span>
                            </span>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </section>
          );
        })}
      </div>
    </div>
  );
}
