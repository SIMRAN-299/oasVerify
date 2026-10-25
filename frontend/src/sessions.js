/**
 * Client-side session registry.
 *
 * The backend identifies a session by a UUID; the UI shows a human-friendly name
 * ("Session 1", "Session 2", … or a user-chosen label). The mapping lives in
 * localStorage so names are stable across views and reloads. Nothing here changes
 * which id is sent to the API.
 */
const SESSIONS_KEY = 'oas_sessions_v1';
const COUNTER_KEY = 'oas_session_counter';
export const SESSIONS_EVENT = 'oas:sessions-changed';
export const MAX_RECENT = 6;

function read() {
  try {
    const raw = localStorage.getItem(SESSIONS_KEY);
    const list = raw ? JSON.parse(raw) : [];
    return Array.isArray(list) ? list.filter((s) => s && typeof s.id === 'string') : [];
  } catch (err) {
    return [];
  }
}

function write(list) {
  try {
    localStorage.setItem(SESSIONS_KEY, JSON.stringify(list));
  } catch (err) {
    /* storage unavailable — names simply won't persist */
  }
  try {
    window.dispatchEvent(new Event(SESSIONS_EVENT));
  } catch (err) {
    /* ignore */
  }
}

function nextNumber(list) {
  let n = 0;
  try {
    n = parseInt(localStorage.getItem(COUNTER_KEY) || '0', 10) || 0;
  } catch (err) {
    n = 0;
  }
  // Never reuse a number already present in the list (e.g. counter cleared but sessions kept)
  const highest = list.reduce((m, s) => Math.max(m, Number(s.number) || 0), 0);
  n = Math.max(n, highest) + 1;
  try {
    localStorage.setItem(COUNTER_KEY, String(n));
  } catch (err) {
    /* ignore */
  }
  return n;
}

function compact(obj) {
  const out = {};
  Object.entries(obj || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') out[k] = v;
  });
  return out;
}

/** Newest first. */
export function getSessions() {
  return read().sort((a, b) => (b.createdAt || 0) - (a.createdAt || 0));
}

export function getSession(id) {
  if (!id) return null;
  return read().find((s) => s.id === id) || null;
}

/** Returns the record for `id`, creating "Session N" on first sight. Merges any metadata given. */
export function ensureSession(id, meta = {}) {
  if (!id) return null;
  const list = read();
  let rec = list.find((s) => s.id === id);
  if (!rec) {
    const number = nextNumber(list);
    rec = { id, number, name: `Session ${number}`, createdAt: Date.now(), ...compact(meta) };
    list.push(rec);
    write(list);
  } else {
    const patch = compact(meta);
    const changed = Object.keys(patch).some((k) => rec[k] !== patch[k]);
    if (changed) {
      Object.assign(rec, patch);
      write(list);
    }
  }
  return rec;
}

export function updateSession(id, patch) {
  return ensureSession(id, patch);
}

export function renameSession(id, name) {
  const clean = String(name || '').trim().slice(0, 48);
  if (!id || !clean) return getSession(id);
  const list = read();
  const rec = list.find((s) => s.id === id);
  if (!rec) return null;
  rec.name = clean;
  write(list);
  return rec;
}

export function removeSession(id) {
  const list = read();
  const next = list.filter((s) => s.id !== id);
  if (next.length !== list.length) write(next);
}

/** Display label for an id — falls back gracefully for ids the registry hasn't seen. */
export function sessionLabel(id) {
  const rec = getSession(id);
  return rec ? rec.name : 'Session';
}
