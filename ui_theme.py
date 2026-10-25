"""Shared design system for the embedded web UI: CSS tokens, components, SVG icons, and layout helpers."""

from __future__ import annotations

import html

APP_NAME = "OASVerify"
APP_VERSION = "2.0.0"
APP_TAGLINE = "Autonomous, zero-script API QA powered by Gemini"

BASE_CSS = """
:root {
    --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;

    --bg: #f3f5fa;
    --surface: #ffffff;
    --surface-2: #f8f9fb;
    --border: #e4e7eb;
    --border-strong: #d0d7de;

    --text: #111418;
    --text-2: #3d444d;
    --text-3: #6e7781;

    --primary: #1a73e8;
    --primary-hover: #1765cc;
    --primary-soft: #e8f0fe;
    --primary-border: #c7dafb;
    --primary-text: #185abc;

    --success: #1e8e3e;
    --success-hover: #197a35;
    --success-soft: #e6f4ea;
    --success-border: #c6e7cf;
    --success-text: #137333;

    --warning: #f9ab00;
    --warning-soft: #fef7e0;
    --warning-border: #fde293;
    --warning-text: #7a4f00;

    --danger: #d93025;
    --danger-soft: #fce8e6;
    --danger-border: #f6c6c2;
    --danger-text: #b3261e;

    --heal: var(--teal);
    --heal-soft: var(--teal-soft);
    --heal-border: var(--teal-border);
    --heal-text: var(--teal-text);

    --note: #e3b341;
    --note-soft: #fff8c5;
    --note-border: #f0dc82;
    --note-text: #6b4e00;

    --teal: #0b8a7a;
    --teal-soft: #e0f5f2;
    --teal-border: #b5e3dc;
    --teal-text: #0b6b5f;

    --code-bg: #0d1117;
    --code-fg: #e6edf3;
    --code-border: #30363d;

    --radius-sm: 6px;
    --radius: 8px;
    --radius-lg: 12px;
    --shadow-sm: 0 1px 2px rgba(31, 35, 40, .06);
    --shadow: 0 1px 3px rgba(31, 35, 40, .08), 0 6px 16px rgba(31, 35, 40, .05);
    --shadow-lg: 0 16px 40px rgba(31, 35, 40, .2);
    --header-h: 56px;
    --container: 1200px;
}

*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
    margin: 0;
    font-family: var(--font-sans);
    font-size: 15px;
    font-weight: 500;
    line-height: 1.5;
    color: var(--text);
    background: var(--bg);
}
a { color: var(--primary); text-decoration: none; }
a:hover { text-decoration: underline; }
h1, h2, h3, h4 { margin: 0; font-weight: 700; letter-spacing: -0.01em; color: var(--text); }
h1 { font-size: 24px; line-height: 1.3; }
h2 { font-size: 18px; }
h3 { font-size: 16px; }
h4 { font-size: 14px; }
p { margin: 0; }
small { font-size: 13px; }
button, input, select, textarea { font-family: inherit; font-size: inherit; font-weight: inherit; }
button { cursor: pointer; }
code, pre, kbd, .mono { font-family: var(--font-mono); }
code {
    font-size: 0.9em;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 5px;
    color: var(--text);
    overflow-wrap: anywhere;
}
pre { margin: 0; }
svg.icon { width: 16px; height: 16px; flex: none; }

/* Layout */
.app-header {
    position: sticky;
    top: 0;
    z-index: 100;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
}
.app-header::before { content: ""; display: block; height: 3px; background: var(--primary); }
.app-header__inner {
    max-width: var(--container);
    margin: 0 auto;
    padding: 8px 24px;
    min-height: var(--header-h);
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
}
.brand {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    color: var(--text);
    font-weight: 700;
    font-size: 16px;
    white-space: nowrap;
}
.brand:hover { text-decoration: none; }
.brand__mark {
    width: 30px;
    height: 30px;
    border-radius: 8px;
    background: var(--primary);
    color: #fff;
    display: grid;
    place-items: center;
    box-shadow: var(--shadow-sm);
}
.brand__mark svg { width: 17px; height: 17px; }
.app-header__divider { width: 1px; height: 22px; background: var(--border); }
.app-header__title {
    font-size: 15px;
    font-weight: 600;
    color: var(--text-2);
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
}
.app-header__spacer { flex: 1; }
.app-header__actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    justify-content: flex-end;
}
.page { max-width: var(--container); margin: 0 auto; padding: 28px 24px 56px; }
.page--narrow { --container: 780px; }
.page-title { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
.page-title__meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
.page-title p { color: var(--text-2); margin-top: 6px; max-width: 70ch; }
.hero {
    position: relative;
    overflow: hidden;
    margin: 8px 0 24px;
    padding: 26px 30px;
    border-radius: var(--radius-lg);
    border: 1px solid #d0dbea;
    background: #e8eef7;
}
.hero h1 { font-size: 26px; letter-spacing: -0.02em; }
.hero p { color: var(--text-2); margin-top: 8px; font-size: 16px; max-width: 72ch; }
.hero__kicker { display: inline-flex; align-items: center; gap: 8px; margin-bottom: 10px; font-size: 12px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--primary-text); }
.hero__kicker svg { width: 15px; height: 15px; flex: none; }
.page-title.hero { margin: 0 0 20px; }
.section-title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--text-2);
    margin: 28px 0 12px;
}
.section-title::before { content: ""; width: 4px; height: 14px; border-radius: 2px; background: var(--primary); flex: none; }
.stack { display: flex; flex-direction: column; gap: 14px; }
.row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.muted { color: var(--text-3); }
.text-2 { color: var(--text-2); }
.hidden { display: none !important; }
.app-footer { text-align: center; color: var(--text-3); font-size: 13px; padding: 24px; }

/* Buttons */
.btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    height: 34px;
    padding: 0 14px;
    border-radius: var(--radius-sm);
    border: 1px solid transparent;
    font-size: 14px;
    font-weight: 600;
    line-height: 1;
    white-space: nowrap;
    text-decoration: none;
    user-select: none;
    transition: background .15s, border-color .15s, box-shadow .15s, color .15s, opacity .15s;
}
.btn:hover { text-decoration: none; }
.btn svg { width: 15px; height: 15px; flex: none; }
.btn:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
.btn:disabled, .btn[aria-disabled="true"] { opacity: .55; cursor: not-allowed; pointer-events: none; }
.btn-primary { background: var(--primary); color: #fff; border-color: var(--primary); }
.btn-primary:hover { background: var(--primary-hover); border-color: var(--primary-hover); }
.btn-success { background: var(--success); color: #fff; border-color: var(--success); }
.btn-success:hover { background: var(--success-hover); border-color: var(--success-hover); }
.btn-danger { background: var(--danger); color: #fff; border-color: var(--danger); }
.btn-dark { background: #24292f; color: #fff; border-color: #1b1f24; }
.btn-dark:hover { background: #32383f; }
.btn-secondary { background: var(--surface); color: var(--text); border-color: var(--border-strong); box-shadow: var(--shadow-sm); }
.btn-secondary:hover { background: var(--surface-2); border-color: #b6bec7; }
.btn-ghost { background: transparent; color: var(--text-2); }
.btn-ghost:hover { background: rgba(31, 35, 40, .06); color: var(--text); }
.btn-sm { height: 28px; padding: 0 10px; font-size: 13px; gap: 6px; }
.btn-sm svg { width: 13px; height: 13px; }
.btn-lg { height: 42px; padding: 0 20px; font-size: 15px; }
.btn-block { width: 100%; }
.btn-icon {
    width: 32px;
    height: 32px;
    padding: 0;
    border-radius: var(--radius-sm);
    background: transparent;
    border: 1px solid transparent;
    color: var(--text-2);
    display: inline-flex;
    align-items: center;
    justify-content: center;
}
.btn-icon:hover { background: rgba(31, 35, 40, .06); color: var(--text); }
.btn-icon svg { width: 16px; height: 16px; }
.btn-group { display: inline-flex; gap: 6px; flex-wrap: wrap; }

/* Dropdown menu */
.dropdown { position: relative; display: inline-block; }
.dropdown__menu {
    position: absolute;
    top: calc(100% + 6px);
    right: 0;
    z-index: 300;
    min-width: 260px;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: var(--radius);
    box-shadow: var(--shadow-lg);
    padding: 6px;
    display: none;
    flex-direction: column;
    gap: 2px;
}
.dropdown.is-open .dropdown__menu { display: flex; }
.dropdown__item {
    display: flex;
    align-items: center;
    gap: 10px;
    width: 100%;
    padding: 9px 10px;
    border-radius: var(--radius-sm);
    border: none;
    background: transparent;
    color: var(--text);
    text-align: left;
    text-decoration: none;
    cursor: pointer;
    transition: background .12s;
}
.dropdown__item:hover { background: var(--surface-2); text-decoration: none; }
.dropdown__item svg { width: 16px; height: 16px; color: var(--primary); flex: none; }
.dropdown__item--success svg { color: var(--success); }
.dropdown__item-body { flex: 1; min-width: 0; }
.dropdown__item-title { flex: 1; display: flex; align-items: center; gap: 10px; font-size: 13.5px; font-weight: 600; color: var(--text); white-space: nowrap; }
.dropdown__item-title > span:first-of-type { flex: 1; }
.dropdown__item-desc { font-size: 12px; color: var(--text-3); margin-top: 2px; line-height: 1.35; }

/* Scope choice cards (GitHub commit scope) */
.scope-choice { display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }
.scope-choice__option {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 10px 12px;
    border: 1.5px solid var(--border-strong);
    border-radius: var(--radius);
    background: var(--surface);
    cursor: pointer;
    transition: border-color .15s, background .15s, box-shadow .15s;
}
.scope-choice__option:hover { border-color: var(--primary); background: var(--surface-2); }
.scope-choice__option input[type="radio"] { position: absolute; opacity: 0; pointer-events: none; }
.scope-choice__option.is-selected {
    border-color: var(--primary);
    background: var(--primary-soft);
    box-shadow: 0 0 0 1px var(--primary);
}
.scope-choice__head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.scope-choice__title { font-size: 13.5px; font-weight: 700; color: var(--text); display: inline-flex; align-items: center; gap: 6px; }
.scope-choice__option.is-selected .scope-choice__title { color: var(--primary-text); }
.scope-choice__desc { font-size: 12px; color: var(--text-2); line-height: 1.35; }

.spinner {
    width: 14px;
    height: 14px;
    border: 2px solid currentColor;
    border-right-color: transparent;
    border-radius: 50%;
    display: inline-block;
    flex: none;
    animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }

/* Cards */
.card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-sm);
}
.card__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
}
.card__header h2, .card__header h3 { display: inline-flex; align-items: center; gap: 8px; }
.card__header svg.icon { color: var(--text-3); }
.card__body { padding: 20px; }
.card__footer {
    padding: 12px 20px;
    border-top: 1px solid var(--border);
    background: var(--surface-2);
    border-radius: 0 0 var(--radius-lg) var(--radius-lg);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
}

/* Accented card: primary call-to-action surface that stands out from neutral cards */
.card--accent {
    background: #f4f8ff;
    border-color: var(--primary-border);
    box-shadow: 0 1px 2px rgba(26, 115, 232, .08), 0 8px 24px rgba(26, 115, 232, .10);
}
.card--accent > .card__header {
    background: var(--primary-soft);
    border-bottom-color: var(--primary-border);
    border-radius: var(--radius-lg) var(--radius-lg) 0 0;
}
.card--accent > .card__header h2, .card--accent > .card__header h3 { color: var(--primary-text); }
.card--accent > .card__header svg.icon { color: var(--primary); }
.card--accent > .card__footer { background: var(--primary-soft); border-top-color: var(--primary-border); }
.card--accent .dropzone { background: var(--surface); border-color: var(--primary-border); }
.card--accent .dropzone:hover, .card--accent .dropzone.is-dragover { background: var(--primary-soft); border-color: var(--primary); }
.card--accent .dropzone.has-file { background: var(--success-soft); border-color: var(--success-border); }
.card--accent .alert-info { background: var(--surface); }

/* Header-tinted cards: colour-code a card by purpose without tinting its whole body */
.card--tint-primary > .card__header { background: var(--primary-soft); border-bottom-color: var(--primary-border); }
.card--tint-primary > .card__header h2, .card--tint-primary > .card__header h3 { color: var(--primary-text); }
.card--tint-primary > .card__header svg.icon { color: var(--primary); }
.card--tint-heal > .card__header { background: var(--heal-soft); border-bottom-color: var(--heal-border); }
.card--tint-heal > .card__header h2, .card--tint-heal > .card__header h3 { color: var(--heal-text); }
.card--tint-heal > .card__header svg.icon { color: var(--heal); }
.card--tint-success > .card__header { background: var(--success-soft); border-bottom-color: var(--success-border); }
.card--tint-success > .card__header h2, .card--tint-success > .card__header h3 { color: var(--success-text); }
.card--tint-success > .card__header svg.icon { color: var(--success); }
.card--tint-danger > .card__header { background: var(--danger-soft); border-bottom-color: var(--danger-border); }
.card--tint-danger > .card__header h2, .card--tint-danger > .card__header h3 { color: var(--danger-text); }
.card--tint-danger > .card__header svg.icon { color: var(--danger); }

/* Badges, chips, method tags */
.badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 22px;
    padding: 0 9px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .02em;
    text-transform: uppercase;
    border: 1px solid transparent;
    white-space: nowrap;
    vertical-align: middle;
}
.badge .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.badge-neutral { background: var(--surface-2); color: var(--text-2); border-color: var(--border); }
.badge-running, .badge-info { background: var(--primary-soft); color: var(--primary-text); border-color: var(--primary-border); }
.badge-running .dot { animation: pulse 1.2s ease-in-out infinite; }
.badge-completed, .badge-success { background: var(--success-soft); color: var(--success-text); border-color: var(--success-border); }
.badge-failed, .badge-danger { background: var(--danger-soft); color: var(--danger-text); border-color: var(--danger-border); }
.badge-warning { background: var(--warning-soft); color: var(--warning-text); border-color: var(--warning-border); }
.badge-heal { background: var(--heal-soft); color: var(--heal-text); border-color: var(--heal-border); }
.badge svg { width: 12px; height: 12px; }

.chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    height: 26px;
    padding: 0 10px;
    border-radius: 999px;
    background: var(--surface);
    border: 1px solid var(--border);
    font-size: 13px;
    color: var(--text-2);
    white-space: nowrap;
    max-width: 100%;
}
.chip svg { width: 13px; height: 13px; color: var(--text-3); flex: none; }
.chip code { background: transparent; border: 0; padding: 0; color: var(--text); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip strong { color: var(--text); font-weight: 600; }

.method {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 62px;
    height: 22px;
    padding: 0 8px;
    border-radius: 5px;
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .04em;
    background: var(--surface-2);
    color: var(--text-2);
    flex: none;
}
.method-GET { background: #e8f0fe; color: #185abc; }
.method-POST { background: #e6f4ea; color: #137333; }
.method-PUT { background: #fef3e2; color: #a85d00; }
.method-PATCH { background: #e0f2f1; color: #00695c; }
.method-DELETE { background: #fce8e6; color: #b3261e; }

/* Alerts */
.alert {
    display: flex;
    gap: 10px;
    align-items: flex-start;
    padding: 12px 14px;
    border-radius: var(--radius);
    border: 1px solid;
    border-left-width: 4px;
    font-size: 14px;
    line-height: 1.5;
}
.alert > svg { width: 16px; height: 16px; flex: none; margin-top: 2px; }
.alert > div { min-width: 0; flex: 1; }
.alert strong { font-weight: 600; }
.alert code { background: rgba(255, 255, 255, .7); border-color: rgba(0, 0, 0, .08); color: inherit; }
.alert a { color: inherit; font-weight: 600; text-decoration: underline; }
.alert-info { background: var(--primary-soft); border-color: var(--primary-border); border-left-color: var(--primary); color: var(--primary-text); }
.alert-success { background: var(--success-soft); border-color: var(--success-border); border-left-color: var(--success); color: var(--success-text); }
.alert-warning { background: var(--warning-soft); border-color: var(--warning-border); border-left-color: var(--warning); color: var(--warning-text); }
.alert-danger { background: var(--danger-soft); border-color: var(--danger-border); border-left-color: var(--danger); color: var(--danger-text); }
.alert-heal { background: var(--heal-soft); border-color: var(--heal-border); border-left-color: var(--heal); color: var(--heal-text); }
.alert-note { background: var(--note-soft); border-color: var(--note-border); border-left-color: var(--note); color: var(--note-text); }
.alert-note strong { color: #4d3800; }
.alert--between { align-items: center; justify-content: space-between; }

/* Progress */
.progress { height: 8px; background: var(--border); border-radius: 999px; overflow: hidden; }
.progress--lg { height: 10px; }
.progress__bar {
    height: 100%;
    width: 0%;
    background: var(--primary);
    border-radius: 999px;
    transition: width .3s ease, background-color .3s ease;
}
.healing-active { animation: healingPulse 1.6s infinite ease-in-out !important; }
@keyframes healingPulse {
    0% { background-color: #f59e0b; }
    50% { background-color: #0b8a7a; }
    100% { background-color: #f59e0b; }
}

/* Switch */
.switch {
    position: relative;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    cursor: pointer;
    user-select: none;
    font-size: 14px;
    font-weight: 600;
    color: var(--text-2);
}
.switch input { position: absolute; opacity: 0; width: 0; height: 0; }
.switch__track {
    position: relative;
    width: 34px;
    height: 20px;
    border-radius: 999px;
    background: #c9ced6;
    transition: background .2s;
    flex: none;
}
.switch__track::after {
    content: "";
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: #fff;
    box-shadow: 0 1px 2px rgba(0, 0, 0, .25);
    transition: transform .2s;
}
.switch input:checked + .switch__track { background: var(--success); }
.switch input:checked + .switch__track::after { transform: translateX(14px); }
.switch input:focus-visible + .switch__track { outline: 2px solid var(--primary); outline-offset: 2px; }
.switch svg { width: 14px; height: 14px; color: var(--heal); }

/* Form fields */
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field__label {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .05em;
    text-transform: uppercase;
    color: var(--text-2);
}
.field__label .req { color: var(--danger); }
.input, .select {
    width: 100%;
    height: 36px;
    padding: 0 12px;
    border: 1px solid var(--border-strong);
    border-radius: var(--radius-sm);
    font-size: 14px;
    color: var(--text);
    background: var(--surface);
    transition: border-color .15s, box-shadow .15s;
}
.input:focus, .select:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(26, 115, 232, .18); }
.input[readonly] { background: var(--surface-2); color: var(--text-2); }
.input.is-invalid { border-color: var(--danger); box-shadow: 0 0 0 3px rgba(217, 48, 37, .15); }
.input--mono { font-family: var(--font-mono); font-size: 13.5px; }
.field__hint { font-size: 13px; color: var(--text-2); line-height: 1.45; }
.field__hint.is-danger { color: var(--danger-text); }
.field__hint.is-success { color: var(--success-text); }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 560px) { .grid-2 { grid-template-columns: 1fr; } }

/* Dropzone */
.dropzone {
    position: relative;
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 16px 18px;
    border: 1.5px dashed var(--border-strong);
    border-radius: var(--radius);
    background: var(--surface-2);
    cursor: pointer;
    transition: border-color .15s, background .15s;
}
.dropzone:hover, .dropzone.is-dragover { border-color: var(--primary); background: var(--primary-soft); }
.dropzone:focus-within { outline: 2px solid var(--primary); outline-offset: 2px; }
.dropzone.has-file { border-style: solid; border-color: var(--success-border); background: var(--success-soft); }
.dropzone input[type="file"] { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
.dropzone__icon {
    width: 40px;
    height: 40px;
    border-radius: 10px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--primary);
    display: grid;
    place-items: center;
    flex: none;
}
.dropzone__icon svg { width: 20px; height: 20px; }
.dropzone.has-file .dropzone__icon { color: var(--success); }
.dropzone__text { flex: 1; min-width: 0; }
.dropzone__title { font-weight: 600; font-size: 14.5px; display: flex; align-items: center; gap: 8px; }
.dropzone__hint { font-size: 13px; color: var(--text-2); margin-top: 2px; }
.dropzone__file {
    display: none;
    font-family: var(--font-mono);
    font-size: 13.5px;
    font-weight: 600;
    color: var(--success-text);
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.dropzone.has-file .dropzone__hint { display: none; }
.dropzone.has-file .dropzone__file { display: block; }
.dropzone__clear { position: relative; z-index: 1; display: none; }
.dropzone.has-file .dropzone__clear { display: inline-flex; }

/* Key-value grid */
.kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px 20px; }
.kv__label { font-size: 12px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: var(--text-3); }
.kv__value { font-size: 14px; margin-top: 3px; overflow-wrap: anywhere; }

/* Stats */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.stat { background: var(--surface); border: 1px solid var(--border); border-top: 3px solid var(--border-strong); border-radius: var(--radius); padding: 12px 16px; box-shadow: var(--shadow-sm); }
.stat__label { font-size: 12px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; color: var(--text-3); }
.stat__value { font-size: 26px; font-weight: 700; letter-spacing: -.02em; margin-top: 2px; display: flex; align-items: baseline; gap: 6px; line-height: 1.2; }
.stat__value small { font-size: 13px; color: var(--text-2); font-weight: 600; }
.stat__pair { display: flex; gap: 22px; flex-wrap: wrap; margin-top: 2px; }
.stat__metric { min-width: 0; }
.stat__metric .stat__value { margin-top: 0; }
.stat__caption { font-size: 12px; font-weight: 600; color: var(--text-3); margin-top: 1px; white-space: nowrap; }
.stat--primary { background: #eef4fe; border-color: var(--primary-border); border-top-color: var(--primary); }
.stat--heal { background: #eaf7f5; border-color: var(--heal-border); border-top-color: var(--heal); }
.stat--slate { background: #eef1f5; border-color: var(--border-strong); border-top-color: #5f6b7a; }
.stat--success { background: #edf8f0; border-color: var(--success-border); border-top-color: var(--success); }
.stat--danger { background: #fdf1ef; border-color: var(--danger-border); border-top-color: var(--danger); }
.stat--teal { background: #eaf7f5; border-color: var(--teal-border); border-top-color: var(--teal); }
.stat--note { background: #fffbe6; border-color: var(--note-border); border-top-color: var(--note); }
.stat--primary .stat__value { color: var(--primary-text); }
.stat--heal .stat__value { color: var(--heal-text); }
.stat--success .stat__value { color: var(--success-text); }
.stat--danger .stat__value { color: var(--danger-text); }
.stat--teal .stat__value { color: var(--teal-text); }
.stat--note .stat__value { color: var(--note-text); }
.stat--slate .stat__value { color: #2c3645; }

/* Status icon */
.status-icon { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; flex: none; vertical-align: middle; }
.status-icon:empty::before { content: ""; width: 10px; height: 10px; border-radius: 50%; border: 2px solid var(--border-strong); }
.status-icon svg { width: 18px; height: 18px; }
.status-icon[data-state="running"] { color: var(--primary); }
.status-icon[data-state="running"] svg { animation: spin .8s linear infinite; }
.status-icon[data-state="pass"] { color: var(--success); }
.status-icon[data-state="fail"] { color: var(--danger); }

/* Console / code */
.console {
    background: var(--code-bg);
    color: var(--code-fg);
    border: 1px solid var(--code-border);
    border-radius: var(--radius);
    overflow: hidden;
}
.console__header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 8px 14px;
    background: #161b22;
    border-bottom: 1px solid var(--code-border);
    color: #9da7b3;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: .05em;
    text-transform: uppercase;
}
.console__header .btn-ghost { color: #9da7b3; }
.console__header .btn-ghost:hover { color: #e6edf3; background: rgba(255, 255, 255, .06); }
.console__body {
    padding: 12px 14px;
    max-height: 280px;
    overflow-y: auto;
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 1.6;
    white-space: pre-wrap;
    word-break: break-word;
}
.console__body:empty::before { content: "No log output yet."; color: #8b949e; }
.console__time { color: #8b949e; }

.codeblock {
    background: var(--code-bg);
    color: var(--code-fg);
    border: 1px solid var(--code-border);
    border-radius: var(--radius-sm);
    padding: 12px 14px;
    font-family: var(--font-mono);
    font-size: 13px;
    line-height: 1.55;
    overflow: auto;
}

/* Modal */
.modal-backdrop {
    position: fixed;
    inset: 0;
    z-index: 1000;
    background: rgba(15, 23, 42, .5);
    backdrop-filter: blur(2px);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 20px;
}
.modal {
    width: 580px;
    max-width: 100%;
    max-height: calc(100vh - 40px);
    display: flex;
    flex-direction: column;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg);
    overflow: hidden;
}
.modal__header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 20px; border-bottom: 1px solid var(--border); background: var(--surface-2); }
.modal__title { display: inline-flex; align-items: center; gap: 10px; font-size: 16px; font-weight: 700; }
.modal__title svg { width: 18px; height: 18px; }
.modal__body { padding: 20px; overflow-y: auto; display: flex; flex-direction: column; gap: 16px; }
.modal__footer { display: flex; justify-content: flex-end; gap: 10px; padding: 12px 20px; border-top: 1px solid var(--border); background: var(--surface-2); }

/* Toast */
.toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 2000;
    display: none;
    align-items: center;
    gap: 10px;
    max-width: 420px;
    background: #1f2328;
    color: #fff;
    padding: 12px 18px;
    border-radius: var(--radius);
    font-size: 14px;
    font-weight: 600;
    box-shadow: var(--shadow-lg);
}
.toast.toast-danger { background: var(--danger); }
.toast.toast-success { background: var(--success); }

/* Error page */
.error-page { max-width: 560px; margin: 80px auto; text-align: center; padding: 0 24px; }
.error-page__code { font-size: 56px; font-weight: 700; letter-spacing: -.04em; color: var(--border-strong); line-height: 1; }
.error-page h1 { margin-top: 12px; }
.error-page p { color: var(--text-2); margin-top: 8px; }
.error-page .btn { margin-top: 20px; }

@media (max-width: 720px) {
    .app-header__inner, .page { padding-left: 16px; padding-right: 16px; }
    .card__body { padding: 16px; }
}
"""

_STROKE_ICONS: dict[str, str] = {
    "flask": (
        '<path d="M10 2v7.5a2 2 0 0 1-.2.9L4.7 20.5a1 1 0 0 0 .9 1.5h12.8a1 1 0 0 0 .9-1.5L14.2 10.4a2 2 0 0 1-.2-.9V2"/>'
        '<path d="M8.5 2h7"/><path d="M7 16h10"/>'
    ),
    "upload": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/>',
    "file": '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/>',
    "sliders": (
        '<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/>'
        '<line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/>'
        '<line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>'
    ),
    "code": '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
    "terminal": '<polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/>',
    "external": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "copy": '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    "sparkles": (
        '<path d="M9.9 15.5a2 2 0 0 0-1.4-1.4l-6.1-1.6a.5.5 0 0 1 0-1l6.1-1.6a2 2 0 0 0 1.4-1.4l1.6-6.1a.5.5 0 0 1 1 0l1.6 6.1a2 2 0 0 0 1.4 1.4l6.1 1.6a.5.5 0 0 1 0 1l-6.1 1.6a2 2 0 0 0-1.4 1.4l-1.6 6.1a.5.5 0 0 1-1 0z"/>'
        '<path d="M20 3v4"/><path d="M22 5h-4"/>'
    ),
    "branch": '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
    "pull-request": '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7"/><line x1="6" x2="6" y1="9" y2="21"/>',
    "lightbulb": '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
    "info": '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    "alert": '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "arrow-right": '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
    "lock": '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "shield": (
        '<path d="M20 13c0 5-3.5 7.5-7.7 9a1 1 0 0 1-.6 0C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.2-2.7a1.2 1.2 0 0 1 1.6 0C14.5 3.8 17 5 19 5a1 1 0 0 1 1 1z"/>'
        '<path d="m9 12 2 2 4-4"/>'
    ),
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
    "hash": '<line x1="4" x2="20" y1="9" y2="9"/><line x1="4" x2="20" y1="15" y2="15"/><line x1="10" x2="8" y1="3" y2="21"/><line x1="16" x2="14" y1="3" y2="21"/>',
    "layers": (
        '<path d="m12.8 2.2a2 2 0 0 0-1.7 0L2.6 6.1a1 1 0 0 0 0 1.8l8.6 3.9a2 2 0 0 0 1.7 0l8.6-3.9a1 1 0 0 0 0-1.8Z"/>'
        '<path d="m22 17.7-9.2 4.1a2 2 0 0 1-1.7 0L2 17.7"/><path d="m22 12.7-9.2 4.1a2 2 0 0 1-1.7 0L2 12.7"/>'
    ),
    "list": '<line x1="8" x2="21" y1="6" y2="6"/><line x1="8" x2="21" y1="12" y2="12"/><line x1="8" x2="21" y1="18" y2="18"/><line x1="3" x2="3.01" y1="6" y2="6"/><line x1="3" x2="3.01" y1="12" y2="12"/><line x1="3" x2="3.01" y1="18" y2="18"/>',
    "bot": '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
    "activity": '<path d="M22 12h-2.5a2 2 0 0 0-1.9 1.4l-2.2 6.6a.5.5 0 0 1-.9 0L9.5 4a.5.5 0 0 0-.9 0L6.4 10.6A2 2 0 0 1 4.5 12H2"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "loader": '<path d="M21 12a9 9 0 1 1-6.2-8.6"/>',
    "rotate": '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/>',
    "clock": '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
}

_FILLED_ICONS: dict[str, tuple[str, str]] = {
    "play": ("0 0 24 24", '<path d="M8 5.14v13.72a1 1 0 0 0 1.5.86l11-6.86a1 1 0 0 0 0-1.72l-11-6.86a1 1 0 0 0-1.5.86Z"/>'),
    "github": (
        "0 0 16 16",
        '<path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"/>',
    ),
    "check-circle": (
        "0 0 24 24",
        '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm-1.2 14.4-3.9-3.9 1.4-1.4 2.5 2.5 5.3-5.3 1.4 1.4-6.7 6.7Z"/>',
    ),
    "x-circle": (
        "0 0 24 24",
        '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20Zm3.5 12.1-1.4 1.4L12 13.4l-2.1 2.1-1.4-1.4 2.1-2.1-2.1-2.1 1.4-1.4 2.1 2.1 2.1-2.1 1.4 1.4-2.1 2.1 2.1 2.1Z"/>',
    ),
}


def icon(name: str, css_class: str = "") -> str:
    """Returns an inline SVG for the named icon (stroke-based Lucide-style or filled)."""
    cls = f"icon {css_class}".strip()
    if name in _FILLED_ICONS:
        view_box, paths = _FILLED_ICONS[name]
        return (
            f'<svg class="{cls}" viewBox="{view_box}" fill="currentColor" aria-hidden="true">{paths}</svg>'
        )
    paths = _STROKE_ICONS.get(name, _STROKE_ICONS["info"])
    return (
        f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{paths}</svg>'
    )


def status_icon_markup() -> dict[str, str]:
    """Returns the SVG markup used by client-side scripts for running / pass / fail status indicators."""
    return {
        "running": icon("loader"),
        "pass": icon("check-circle"),
        "fail": icon("x-circle"),
    }


def render_head(title: str, extra_css: str = "") -> str:
    """Renders the shared <head> block including the base stylesheet."""
    full_title = f"{title} · {APP_NAME}" if title and title != APP_NAME else APP_NAME
    return (
        "<head>"
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(full_title)}</title>"
        f"<style>{BASE_CSS}{extra_css}</style>"
        "</head>"
    )


def render_header(title: str = "", actions_html: str = "") -> str:
    """Renders the sticky application header with brand mark, optional page title, and action slot."""
    title_html = (
        f'<span class="app-header__divider"></span><span class="app-header__title">{html.escape(title)}</span>'
        if title
        else ""
    )
    return (
        '<header class="app-header"><div class="app-header__inner">'
        f'<a class="brand" href="/" title="{html.escape(APP_NAME)} home">'
        f'<span class="brand__mark">{icon("shield")}</span><span>{html.escape(APP_NAME)}</span></a>'
        f"{title_html}"
        '<div class="app-header__spacer"></div>'
        f'<div class="app-header__actions">{actions_html}</div>'
        "</div></header>"
    )


def render_page(
    title: str,
    body_html: str,
    header_title: str = "",
    header_actions_html: str = "",
    extra_css: str = "",
    scripts_js: str = "",
    body_class: str = "",
    show_footer: bool = True,
) -> str:
    """Assembles a complete HTML document from the shared chrome plus page-specific body and scripts."""
    footer = (
        f'<footer class="app-footer">{html.escape(APP_NAME)} v{APP_VERSION} · {html.escape(APP_TAGLINE)}.</footer>'
        if show_footer
        else ""
    )
    script_tag = f"<script>{scripts_js}</script>" if scripts_js else ""
    body_cls = f' class="{body_class}"' if body_class else ""
    return (
        '<!DOCTYPE html><html lang="en">'
        f"{render_head(title, extra_css)}"
        f"<body{body_cls}>"
        f"{render_header(header_title, header_actions_html)}"
        f"{body_html}"
        f"{footer}"
        f"{script_tag}"
        "</body></html>"
    )


def render_error_page(status_code: int, title: str, message: str) -> str:
    """Renders a minimal, branded error page (e.g., session or artifact not found)."""
    body = (
        '<main class="error-page">'
        f'<div class="error-page__code">{int(status_code)}</div>'
        f"<h1>{html.escape(title)}</h1>"
        f"<p>{html.escape(message)}</p>"
        f'<a class="btn btn-primary" href="/">{icon("arrow-right")} Back to generator</a>'
        "</main>"
    )
    return render_page(title=title, body_html=body, header_title="", show_footer=False)
