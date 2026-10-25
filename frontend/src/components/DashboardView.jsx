import React, { useState, useEffect, useRef, useMemo } from 'react';
import { createPortal } from 'react-dom';
import { Icon, StatusIcon } from './Icon';
import { sessionLabel } from '../sessions';

function normalizeScenario(rawScen) {
  const id = rawScen.scenario_id || rawScen.id || '';
  const title = rawScen.name || rawScen.title || id;
  const description = rawScen.description || '';
  const combinedSteps = rawScen.steps || [
    ...(rawScen.setup_steps || []).map((s) => ({ ...s, _phase: 'setup' })),
    ...(rawScen.test_steps || []).map((s) => ({ ...s, _phase: 'test' })),
    ...(rawScen.teardown_steps || []).map((s) => ({ ...s, _phase: 'teardown' }))
  ];
  const steps = combinedSteps.map((st) => ({
    ...st,
    step_id: st.step_id || st.id,
    method: st.method || 'GET',
    path: st.path_template || st.path || '/',
    operation_id: st.operation_id || st.description || '',
    expected_status: st.expected_status_code || st.expected_status || 200,
    healed: !!(st.healed_by_ai || st.healed)
  }));
  return { ...rawScen, id, title, description, steps };
}

/** Strip the view-only aliases added by normalizeScenario so the Request drawer shows the real step payload. */
function requestPreview(step) {
  if (!step || typeof step !== 'object') return step;
  const out = {};
  Object.entries(step).forEach(([k, v]) => {
    if (k.startsWith('_')) return;
    if (k === 'path' && 'path_template' in step) return;
    if (k === 'expected_status' && 'expected_status_code' in step) return;
    if (k === 'operation_id' && !step.operation_id) return;
    if (k === 'healed' && 'healed_by_ai' in step) return;
    out[k] = v;
  });
  return out;
}

function logTone(msg) {
  const m = String(msg);
  if (/\[(PASS|STEP_SUCCESS|SUITE_COMPLETE)\]/.test(m)) return 'pass';
  if (/\[(FAIL|STEP_FAILED|ERROR|HEALING_FAILED)\]/.test(m)) return 'fail';
  if (/\[(HEALING_START|HEALING_APPLIED)\]/.test(m)) return 'heal';
  if (/\[(JOURNEY_START|STEP_START|JOURNEY_COMPLETE)\]/.test(m)) return 'info';
  if (m.startsWith('//')) return 'muted';
  return '';
}

export function DashboardView({ sessionId, navigate }) {
  const [suiteData, setSuiteData] = useState(null);
  const [scenarios, setScenarios] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  // Controls
  const [healingEnabled, setHealingEnabled] = useState(true);
  const [consoleVisible, setConsoleVisible] = useState(false);
  const [consoleLogs, setConsoleLogs] = useState([
    '// Ready. Run the full suite, a journey, or an individual step to stream live telemetry.'
  ]);
  const [downloadMenuOpen, setDownloadMenuOpen] = useState(false);

  // Runtime states
  const [isSuiteRunning, setIsSuiteRunning] = useState(false);
  const [runningScenarios, setRunningScenarios] = useState({});
  const [runningSteps, setRunningSteps] = useState({});

  // Step state maps keyed by `${scenarioId}__${stepId}`
  const [stepStatusMap, setStepStatusMap] = useState({});
  const [stepDetailsMap, setStepDetailsMap] = useState({});
  const [stepHealedMap, setStepHealedMap] = useState({});
  const [stepPayloadsMap, setStepPayloadsMap] = useState({});
  const [scenarioHealingNotes, setScenarioHealingNotes] = useState({});
  const [openRequestSteps, setOpenRequestSteps] = useState({});
  const [openResponseSteps, setOpenResponseSteps] = useState({});

  // GitHub Push Modal state
  const [githubSyncData, setGithubSyncData] = useState(null);
  const [githubModalOpen, setGithubModalOpen] = useState(false);
  const [ghRepo, setGhRepo] = useState('');
  const [ghBranch, setGhBranch] = useState('');
  const [ghFolder, setGhFolder] = useState('');
  const [ghMode, setGhMode] = useState('commit');
  const [ghScope, setGhScope] = useState('all');
  const [ghToken, setGhToken] = useState('');
  const [ghStatusBanner, setGhStatusBanner] = useState(null);
  const [ghSubmitting, setGhSubmitting] = useState(false);

  const consoleRef = useRef(null);
  const downloadMenuRef = useRef(null);

  const githubTokenKey = `oas_gh_token_${sessionId}`;

  const refreshGithubSyncStatus = () => {
    fetch(`/github/status/${encodeURIComponent(sessionId)}`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data && data.pushed) setGithubSyncData(data);
      })
      .catch(() => {});
  };

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError('');

    fetch(`/api/session/${sessionId}/suite`)
      .then(async (res) => {
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || err.detail || 'Session not found');
        }
        return res.json();
      })
      .then((payload) => {
        if (!active) return;
        setSuiteData(payload);
        const rawScenarios = payload.suite?.scenarios || payload.scenarios || [];
        const normalized = rawScenarios.map(normalizeScenario);
        setScenarios(normalized);

        const initialPayloads = {};
        const initialHealed = {};
        const initialStatus = {};
        const initialDetails = {};
        const execScenarios = payload.execution_results?.scenarios || {};

        normalized.forEach((scen) => {
          const savedScenRes = execScenarios[scen.id];
          const savedSteps = savedScenRes?.steps || {};
          (scen.steps || []).forEach((st) => {
            const key = `${scen.id}__${st.step_id}`;
            initialPayloads[key] = st;
            if (st.healed) initialHealed[key] = true;
            const savedSt = savedSteps[st.step_id];
            if (savedSt !== undefined && savedSt !== null) {
              // server.py persists either a bare status string ("SUCCESS" | "FAILED") or a result object
              const rawStatus = typeof savedSt === 'string' ? savedSt : savedSt.status || (savedSt.passed ? 'SUCCESS' : 'FAILED');
              const passed = String(rawStatus).toUpperCase() === 'SUCCESS';
              initialStatus[key] = passed ? 'passed' : 'failed';
              if (typeof savedSt === 'object') {
                initialDetails[key] = {
                  status_code: savedSt.status_code ?? savedSt.response_status,
                  url: savedSt.request_url || st.path,
                  expected_status: st.expected_status,
                  response: savedSt.body ?? savedSt.response_body,
                  error: savedSt.error,
                  diagnosis: savedSt.diagnosis
                };
              }
            }
          });
        });

        setStepPayloadsMap(initialPayloads);
        setStepHealedMap(initialHealed);
        setStepStatusMap(initialStatus);
        setStepDetailsMap(initialDetails);
        setLoading(false);
      })
      .catch((err) => {
        if (active) {
          setError(err.message);
          setLoading(false);
        }
      });

    refreshGithubSyncStatus();
    setGhToken(sessionStorage.getItem(githubTokenKey) || '');

    return () => {
      active = false;
    };
  }, [sessionId]);

  useEffect(() => {
    if (consoleRef.current && consoleVisible) {
      consoleRef.current.scrollTop = consoleRef.current.scrollHeight;
    }
  }, [consoleLogs, consoleVisible]);

  useEffect(() => {
    const handleClickOutside = (e) => {
      if (downloadMenuRef.current && !downloadMenuRef.current.contains(e.target)) setDownloadMenuOpen(false);
    };
    document.addEventListener('click', handleClickOutside);
    return () => document.removeEventListener('click', handleClickOutside);
  }, []);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return;
      setDownloadMenuOpen(false);
      if (githubModalOpen && !ghSubmitting) setGithubModalOpen(false);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [githubModalOpen, ghSubmitting]);

  const appendLog = (msg) => {
    const time = new Date().toLocaleTimeString();
    setConsoleLogs((prev) => [...prev, `[${time}] ${msg}`]);
  };

  // KPI & Journey Status derivation
  const metrics = useMemo(() => {
    let totalSteps = 0;
    let passedSteps = 0;
    let failedSteps = 0;
    let passedJourneys = 0;
    let failedJourneys = 0;
    let completedSteps = 0;
    const scenarioStates = {};

    scenarios.forEach((scen) => {
      const steps = scen.steps || [];
      totalSteps += steps.length;
      let scenPassed = 0;
      let scenFailed = 0;
      let scenRunning = !!runningScenarios[scen.id];

      steps.forEach((st) => {
        const stState = stepStatusMap[`${scen.id}__${st.step_id}`] || 'idle';
        if (stState === 'passed') {
          passedSteps += 1;
          scenPassed += 1;
          completedSteps += 1;
        } else if (stState === 'failed' || stState === 'skipped') {
          failedSteps += 1;
          scenFailed += 1;
          completedSteps += 1;
        } else if (stState === 'running' || stState === 'healing') {
          scenRunning = true;
        }
      });

      if (scenRunning) scenarioStates[scen.id] = 'running';
      else if (steps.length > 0 && scenPassed === steps.length) {
        scenarioStates[scen.id] = 'passed';
        passedJourneys += 1;
      } else if (scenFailed > 0) {
        scenarioStates[scen.id] = 'failed';
        failedJourneys += 1;
      } else scenarioStates[scen.id] = 'idle';
    });

    const totalJourneys = scenarios.length;
    return {
      totalJourneys,
      totalSteps,
      passedJourneys,
      passedSteps,
      failedJourneys,
      failedSteps,
      journeyPassRate: totalJourneys > 0 ? Math.round((passedJourneys / totalJourneys) * 100) : 0,
      stepPassRate: totalSteps > 0 ? Math.round((passedSteps / totalSteps) * 100) : 0,
      progressPct: totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0,
      scenarioStates
    };
  }, [scenarios, stepStatusMap, runningScenarios]);

  const getPassedStepsForScenarioMap = () => {
    const map = {};
    scenarios.forEach((scen) => {
      const passedIds = [];
      (scen.steps || []).forEach((st) => {
        if (stepStatusMap[`${scen.id}__${st.step_id}`] === 'passed') passedIds.push(st.step_id);
      });
      if (passedIds.length > 0) map[scen.id] = passedIds;
    });
    return map;
  };

  // Shared SSE consumer matching server.py's consumeSSE
  const consumeSSE = async (url, defaultScenId = null) => {
    const response = await fetch(url);
    if (!response.ok || !response.body) throw new Error(`Stream connection failed (${response.status})`);
    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split('\n\n');
      buffer = parts.pop() || '';
      for (const part of parts) {
        for (const rawLine of part.split('\n')) {
          const line = rawLine.trim();
          if (!line.startsWith('data:')) continue;
          const jsonStr = line.slice(5).trim();
          if (!jsonStr) continue;
          let ev;
          try {
            ev = JSON.parse(jsonStr);
          } catch (err) {
            console.error('Failed to parse SSE JSON:', err);
            continue;
          }
          handleStreamEvent(ev, defaultScenId);
        }
      }
    }
  };

  const handleStreamEvent = (ev, defaultScenId) => {
    const eventType = ev.event || ev.type;
    const scenId = ev.scenario_id || defaultScenId;
    if (ev.message) appendLog(ev.message);

    if ((eventType === 'JOURNEY_START' || eventType === 'SCENARIO_START') && scenId) {
      setRunningScenarios((prev) => ({ ...prev, [scenId]: true }));
      appendLog(`[JOURNEY_START] ${ev.name || scenId} (${ev.total_steps || 0} steps)`);
    } else if (eventType === 'STEP_START' && scenId && ev.step_id) {
      setStepStatusMap((prev) => ({ ...prev, [`${scenId}__${ev.step_id}`]: 'running' }));
      appendLog(`[STEP_START] ${scenId} -> ${ev.method || ''} ${ev.path_template || ev.step_id}`);
    } else if ((eventType === 'STEP_PROGRESS' || eventType === 'STEP_RESULT') && scenId && ev.step_id) {
      const key = `${scenId}__${ev.step_id}`;
      const passed = ev.status === 'SUCCESS' || ev.passed === true;
      setStepStatusMap((prev) => ({ ...prev, [key]: passed ? 'passed' : 'failed' }));
      setStepDetailsMap((prev) => ({
        ...prev,
        [key]: ev.details || {
          status_code: ev.status_code,
          url: ev.request_url || ev.path_template,
          response: ev.body,
          error: ev.error,
          diagnosis: ev.diagnosis
        }
      }));
      appendLog(`[${passed ? 'PASS' : 'FAIL'}] ${scenId} / ${ev.step_id} (${ev.method || ''} ${ev.path_template || ''}) -> HTTP ${ev.status_code ?? 'ERR'}`);
    } else if (eventType === 'HEALING_START' && scenId) {
      const targetStepId = ev.step_id || ev.target_step;
      if (targetStepId) setStepStatusMap((prev) => ({ ...prev, [`${scenId}__${targetStepId}`]: 'healing' }));
      setScenarioHealingNotes((prev) => ({
        ...prev,
        [scenId]: { tone: 'healing', text: `TestValidatorAgent is analyzing the failure on step ${targetStepId || ''}…` }
      }));
      appendLog(`[HEALING_START] Analyzing failure on ${scenId} / ${targetStepId || ''}...`);
    } else if (eventType === 'HEALING_APPLIED' && scenId) {
      const targetStepId = ev.target_step || ev.step_id;
      const fixedStep = ev.fixed_step || ev.updated_step;
      if (targetStepId) {
        const key = `${scenId}__${targetStepId}`;
        setStepHealedMap((prev) => ({ ...prev, [key]: true }));
        if (fixedStep) setStepPayloadsMap((prev) => ({ ...prev, [key]: fixedStep }));
      }
      setScenarioHealingNotes((prev) => ({
        ...prev,
        [scenId]: {
          tone: 'success',
          text: `Fix applied on ${targetStepId}: ${ev.reasoning || 'Updated step definition'}. Re-running verification…`
        }
      }));
      appendLog(`[HEALING_APPLIED] ${scenId} / ${targetStepId}: ${ev.reasoning || ''}`);
    } else if (eventType === 'HEALING_FAILED' && scenId) {
      setScenarioHealingNotes((prev) => ({
        ...prev,
        [scenId]: { tone: 'danger', text: `Diagnosis: ${ev.reasoning || 'No automated patch applied.'}` }
      }));
      appendLog(`[HEALING_FAILED] ${scenId}: ${ev.reasoning || ''}`);
    } else if ((eventType === 'JOURNEY_COMPLETE' || eventType === 'SCENARIO_END') && scenId) {
      if (ev.steps) {
        setStepStatusMap((prev) => {
          const next = { ...prev };
          Object.entries(ev.steps).forEach(([stId, stData]) => {
            const passed = stData.status === 'SUCCESS' || stData.passed === true;
            next[`${scenId}__${stId}`] = passed ? 'passed' : 'failed';
          });
          return next;
        });
        setStepDetailsMap((prev) => {
          const next = { ...prev };
          Object.entries(ev.steps).forEach(([stId, stData]) => {
            next[`${scenId}__${stId}`] = {
              status_code: stData.status_code,
              url: stData.request_url,
              response: stData.body,
              error: stData.error,
              diagnosis: stData.diagnosis
            };
          });
          return next;
        });
      }
      if (ev.healing) {
        if (ev.healing.applied) {
          setScenarioHealingNotes((prev) => ({
            ...prev,
            [scenId]: {
              tone: ev.healing.re_run_success ? 'success' : 'warning',
              text: `Fix applied on ${ev.healing.target_step || ''}: ${ev.healing.reasoning || ''}${ev.healing.re_run_success ? ' — re-run passed.' : ' — re-run still failed.'}`
            }
          }));
        } else if (ev.healing.attempted && ev.healing.reasoning) {
          setScenarioHealingNotes((prev) => ({
            ...prev,
            [scenId]: { tone: 'danger', text: `Diagnosis: ${ev.healing.reasoning}` }
          }));
        }
      }
      setRunningScenarios((prev) => {
        const next = { ...prev };
        delete next[scenId];
        return next;
      });
      appendLog(`[JOURNEY_COMPLETE] ${scenId} -> ${ev.status || 'DONE'}`);
    } else if (eventType === 'SUITE_COMPLETE') {
      appendLog(`[SUITE_COMPLETE] ${ev.passed ?? 0} passed, ${ev.failed ?? 0} failed (${ev.score ?? 0}% pass rate across ${ev.total ?? 0} journeys)`);
    }
  };

  const runSuite = async () => {
    if (isSuiteRunning) return;
    setIsSuiteRunning(true);
    const nextStatus = {};
    const nextRunningScens = {};
    scenarios.forEach((scen) => {
      nextRunningScens[scen.id] = true;
      (scen.steps || []).forEach((st) => {
        nextStatus[`${scen.id}__${st.step_id}`] = 'idle';
      });
    });
    setStepStatusMap(nextStatus);
    setStepDetailsMap({});
    setScenarioHealingNotes({});
    setRunningScenarios(nextRunningScens);
    appendLog(`Starting parallel suite execution (4x concurrency, Self-Healing: ${healingEnabled ? 'ON' : 'OFF'})...`);
    try {
      await consumeSSE(`/stream_suite/${encodeURIComponent(sessionId)}?healing=${healingEnabled}&concurrency=4`);
    } catch (err) {
      appendLog(`[ERROR] Suite stream interrupted: ${err.message}`);
    } finally {
      setIsSuiteRunning(false);
      setRunningScenarios({});
    }
  };

  const runScenario = async (scenId) => {
    if (runningScenarios[scenId] || isSuiteRunning) return;
    setRunningScenarios((prev) => ({ ...prev, [scenId]: true }));
    setScenarioHealingNotes((prev) => {
      const next = { ...prev };
      delete next[scenId];
      return next;
    });
    const scen = scenarios.find((s) => s.id === scenId);
    if (scen) {
      setStepStatusMap((prev) => {
        const next = { ...prev };
        (scen.steps || []).forEach((st) => {
          next[`${scenId}__${st.step_id}`] = 'idle';
        });
        return next;
      });
    }
    appendLog(`Running Journey: ${scen?.title || scenId}...`);
    try {
      await consumeSSE(`/stream_scenario/${encodeURIComponent(sessionId)}/${encodeURIComponent(scenId)}?healing=${healingEnabled}`, scenId);
    } catch (err) {
      appendLog(`[ERROR] Journey stream failed: ${err.message}`);
    } finally {
      setRunningScenarios((prev) => {
        const next = { ...prev };
        delete next[scenId];
        return next;
      });
    }
  };

  const executeSingleStep = async (scenId, stepId) => {
    const key = `${scenId}__${stepId}`;
    if (runningSteps[key]) return;
    setRunningSteps((prev) => ({ ...prev, [key]: true }));
    setStepStatusMap((prev) => ({ ...prev, [key]: 'running' }));
    appendLog(`[STEP_START] Executing single step: ${scenId} / ${stepId}...`);
    try {
      const res = await fetch(
        `/execute_single/${encodeURIComponent(sessionId)}/step/${encodeURIComponent(scenId)}/${encodeURIComponent(stepId)}?healing=${healingEnabled}`,
        { method: 'POST' }
      );
      const data = await res.json();
      const passed = data.status === 'SUCCESS' || data.passed === true;
      setStepStatusMap((prev) => ({ ...prev, [key]: passed ? 'passed' : 'failed' }));
      setStepDetailsMap((prev) => ({
        ...prev,
        [key]: data.details || {
          status_code: data.response_status ?? data.status_code,
          url: data.request_url,
          response: data.response_body ?? data.body,
          error: data.error,
          diagnosis: data.healing?.reasoning
        }
      }));
      setOpenResponseSteps((prev) => ({ ...prev, [key]: true }));
      if ((data.healing && data.healing.applied) || data.healed) {
        setStepHealedMap((prev) => ({ ...prev, [key]: true }));
        appendLog(`[HEALING_APPLIED] Fixed step ${stepId}: ${data.healing?.reasoning || ''}`);
      }
      if (data.fixed_step || data.updated_step) {
        setStepPayloadsMap((prev) => ({ ...prev, [key]: data.fixed_step || data.updated_step }));
      }
      appendLog(`[${passed ? 'STEP_SUCCESS' : 'STEP_FAILED'}] ${stepId} -> ${data.response_status ?? data.error ?? ''}`);
    } catch (err) {
      appendLog(`[ERROR] Step execution failed: ${err.message}`);
      setStepStatusMap((prev) => ({ ...prev, [key]: 'failed' }));
    } finally {
      setRunningSteps((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    }
  };

  const downloadTests = async (scope) => {
    setDownloadMenuOpen(false);
    const passedStepsMap = getPassedStepsForScenarioMap();
    if (scope === 'successful' && Object.keys(passedStepsMap).length === 0) {
      alert('Run at least one passing test before downloading successful tests.');
      return;
    }
    try {
      const res = await fetch(`/download_tests/${encodeURIComponent(sessionId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope, passed_steps: scope === 'successful' ? passedStepsMap : null })
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Unable to download test file.');
      }
      const blob = await res.blob();
      const disposition = res.headers.get('Content-Disposition') || '';
      const match = disposition.match(/filename="?([^"]+)"?/i);
      const filename = match
        ? match[1]
        : scope === 'successful'
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
      alert(err.message);
    }
  };

  const openGithubModal = async () => {
    setGhStatusBanner(null);
    const now = new Date();
    const pad = (n) => String(n).padStart(2, '0');
    const ts = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}_${pad(now.getHours())}-${pad(now.getMinutes())}-${pad(now.getSeconds())}`;
    setGhFolder(`tests/oas-tests-agent/${ts}`);
    setGhBranch(`oas-tests-agent/${ts}`);
    setGithubModalOpen(true);
    try {
      const cfgRes = await fetch(`/github/config?session_id=${encodeURIComponent(sessionId)}`);
      if (cfgRes.ok) {
        const cfg = await cfgRes.json();
        setGhRepo((prev) => prev || cfg.repo || suiteData?.default_repo || '');
      }
    } catch (err) {
      console.error('Failed to load GitHub config:', err);
    }
  };

  const submitGithubPush = async (e) => {
    e.preventDefault();
    const passedStepsMap = getPassedStepsForScenarioMap();
    if (ghScope === 'successful' && metrics.passedSteps === 0) {
      setGhStatusBanner({ tone: 'danger', text: 'No successful tests to commit yet. Run journeys first or select "Commit all tests".' });
      return;
    }
    if (!ghToken.trim()) {
      setGhStatusBanner({ tone: 'danger', text: 'GitHub token required. Enter a Personal Access Token (ghp_… or github_pat_…) before pushing.' });
      return;
    }
    sessionStorage.setItem(githubTokenKey, ghToken.trim());
    setGhSubmitting(true);
    setGhStatusBanner({
      tone: 'info',
      text: `Committing and pushing ${ghScope === 'successful' ? `successful tests (${metrics.passedSteps} passed)` : 'all generated tests'} to GitHub (${ghFolder})…`
    });
    try {
      const res = await fetch(`/github/push/${encodeURIComponent(sessionId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          repo: ghRepo.trim(),
          branch: ghBranch.trim(),
          token: ghToken.trim(),
          folder: ghFolder.trim(),
          create_pr: ghMode === 'pr',
          spec_file: suiteData?.spec_file || 'openapi.yaml',
          test_scope: ghScope,
          passed_steps: passedStepsMap
        })
      });
      const data = await res.json().catch(() => ({}));
      if (data.status === 'success') {
        setGhStatusBanner({
          tone: 'success',
          text:
            data.type === 'pr'
              ? `Pull request created on GitHub (${data.branch}).`
              : data.type === 'pr_compare'
              ? `Branch ${data.branch} pushed. Open the pre-filled pull request to finish.`
              : `Successfully pushed tests to ${data.branch} (${data.folder}).`,
          url: data.url,
          commitUrl: data.commit_url
        });
        refreshGithubSyncStatus();
      } else {
        throw new Error(data.message || data.detail || 'GitHub push failed.');
      }
    } catch (err) {
      setGhStatusBanner({ tone: 'danger', text: err.message || 'GitHub push failed.' });
    } finally {
      setGhSubmitting(false);
    }
  };

  /* ------------------------------------------------------------------ */

  if (loading) {
    return (
      <div className="card loading-state">
        <span className="spinner" /> Loading execution dashboard…
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
        <p>The session could not be loaded. Generate a new suite or pick another session.</p>
        <div className="empty__actions">
          <button type="button" className="btn btn-primary" onClick={() => navigate('/')}>
            <Icon name="arrow-left" /> Back to Generator
          </button>
        </div>
      </div>
    );
  }

  const progressTone = metrics.progressPct === 100 ? (metrics.failedSteps > 0 ? 'is-warning' : 'is-success') : '';

  return (
    <div>
      {/* Sticky toolbar */}
      <div className="toolbar glass">
        <div className="toolbar__row">
          <div className="toolbar__meta">
            <h2 style={{ fontSize: 16 }}>{suiteData?.api_title || suiteData?.title || 'API Test Suite'}</h2>
            {suiteData?.base_url && (
              <span className="chip" title={suiteData.base_url}>
                <Icon name="server" />
                <code>{suiteData.base_url}</code>
              </span>
            )}
            {githubSyncData?.pushed && (
              <a className="pill pill--success" href={githubSyncData.url || '#'} target="_blank" rel="noreferrer" title="View on GitHub">
                <Icon name="github" /> Pushed to {githubSyncData.branch || 'GitHub'}
                <Icon name="arrow-up-right" />
              </a>
            )}
          </div>

          <div className="toolbar__actions">
            <label className={`switch ${healingEnabled ? 'is-on' : ''}`} title="Toggle AI self-healing for failed steps">
              <input type="checkbox" checked={healingEnabled} onChange={(e) => setHealingEnabled(e.target.checked)} />
              <span className="switch__track" aria-hidden="true" />
              <Icon name="wand" />
              <span>Self-healing</span>
            </label>

            <button type="button" className="btn btn-ghost btn-sm" onClick={() => setConsoleVisible((v) => !v)} aria-pressed={consoleVisible}>
              <Icon name="terminal" />
              {consoleVisible ? 'Hide logs' : 'Show logs'}
            </button>

            <div ref={downloadMenuRef} className="dropdown">
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => setDownloadMenuOpen((o) => !o)}
                aria-haspopup="menu"
                aria-expanded={downloadMenuOpen}
              >
                <Icon name="download" />
                Download
                <Icon name="chevron-down" />
              </button>
              {downloadMenuOpen && (
                <div className="dropdown__menu glass" role="menu">
                  <button type="button" className="dropdown__item" role="menuitem" onClick={() => downloadTests('all')}>
                    <Icon name="file" />
                    <span>
                      All tests (.py)
                      <small>Every generated journey step</small>
                    </span>
                  </button>
                  <button type="button" className="dropdown__item" role="menuitem" onClick={() => downloadTests('successful')}>
                    <Icon name="check-circle" />
                    <span>
                      Successful tests (.py)
                      <small>Only steps that passed in this session</small>
                    </span>
                  </button>
                </div>
              )}
            </div>

            <button type="button" className="btn btn-secondary btn-sm" onClick={openGithubModal}>
              <Icon name="github" />
              {githubSyncData?.pushed ? 'Push updates' : 'Push to GitHub'}
            </button>

            <button type="button" className="btn btn-primary btn-sm" disabled={isSuiteRunning} onClick={runSuite} id="run-all-journeys-btn">
              {isSuiteRunning ? <span className="spinner" /> : <Icon name="play" />}
              {isSuiteRunning ? 'Running 4× parallel…' : 'Run all journeys'}
            </button>
          </div>
        </div>

        <div className="toolbar__progress" aria-label="Suite progress">
          <div className="progress">
            <div className={`progress__bar ${progressTone}`} style={{ width: `${metrics.progressPct}%` }} />
          </div>
          <span className="toolbar__pct">{metrics.progressPct}%</span>
        </div>
      </div>

      {/* KPIs */}
      <div className="stats" style={{ marginBottom: 16 }}>
        <div className="stat">
          <div className="stat__label">Journeys</div>
          <div className="stat__value">{metrics.totalJourneys}</div>
          <div className="stat__caption">End-to-end API flows</div>
        </div>
        <div className="stat stat--primary">
          <div className="stat__label">Tests</div>
          <div className="stat__value">{metrics.totalSteps}</div>
          <div className="stat__caption">HTTP step assertions</div>
        </div>
        <div className="stat stat--success">
          <div className="stat__label">Passed</div>
          <div className="stat__value">
            {metrics.passedJourneys} <small>journeys</small>
          </div>
          <div className="stat__caption">{metrics.passedSteps} tests passed</div>
        </div>
        <div className="stat stat--danger">
          <div className="stat__label">Failed</div>
          <div className="stat__value">
            {metrics.failedJourneys} <small>journeys</small>
          </div>
          <div className="stat__caption">{metrics.failedSteps} tests failed</div>
        </div>
        <div className="stat stat--heal">
          <div className="stat__label">Pass rate</div>
          <div className="stat__value">{metrics.journeyPassRate}%</div>
          <div className="stat__caption">{metrics.stepPassRate}% of individual tests</div>
        </div>
      </div>

      {/* Console */}
      {consoleVisible && (
        <div className="console" style={{ marginBottom: 16 }}>
          <div className="console__header">
            <span>Execution logs</span>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="faint tabular" style={{ textTransform: 'none', letterSpacing: 0 }}>
                {consoleLogs.length} lines
              </span>
              <button
                type="button"
                className="btn-icon"
                onClick={() => setConsoleLogs(['// Logs cleared.'])}
                aria-label="Clear logs"
                title="Clear logs"
              >
                <Icon name="trash" />
              </button>
            </div>
          </div>
          <div className="console__body" ref={consoleRef}>
            {consoleLogs.map((log, i) => {
              const tone = logTone(log);
              return (
                <span key={i} className={`log-line ${tone ? `log-line--${tone}` : ''}`}>
                  {log}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {/* Journeys */}
      <div className="journeys">
        {scenarios.map((scen, idx) => {
          const scenState = metrics.scenarioStates[scen.id] || 'idle';
          const isScenRunning = !!runningScenarios[scen.id];
          const healingNote = scenarioHealingNotes[scen.id];
          const scenType = scen.scenario_type || scen.type || '';

          return (
            <section key={scen.id} className="card journey" data-state={scenState} style={{ '--i': idx }}>
              <div className="journey__header">
                <div className="journey__lead">
                  <StatusIcon state={scenState} size={20} />
                  <div style={{ minWidth: 0 }}>
                    <div className="journey__title">
                      <span className="journey__name">
                        {idx + 1}. {scen.title}
                      </span>
                      {scenType && <span className={`badge badge-neutral type-${scenType}`}>{String(scenType).replace(/_/g, ' ')}</span>}
                      <span className="pill">{(scen.steps || []).length} steps</span>
                    </div>
                    {scen.description && <div className="journey__desc">{scen.description}</div>}
                  </div>
                </div>

                <button
                  type="button"
                  className="btn btn-secondary btn-sm"
                  disabled={isScenRunning || isSuiteRunning}
                  onClick={() => runScenario(scen.id)}
                >
                  {isScenRunning ? <span className="spinner" /> : <Icon name="play" />}
                  {isScenRunning ? 'Running…' : 'Run journey'}
                </button>
              </div>

              <div className="journey__body">
                {healingNote && (
                  <div
                    className={`alert journey__note ${
                      healingNote.tone === 'success' ? 'alert-success' : healingNote.tone === 'danger' ? 'alert-danger' : healingNote.tone === 'warning' ? 'alert-warning' : 'alert-heal'
                    }`}
                  >
                    <Icon name={healingNote.tone === 'healing' ? 'bot' : 'wand'} />
                    <div>
                      <strong>Self-healing.</strong> {healingNote.text}
                    </div>
                  </div>
                )}

                <div className="steps-list">
                  {(scen.steps || []).map((st, stIdx) => {
                    const key = `${scen.id}__${st.step_id}`;
                    const stState = stepStatusMap[key] || 'idle';
                    const stDetails = stepDetailsMap[key];
                    const isHealed = !!stepHealedMap[key];
                    const payload = stepPayloadsMap[key] || st;
                    const reqOpen = !!openRequestSteps[key];
                    const resOpen = !!openResponseSteps[key];
                    const isStepRunning = !!runningSteps[key];
                    const expected = payload.expected_status_code || payload.expected_status || st.expected_status;

                    return (
                      <div key={st.step_id} className="step" data-state={stState}>
                        <div className="step__row">
                          <StatusIcon state={stState} size={18} />
                          <span className="step__idx">{stIdx + 1}</span>
                          <div className="step__main">
                            <span className={`method method-${(st.method || 'GET').toUpperCase()}`}>{st.method}</span>
                            <span className="step__path">{st.path}</span>
                            <span className="step__expect" title="Expected status">
                              → {expected}
                            </span>
                            {st.operation_id && <span className="step__op">{st.operation_id}</span>}
                            {st._phase && st._phase !== 'test' && <span className="step__phase">{st._phase}</span>}
                            {isHealed && (
                              <span className="badge badge-heal" title="Patched by TestValidatorAgent">
                                <Icon name="sparkles" /> Healed
                              </span>
                            )}
                            {stState === 'healing' && <span className="badge badge-heal">AI healing…</span>}
                          </div>

                          <div className="step__actions">
                            <button
                              type="button"
                              className="btn btn-ghost btn-xs"
                              aria-expanded={reqOpen}
                              onClick={() => setOpenRequestSteps((prev) => ({ ...prev, [key]: !prev[key] }))}
                            >
                              <Icon name="code" /> Request
                            </button>
                            {stDetails && (
                              <button
                                type="button"
                                className="btn btn-ghost btn-xs"
                                aria-expanded={resOpen}
                                onClick={() => setOpenResponseSteps((prev) => ({ ...prev, [key]: !prev[key] }))}
                              >
                                <Icon name="terminal" /> Response
                                <code style={{ fontSize: 11 }}>{stDetails.status_code || (stDetails.skipped ? 'SKIP' : 'ERR')}</code>
                              </button>
                            )}
                            <button
                              type="button"
                              className="btn btn-secondary btn-xs"
                              disabled={isStepRunning || isScenRunning || isSuiteRunning}
                              onClick={() => executeSingleStep(scen.id, st.step_id)}
                            >
                              {isStepRunning ? <span className="spinner" /> : <Icon name="play" />} Run
                            </button>
                          </div>
                        </div>

                        {reqOpen && (
                          <pre className={`step__drawer ${isHealed ? 'is-healed' : ''}`}>
                            <span className="step__drawer-title">
                              <Icon name="code" /> Request definition{isHealed ? ' · healed' : ''}
                            </span>
                            {JSON.stringify(requestPreview(payload), null, 2)}
                          </pre>
                        )}

                        {resOpen && stDetails && (
                          <pre className={`step__drawer ${stState === 'failed' ? 'step__drawer--fail' : ''}`}>
                            <span className="step__drawer-title">
                              <Icon name="terminal" /> Response
                            </span>
                            {stDetails.skipped ? (
                              <span className="log-line log-line--warn">SKIPPED: {stDetails.reason || 'Prior step in journey failed'}</span>
                            ) : (
                              <>
                                {stDetails.diagnosis && <span className="log-line log-line--heal">[Diagnosis]: {stDetails.diagnosis}{'\n'}</span>}
                                <span className="log-line log-line--info">
                                  {stDetails.url || st.path} → HTTP {stDetails.status_code}
                                </span>
                                {stDetails.error && <span className="log-line log-line--fail">Error: {stDetails.error}</span>}
                                <span className="log-line">
                                  {typeof stDetails.response === 'object' ? JSON.stringify(stDetails.response, null, 2) : String(stDetails.response ?? '')}
                                </span>
                              </>
                            )}
                          </pre>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            </section>
          );
        })}
      </div>

      {/* GitHub Push Modal — portalled to <body> so no ancestor transform/filter can become its containing block */}
      {githubModalOpen &&
        createPortal(
        <div
          className="modal-backdrop"
          onClick={(e) => {
            if (e.target === e.currentTarget && !ghSubmitting) setGithubModalOpen(false);
          }}
        >
          <div className="modal glass" role="dialog" aria-modal="true" aria-labelledby="gh-modal-title">
            <div className="modal__header">
              <div className="modal__title" id="gh-modal-title">
                <Icon name="github" /> Push generated tests to GitHub
              </div>
              <button type="button" className="btn-icon" onClick={() => setGithubModalOpen(false)} aria-label="Close dialog">
                <Icon name="x" />
              </button>
            </div>

            <form onSubmit={submitGithubPush}>
              <div className="modal__body">
                <div className="field">
                  <label className="field__label" htmlFor="gh-repo">
                    Target repository <span className="req">*</span>
                  </label>
                  <input id="gh-repo" type="text" className="input input--mono" placeholder="owner/repo" value={ghRepo} onChange={(e) => setGhRepo(e.target.value)} required />
                </div>

                <div className="grid-2">
                  <div className="field">
                    <label className="field__label" htmlFor="gh-branch">
                      Branch <span className="req">*</span>
                    </label>
                    <input id="gh-branch" type="text" className="input input--mono" value={ghBranch} onChange={(e) => setGhBranch(e.target.value)} required />
                  </div>
                  <div className="field">
                    <label className="field__label" htmlFor="gh-folder">
                      Destination folder <span className="req">*</span>
                    </label>
                    <input id="gh-folder" type="text" className="input input--mono" value={ghFolder} onChange={(e) => setGhFolder(e.target.value)} required />
                  </div>
                </div>

                <div className="field">
                  <span className="field__label">Push mode</span>
                  <div className="scope-choice">
                    <label className={`scope-choice__option ${ghMode === 'commit' ? 'is-selected' : ''}`}>
                      <input type="radio" name="gh-mode" value="commit" checked={ghMode === 'commit'} onChange={() => setGhMode('commit')} />
                      <span className="scope-choice__title">
                        <Icon name="git-commit" /> Commit to branch
                      </span>
                      <span className="scope-choice__desc">Push files directly to the target branch.</span>
                    </label>
                    <label className={`scope-choice__option ${ghMode === 'pr' ? 'is-selected' : ''}`}>
                      <input type="radio" name="gh-mode" value="pr" checked={ghMode === 'pr'} onChange={() => setGhMode('pr')} />
                      <span className="scope-choice__title">
                        <Icon name="pull-request" /> Open pull request
                      </span>
                      <span className="scope-choice__desc">Push the branch and create a PR for review.</span>
                    </label>
                  </div>
                </div>

                <div className="field">
                  <span className="field__label">Test scope</span>
                  <div className="scope-choice">
                    <label className={`scope-choice__option ${ghScope === 'all' ? 'is-selected' : ''}`}>
                      <input type="radio" name="gh-scope" value="all" checked={ghScope === 'all'} onChange={() => setGhScope('all')} />
                      <span className="scope-choice__title">
                        <Icon name="layers" /> All tests
                      </span>
                      <span className="scope-choice__desc">{metrics.totalSteps} generated steps.</span>
                    </label>
                    <label className={`scope-choice__option ${ghScope === 'successful' ? 'is-selected' : ''}`}>
                      <input type="radio" name="gh-scope" value="successful" checked={ghScope === 'successful'} onChange={() => setGhScope('successful')} />
                      <span className="scope-choice__title">
                        <Icon name="check-circle" /> Successful only
                      </span>
                      <span className="scope-choice__desc">{metrics.passedSteps} passed in this session.</span>
                    </label>
                  </div>
                </div>

                <div className="field">
                  <label className="field__label" htmlFor="gh-token">
                    Personal access token <span className="req">*</span>
                  </label>
                  <input
                    id="gh-token"
                    type="password"
                    className="input input--mono"
                    placeholder="ghp_… or github_pat_…"
                    value={ghToken}
                    onChange={(e) => setGhToken(e.target.value)}
                    autoComplete="off"
                    required
                  />
                  <span className={`field__hint ${ghToken.trim() ? 'is-success' : ''}`}>
                    {ghToken.trim()
                      ? `Kept in this browser tab's sessionStorage for ${sessionLabel(sessionId)} only.`
                      : `Needs Contents: Read & write${ghMode === 'pr' ? ' and Pull requests: Read & write' : ''}. Never persisted server-side.`}
                  </span>
                </div>

                {ghStatusBanner && (
                  <div className={`alert alert-${ghStatusBanner.tone}`} role="status">
                    <Icon name={ghStatusBanner.tone === 'success' ? 'check-circle' : ghStatusBanner.tone === 'danger' ? 'alert' : 'loader'} />
                    <div>
                      {ghStatusBanner.text}
                      {ghStatusBanner.url && (
                        <div style={{ marginTop: 8, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                          <a className="btn btn-secondary btn-sm" href={ghStatusBanner.url} target="_blank" rel="noreferrer">
                            Open on GitHub <Icon name="arrow-up-right" />
                          </a>
                          {ghStatusBanner.commitUrl && ghStatusBanner.commitUrl !== ghStatusBanner.url && (
                            <a className="btn btn-ghost btn-sm" href={ghStatusBanner.commitUrl} target="_blank" rel="noreferrer">
                              View commit <Icon name="arrow-up-right" />
                            </a>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>

              <div className="modal__footer">
                <button type="button" className="btn btn-ghost" onClick={() => setGithubModalOpen(false)} disabled={ghSubmitting}>
                  Close
                </button>
                <button type="submit" className="btn btn-primary" disabled={ghSubmitting}>
                  {ghSubmitting ? <span className="spinner" /> : <Icon name="github" />}
                  {ghSubmitting ? 'Pushing…' : ghMode === 'pr' ? 'Push & open PR' : 'Push to GitHub'}
                </button>
              </div>
            </form>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
