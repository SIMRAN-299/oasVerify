import React, { useState, useEffect, useMemo, useRef } from 'react';
import { Icon, StatusIcon } from './Icon';

/* --------------------------------------------------------------------------
   Small building blocks
   -------------------------------------------------------------------------- */
function CodeBlock({ title, code }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch (err) {
      console.error('Copy failed:', err);
    }
  };
  return (
    <div className="doc-code">
      <div className="doc-code__bar">
        <span>{title}</span>
        <button type="button" className="btn-icon" onClick={copy} aria-label={copied ? 'Copied' : 'Copy to clipboard'} title="Copy">
          <Icon name={copied ? 'check' : 'copy'} />
        </button>
      </div>
      <pre>{code}</pre>
    </div>
  );
}

function Faq({ q, children }) {
  return (
    <details className="faq">
      <summary>
        <Icon name="help-circle" />
        <span>{q}</span>
        <Icon name="chevron-down" />
      </summary>
      <div className="faq__body">{children}</div>
    </details>
  );
}

const TEST_CONFIG_EXAMPLE = `{
  "global": {
    "path_parameters": { "zoneId": "ILZ014", "stationId": "KORD" },
    "query_parameters": { "limit": 10 },
    "payload_overrides": { "status": "active" }
  },
  "endpoints": {
    "/v1/products/{id}": {
      "path_parameters": { "id": "prod_12345" }
    }
  }
}`;

const RUN_LOCALLY = `pip install pytest httpx

# run everything
pytest test_suite.py -v

# point the suite at another environment
API_BASE_URL="https://staging.example.com" pytest test_suite.py -v

# slow down pacing between dependent steps (default 0.25s)
OAS_STEP_DELAY_SECONDS=0.5 pytest test_suite.py -v

# pass credentials when the spec declares a security scheme
API_AUTH_TOKEN="<bearer token>" pytest test_suite.py -v   # Authorization header
API_KEY="<key>" pytest test_suite.py -v                   # apiKey header schemes`;

const CI_SNIPPET = `name: OAS journey tests
on: [push, pull_request]
jobs:
  api-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install pytest httpx
      - run: pytest tests/oas-tests-agent/**/test_suite.py -v
        env:
          API_BASE_URL: \${{ vars.API_BASE_URL }}
          API_AUTH_TOKEN: \${{ secrets.API_AUTH_TOKEN }}`;

/* --------------------------------------------------------------------------
   Sections — `keywords` power the client-side search
   -------------------------------------------------------------------------- */
function buildSections({ navigate, activeSessionId }) {
  const dash = activeSessionId ? `/execution_dashboard/${activeSessionId}` : null;
  return [
    {
      id: 'quick-start',
      icon: 'rocket',
      title: 'Quick start',
      blurb: 'From spec to passing tests in three steps.',
      keywords: 'start begin first run upload generate dashboard export quick',
      body: (
        <>
          <ol className="doc-steps">
            <li>
              <strong>Generate a suite</strong>
              Open the Generator, drop an OpenAPI 3.x / Swagger 2.0 file (<code>.yaml</code>, <code>.yml</code>, <code>.json</code>) or pick a
              bundled sample, then click <em>Generate test suite</em>. Generation usually takes 20–60 seconds.
            </li>
            <li>
              <strong>Run it against the live API</strong>
              In the Execution Dashboard click <em>Run all journeys</em>. Journeys stream 4× in parallel and failing steps are
              repaired by AI self-healing while you watch.
            </li>
            <li>
              <strong>Export what passed</strong>
              Download a standalone PyTest file (all tests or only the successful ones) or push it to a GitHub branch / pull
              request from the same toolbar.
            </li>
          </ol>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button type="button" className="btn btn-primary btn-sm" onClick={() => navigate('/')}>
              <Icon name="rocket" /> Open Generator
            </button>
            {dash && (
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => navigate(dash)}>
                <Icon name="play" /> Open current dashboard
              </button>
            )}
          </div>
        </>
      )
    },
    {
      id: 'generator',
      icon: 'upload',
      title: 'Generating a test suite',
      blurb: 'What happens between upload and “Completed”.',
      keywords: 'generator upload spec openapi swagger sample ecommerce weather pipeline gemini cluster baseline quality gate logs session resume',
      body: (
        <>
          <p>
            Uploading starts a background session. The status card polls every 1.5&nbsp;s and the session id is kept in your
            browser, so you can close the tab and the Generator will resume it on your next visit.
          </p>
          <h3>Pipeline stages</h3>
          <ol>
            <li>
              <strong>Parse &amp; resolve</strong> — the spec is dereferenced (all <code>$ref</code>s inlined) and every operation is
              catalogued with its parameters, request body and declared responses.
            </li>
            <li>
              <strong>Domain clustering</strong> — endpoints are partitioned into cohesive domains (e.g. <em>products</em>,{' '}
              <em>alerts</em>, <em>stations</em>) so each journey stays focused and dependency chains are short.
            </li>
            <li>
              <strong>Journey synthesis</strong> — per cluster, Gemini designs stateful journeys (create → read → update → delete,
              boundary/negative cases) while a deterministic baseline guarantees coverage even without AI credentials.
              Guardrails sanitise the output: collection endpoints never expect 404, happy paths must expect 2xx, and
              negative inputs are left intact.
            </li>
            <li>
              <strong>Quality gates</strong> — the generated PyTest module is formatted, linted and dry-run collected by pytest
              before the session is marked <span className="badge badge-success">Completed</span>.
            </li>
          </ol>
          <div className="alert alert-info">
            <Icon name="info" />
            <div>
              Use <strong>Show generation logs</strong> on the status card to follow each stage. A failed session keeps its logs
              open so you can see which stage rejected the spec.
            </div>
          </div>
        </>
      )
    },
    {
      id: 'test-config',
      icon: 'file-json',
      title: 'Execution config (test_config.json)',
      blurb: 'Seed known-good IDs and parameter overrides.',
      keywords: 'config test_config json path parameters query overrides payload seed ids global endpoints',
      body: (
        <>
          <p>
            Optional. Attach a <code>test_config.json</code> in step 2 of the Generator to seed values the generator could not
            infer from the spec — real entity ids, required query parameters, or payload fields that must take a specific
            value. Seeds are applied globally or per endpoint path.
          </p>
          <CodeBlock title="test_config.json" code={TEST_CONFIG_EXAMPLE} />
          <ul>
            <li>
              <code>global.path_parameters</code> / <code>global.query_parameters</code> apply to every operation that declares a
              parameter with that name.
            </li>
            <li>
              <code>endpoints["/path"]</code> entries override the global seeds for that exact path template.
            </li>
            <li>
              <code>payload_overrides</code> forces values inside generated request bodies (handy for enum fields such as{' '}
              <code>status</code>).
            </li>
          </ul>
          <p>
            The target base URL comes from the specification&apos;s <code>servers</code> block. To run against another
            environment set <code>API_BASE_URL</code> when executing the exported suite (see <a href="#export">Export</a>).
          </p>
        </>
      )
    },
    {
      id: 'dashboard',
      icon: 'activity',
      title: 'Execution Dashboard',
      blurb: 'Run the suite, a journey, or a single step — live.',
      keywords: 'dashboard run all journeys step request response logs console status icons kpi pass rate parallel concurrency',
      body: (
        <>
          <h3>Three ways to run</h3>
          <ul>
            <li>
              <strong>Run all journeys</strong> — streams the whole suite with 4 journeys in flight at once. The toolbar progress
              bar tracks completed steps; KPIs update as results arrive.
            </li>
            <li>
              <strong>Run journey</strong> — executes one journey end to end. Steps run in order and later steps are skipped if an
              earlier dependency fails.
            </li>
            <li>
              <strong>Run</strong> (per step) — fires a single request using the values captured so far. Useful to retry one
              call after fixing data on the server.
            </li>
          </ul>
          <h3>Reading the status icons</h3>
          <div className="doc-legend">
            <div className="doc-legend__item">
              <StatusIcon state="idle" />
              <div>
                <strong>Not run</strong> <span>— no result yet</span>
              </div>
            </div>
            <div className="doc-legend__item">
              <StatusIcon state="running" />
              <div>
                <strong>Running</strong> <span>— request in flight</span>
              </div>
            </div>
            <div className="doc-legend__item">
              <StatusIcon state="healing" />
              <div>
                <strong>Self-healing</strong> <span>— agent is patching the step</span>
              </div>
            </div>
            <div className="doc-legend__item">
              <StatusIcon state="passed" />
              <div>
                <strong>Passed</strong> <span>— status and assertions matched</span>
              </div>
            </div>
            <div className="doc-legend__item">
              <StatusIcon state="failed" />
              <div>
                <strong>Failed</strong> <span>— open <em>Response</em> for the diagnosis</span>
              </div>
            </div>
            <div className="doc-legend__item">
              <StatusIcon state="skipped" />
              <div>
                <strong>Skipped</strong> <span>— a prerequisite step failed</span>
              </div>
            </div>
          </div>
          <h3>Inspecting a step</h3>
          <ul>
            <li>
              <strong>Request</strong> shows the step definition: method, path template, headers, parameters, body and the
              expected status. After a heal it displays the patched version with a teal marker.
            </li>
            <li>
              <strong>Response</strong> shows the resolved URL, HTTP status, body and — when healing ran — the agent&apos;s
              diagnosis.
            </li>
            <li>
              <strong>Show logs</strong> opens the execution console: a timestamped, colour-coded stream of every event
              (journey start, step results, healing, suite summary).
            </li>
          </ul>
          <div className="alert alert-note">
            <Icon name="lightbulb" />
            <div>
              <strong>KPIs count two things.</strong> <em>Journeys</em> are end-to-end flows; <em>Tests</em> are individual HTTP
              steps (setup, test and teardown). A journey only counts as passed when every one of its steps passes.
            </div>
          </div>
        </>
      )
    },
    {
      id: 'self-healing',
      icon: 'wand',
      title: 'AI self-healing',
      blurb: 'How failures are diagnosed and repaired in place.',
      keywords: 'self healing heal ai gemini validator agent diagnosis patch retry 404 fix toggle disable',
      body: (
        <>
          <p>
            With the <strong>Self-healing</strong> switch on, a failed step hands its request, response and the relevant part of
            the spec to the TestValidatorAgent. The agent proposes a minimal patch, the step is re-run, and the journey
            continues. Healed steps get a <span className="badge badge-heal">Healed</span> badge and the journey card shows the
            reasoning.
          </p>
          <h3>What it will fix</h3>
          <ul>
            <li>
              <strong>Wrong identifiers</strong> — a 404 on <code>/stations/{'{stationId}'}</code> is retried with an id captured
              from an earlier step, or with a value that satisfies the parameter&apos;s regex / enum.
            </li>
            <li>
              <strong>Sibling parameters</strong> — when one path parameter is corrected, related query parameters are aligned
              with it.
            </li>
            <li>
              <strong>Expected status drift</strong> — the expected code is adjusted when the server&apos;s documented and actual
              behaviour differ for a legitimate reason.
            </li>
          </ul>
          <h3>What it refuses to do</h3>
          <ul>
            <li>It never weakens a happy-path expectation down to a 4xx just to make a test green.</li>
            <li>It never mutates the deliberately invalid inputs of boundary / negative tests.</li>
            <li>It never clears assertions on validation errors — instead it records a descriptive diagnosis.</li>
          </ul>
          <p>
            Turn the switch off for a strict, unmodified run. Heals are persisted with the session, so the Visualizer and the
            exported suite both reflect the repaired steps.
          </p>
        </>
      )
    },
    {
      id: 'visualize',
      icon: 'compass',
      title: 'Visualize: provenance & flowcharts',
      blurb: 'See where each journey came from and how data flows.',
      keywords: 'visualize provenance mermaid flowchart dag dependency gemini baseline template guardrail adjustments legend',
      body: (
        <>
          <p>
            Each journey card pairs a dependency flowchart with its provenance. Solid arrows are execution order; dotted,
            labelled arrows show a value captured in one step (for example <code>products_id</code>) being consumed by a later
            one.
          </p>
          <ul>
            <li>
              <strong>Designed by Gemini</strong> — the journey was synthesised by the model from the spec and domain context.
            </li>
            <li>
              <strong>Deterministic baseline</strong> — rule-based CRUD coverage generated without AI, also used as a fallback when
              credentials are missing.
            </li>
            <li>
              <strong>Guardrail adjustments</strong> — edits the sanitiser made to a generated step (e.g. forcing a 2xx expectation on
              a happy path).
            </li>
            <li>
              <strong>Runtime self-heals</strong> — patches applied during execution, listed with the step id and reasoning.
            </li>
          </ul>
          <p>
            The summary table breaks the spec down by domain cluster so you can compare how many journeys the baseline, the raw
            model output and the final suite produced for each area of the API.
          </p>
        </>
      )
    },
    {
      id: 'export',
      icon: 'download',
      title: 'Exporting & running the PyTest suite',
      blurb: 'A dependency-light test module you can run anywhere.',
      keywords: 'export download pytest python code suite successful all run locally ci environment api_base_url httpx',
      body: (
        <>
          <p>
            The <strong>PyTest Code</strong> view renders the generated module. <em>All tests</em> contains every step; <em>Successful</em>{' '}
            contains only steps that passed in this session — the safest thing to commit to CI. Both are available from the
            dashboard&apos;s <em>Download</em> menu as well.
          </p>
          <CodeBlock title="Run locally" code={RUN_LOCALLY} />
          <p>
            The module needs only <code>pytest</code> and <code>httpx</code>; runtime helpers (JSONPath extraction, variable
            resolution, pacing and 404 retry) are embedded in the file.
          </p>
          <CodeBlock title=".github/workflows/oas-tests.yml" code={CI_SNIPPET} />
        </>
      )
    },
    {
      id: 'github',
      icon: 'github',
      title: 'Pushing to GitHub',
      blurb: 'Commit the suite or open a pull request from the dashboard.',
      keywords: 'github push commit pull request pr token pat permissions branch folder repository fine-grained classic',
      body: (
        <>
          <ol className="doc-steps">
            <li>
              <strong>Target repository</strong>
              Enter <code>owner/repo</code>. The field is pre-filled from the backend&apos;s detected origin when available.
            </li>
            <li>
              <strong>Branch &amp; folder</strong>
              Defaults are timestamped — <code>oas-tests-agent/&lt;timestamp&gt;</code> and <code>tests/oas-tests-agent/&lt;timestamp&gt;</code> —
              so repeated pushes never collide. Edit them to reuse a branch.
            </li>
            <li>
              <strong>Mode &amp; scope</strong>
              <em>Commit to branch</em> pushes directly; <em>Open pull request</em> pushes the branch and creates a PR against the
              default branch. Scope mirrors the download options (all vs. successful).
            </li>
            <li>
              <strong>Personal access token</strong>
              Required on every push. It is kept only in this tab&apos;s <code>sessionStorage</code>, scoped to the session id, and is
              never written to disk by the backend.
            </li>
          </ol>
          <div className="doc-table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Token type</th>
                  <th>Required permissions</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>Fine-grained PAT (<code>github_pat_…</code>)</td>
                  <td>
                    <em>Resource owner</em> set to the repo&apos;s org/user · Contents: Read &amp; write · Pull requests: Read &amp; write (PR
                    mode) · SAML SSO authorised if the org enforces it
                  </td>
                </tr>
                <tr>
                  <td>Classic PAT (<code>ghp_…</code>)</td>
                  <td>
                    <code>repo</code> scope
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p>
            After a successful push the toolbar shows a <span className="pill pill--success"><Icon name="github" /> Pushed</span>{' '}
            link and the button changes to <em>Push updates</em>, so you can re-sync after more healing.
          </p>
        </>
      )
    },
    {
      id: 'interface',
      icon: 'keyboard',
      title: 'Interface, themes & accessibility',
      blurb: 'Navigation, theming, keyboard and motion preferences.',
      keywords: 'theme light dark system keyboard escape sidebar collapse accessibility reduced motion mobile drawer shortcuts',
      body: (
        <>
          <ul>
            <li>
              <strong>Theme</strong> — choose <em>System</em>, <em>Light</em> or <em>Dark</em> in the sidebar footer, or use the sun/moon
              button in the top bar for a quick toggle. <em>System</em> follows your OS and updates live when it changes.
            </li>
            <li>
              <strong>Sidebar</strong> — collapse it to icons with the <em>Collapse</em> control; the choice is remembered. On narrow
              screens it becomes a drawer behind the <Icon name="menu" style={{ verticalAlign: '-3px' }} /> button.
            </li>
            <li>
              <strong>Signed-in user</strong> — when OASVerify runs on Cloud Run behind Identity-Aware Proxy, the chip at the
              right of the top bar shows your account name (the part of your email before <code>@</code>). Locally it reads{' '}
              <code>LOCAL_USER_EMAIL</code> from the backend environment, and otherwise shows <em>User</em>.
            </li>
            <li>
              <strong>Sessions</strong> — each generation is shown by a friendly name (<em>Session 1</em>, <em>Session 2</em>, …).
              Rename the active one with the pencil in the sidebar card, and switch between earlier runs from the{' '}
              <em>Recent</em> list beneath it. Names are stored in your browser only.
            </li>
            <li>
              <strong>Deep links</strong> — every view has a stable URL (<code>/execution_dashboard/&lt;id&gt;</code>,{' '}
              <code>/visualize/&lt;id&gt;</code>, <code>/tests/&lt;id&gt;</code>). The URL and the backend API use the underlying
              session id; the copy button in the top-bar chip puts it on your clipboard for API calls.
            </li>
            <li>
              <strong>Keyboard</strong> — <kbd>Tab</kbd> reaches every control with a visible focus ring; <kbd>Esc</kbd> closes menus,
              dialogs and the mobile drawer.
            </li>
            <li>
              <strong>Reduced motion</strong> — when your OS requests it, transitions become short fades, the background stops
              drifting and the theme switch is instant.
            </li>
          </ul>
        </>
      )
    },
    {
      id: 'troubleshooting',
      icon: 'life-buoy',
      title: 'Troubleshooting',
      blurb: 'Common messages and what to do about them.',
      keywords: 'troubleshooting faq error session not found successful tests flowchart offline backend 401 403 404 github counts differ',
      body: (
        <>
          <Faq q="“Session not found” when opening a dashboard link">
            <p>
              The session workspace no longer exists on the backend (it was cleaned up or the server was reset). Generate a new
              suite — the sidebar session card will pick it up automatically.
            </p>
          </Faq>
          <Faq q="“No successful tests recorded yet” on export">
            <p>
              The <em>Successful</em> scope only includes steps that passed in this session. Run the suite (or at least one
              journey) in the Execution Dashboard first, or switch to <em>All tests</em>.
            </p>
          </Faq>
          <Faq q="Flowcharts show the raw graph definition instead of a diagram">
            <p>
              The Mermaid renderer is loaded from a CDN. When you are offline it cannot load, so the Visualizer falls back to
              showing the graph source. Reconnect and reload the page.
            </p>
          </Faq>
          <Faq q="GitHub push fails with 401 / 403 / 404">
            <p>
              <strong>401</strong> — the token is invalid or expired. <strong>403</strong> — the token lacks <em>Contents: Read &amp;
              write</em> (or <em>Pull requests</em> for PR mode), or SAML SSO is not authorised. <strong>404</strong> — the repository is
              private and a fine-grained token&apos;s <em>Resource owner</em> is not the repo&apos;s org, or the <code>owner/repo</code>{' '}
              value is misspelled.
            </p>
          </Faq>
          <Faq q="The page loads but nothing generates or runs">
            <p>
              The React app proxies <code>/api</code>, <code>/upload</code>, <code>/status</code>, <code>/stream_*</code> and the other
              endpoints to the FastAPI backend on port 8081. Make sure <code>python server.py</code> is running; the sidebar&apos;s
              <em> API reference</em> link opening correctly is a quick health check.
            </p>
          </Faq>
          <Faq q="Tests pass in the dashboard but fail when I run the file locally">
            <p>
              Local runs use the spec&apos;s <code>servers</code> URL unless you set <code>API_BASE_URL</code>, and they need the same
              credentials (<code>API_AUTH_TOKEN</code> / <code>API_KEY</code>). Stateful journeys also depend on execution order —
              run the whole file rather than a single test function.
            </p>
          </Faq>
          <Faq q="Why does the sidebar say 68 tests while the dashboard says 70?">
            <p>
              The session summary counts only <em>test</em> steps, while the dashboard also counts setup and teardown steps that
              run as part of each journey. Both numbers describe the same suite.
            </p>
          </Faq>
        </>
      )
    }
  ];
}

/* --------------------------------------------------------------------------
   View
   -------------------------------------------------------------------------- */
export function GuideView({ navigate, activeSessionId }) {
  const [query, setQuery] = useState('');
  const [activeId, setActiveId] = useState('quick-start');
  const sections = useMemo(() => buildSections({ navigate, activeSessionId }), [navigate, activeSessionId]);
  const mainRef = useRef(null);

  const normalizedQuery = query.trim().toLowerCase();
  const visible = useMemo(() => {
    if (!normalizedQuery) return sections;
    return sections.filter((s) => `${s.title} ${s.blurb} ${s.keywords}`.toLowerCase().includes(normalizedQuery));
  }, [sections, normalizedQuery]);

  // Scroll-spy: highlight the section currently under the top bar
  useEffect(() => {
    if (!mainRef.current || typeof IntersectionObserver === 'undefined') return undefined;
    const nodes = Array.from(mainRef.current.querySelectorAll('.doc-section'));
    if (nodes.length === 0) return undefined;
    const observer = new IntersectionObserver(
      (entries) => {
        const hit = entries.filter((e) => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
        if (hit) setActiveId(hit.target.id);
      },
      { rootMargin: '-80px 0px -60% 0px', threshold: [0, 0.2] }
    );
    nodes.forEach((n) => observer.observe(n));
    return () => observer.disconnect();
  }, [visible]);

  // Honour a hash on first load (/guide#github)
  useEffect(() => {
    const hash = window.location.hash.replace('#', '');
    if (!hash) return;
    const t = setTimeout(() => {
      const el = document.getElementById(hash);
      if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }, 120);
    return () => clearTimeout(t);
  }, []);

  const jumpTo = (id) => {
    const el = document.getElementById(id);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      window.history.replaceState({}, '', `/guide#${id}`);
      setActiveId(id);
    }
  };

  return (
    <div>
      <div className="page-head">
        <div>
          <span className="eyebrow">
            <Icon name="book-open" /> User guide
          </span>
          <h1 style={{ marginTop: 6 }}>How OASVerify works, end to end</h1>
          <p>
            Everything from uploading a spec to shipping a self-healed PyTest suite. Search below or jump to a section from
            the index.
          </p>
        </div>
        <div className="page-head__actions">
          <button type="button" className="btn btn-primary btn-sm" onClick={() => navigate('/')}>
            <Icon name="rocket" /> Open Generator
          </button>
          <a className="btn btn-secondary btn-sm" href="/docs" target="_blank" rel="noreferrer">
            <Icon name="book" /> API reference <Icon name="arrow-up-right" />
          </a>
        </div>
      </div>

      <div className="doc-layout">
        <div className="doc-main" ref={mainRef}>
          <div className="card doc-search" style={{ padding: 16 }}>
            <label className="sr-only" htmlFor="guide-search">
              Search the guide
            </label>
            <div className="input-with-icon">
              <Icon name="search" />
              <input
                id="guide-search"
                type="search"
                className="input"
                placeholder="Search the guide — e.g. token, self-healing, API_BASE_URL"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                autoComplete="off"
              />
              {query && (
                <button type="button" className="btn-icon" onClick={() => setQuery('')} aria-label="Clear search">
                  <Icon name="x" />
                </button>
              )}
            </div>
            <div className="doc-search__meta">
              <span>
                {normalizedQuery
                  ? `${visible.length} of ${sections.length} sections match`
                  : 'Popular topics'}
              </span>
            </div>
            {!normalizedQuery && (
              <div className="doc-chips" style={{ marginTop: 10 }}>
                {['self-healing', 'test-config', 'github', 'export', 'troubleshooting'].map((id) => {
                  const s = sections.find((x) => x.id === id);
                  return (
                    <button key={id} type="button" className="doc-chip" onClick={() => jumpTo(id)}>
                      <Icon name={s.icon} /> {s.title}
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          {visible.length === 0 && (
            <div className="card empty">
              <div className="empty__icon">
                <Icon name="search" />
              </div>
              <h2>No sections match “{query}”</h2>
              <p>Try a broader term such as “token”, “healing” or “export”.</p>
              <div className="empty__actions">
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => setQuery('')}>
                  Clear search
                </button>
              </div>
            </div>
          )}

          {visible.map((s, idx) => (
            <section key={s.id} id={s.id} className="card doc-section" style={{ '--i': idx }} aria-labelledby={`${s.id}-title`}>
              <div className="doc-section__head">
                <div className="doc-section__icon" aria-hidden="true">
                  <Icon name={s.icon} />
                </div>
                <div style={{ minWidth: 0 }}>
                  <h2 id={`${s.id}-title`}>{s.title}</h2>
                  <p>{s.blurb}</p>
                </div>
                <button
                  type="button"
                  className="btn-icon doc-section__anchor"
                  onClick={() => jumpTo(s.id)}
                  aria-label={`Link to ${s.title}`}
                  title="Copy link position"
                >
                  <Icon name="link" />
                </button>
              </div>
              <div className="doc-body">{s.body}</div>
            </section>
          ))}
        </div>

        <nav className="doc-toc glass" aria-label="On this page">
          <div className="doc-toc__label">On this page</div>
          {sections.map((s) => (
            <button
              key={s.id}
              type="button"
              className={`doc-toc__link ${activeId === s.id ? 'is-active' : ''}`}
              aria-current={activeId === s.id ? 'true' : undefined}
              onClick={() => jumpTo(s.id)}
            >
              <Icon name={s.icon} />
              <span>{s.title}</span>
            </button>
          ))}
          <div className="doc-toc__divider" />
          <a className="doc-toc__link" href="/docs" target="_blank" rel="noreferrer">
            <Icon name="book" />
            <span>Backend API reference</span>
          </a>
        </nav>
      </div>
    </div>
  );
}
