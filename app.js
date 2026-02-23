/**
 * NDSS 2026 Personal Agenda – app.js
 * No build step. Runs directly on GitHub Pages.
 *
 * Architecture:
 *   - Loads /data/ndss2026.program.json on startup.
 *   - Reads URL hash (#a=<base64url>) on load; syncs with localStorage.
 *   - Renders Browse view (search/filter/star) and Agenda view (conflicts).
 *   - Stars are session-level (all-or-nothing per session).
 *
 * localStorage key:  ndss2026_starred_talks  → JSON array of session_ids
 * URL hash:          #a=<base64url(JSON array of session_ids)>
 */

'use strict';

/* ── Constants ───────────────────────────────────────────────────────── */
const DATA_URL       = 'data/ndss2026.program.json';
const LS_KEY         = 'ndss2026_starred_talks';
const HASH_PARAM     = 'a';

/* ── State ───────────────────────────────────────────────────────────── */
let programData  = null;   // parsed JSON from server
let starred      = new Set(); // set of session_ids

/* ── Encoding helpers ────────────────────────────────────────────────── */

/**
 * Encode an array of session_id strings to a URL-safe base64 string.
 * We use a simple approach: JSON → UTF-8 bytes → base64url.
 */
function encodeStarred(ids) {
  if (ids.length === 0) return '';
  const json = JSON.stringify([...ids].sort());
  // btoa works on byte strings; we encode via encodeURIComponent trick
  const b64 = btoa(unescape(encodeURIComponent(json)));
  // make base64url (no padding issues in hash)
  return b64.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

/**
 * Decode a base64url string back to an array of session_ids.
 * Returns [] on any error.
 */
function decodeStarred(encoded) {
  if (!encoded) return [];
  try {
    const b64 = encoded.replace(/-/g, '+').replace(/_/g, '/');
    const json = decodeURIComponent(escape(atob(b64)));
    const arr  = JSON.parse(json);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

/* ── URL hash persistence ────────────────────────────────────────────── */

function readHashStarred() {
  const hash = window.location.hash.slice(1); // strip '#'
  if (!hash) return null;
  const params = new URLSearchParams(hash);
  const encoded = params.get(HASH_PARAM);
  if (!encoded) return null;
  return decodeStarred(encoded);
}

function writeHash(ids) {
  if (ids.length === 0) {
    // clear hash without triggering a scroll
    history.replaceState(null, '', window.location.pathname + window.location.search);
    return;
  }
  const encoded = encodeStarred(ids);
  history.replaceState(null, '', '#' + HASH_PARAM + '=' + encoded);
}

/* ── localStorage persistence ────────────────────────────────────────── */

function loadStarredFromLS() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

function saveStarredToLS(ids) {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify([...ids]));
  } catch {
    // storage may be unavailable (private mode etc.)
  }
}

/* ── Time helpers ────────────────────────────────────────────────────── */

/** Parse "HH:MM" to minutes-since-midnight (integer). Returns null if invalid. */
function toMinutes(timeStr) {
  if (!timeStr || !timeStr.includes(':')) return null;
  const [h, m] = timeStr.split(':').map(Number);
  if (isNaN(h) || isNaN(m)) return null;
  return h * 60 + m;
}

/** Return true if two sessions overlap (both must have valid start+end). */
function overlaps(a, b) {
  const aStart = toMinutes(a.start);
  const aEnd   = toMinutes(a.end);
  const bStart = toMinutes(b.start);
  const bEnd   = toMinutes(b.end);
  if (aStart === null || aEnd === null || bStart === null || bEnd === null) return false;
  if (aStart === aEnd || bStart === bEnd) return false; // point-in-time (breaks etc.)
  return aStart < bEnd && bStart < aEnd;
}

/** Given a list of sessions, return a Set of session_ids that conflict. */
function findConflicts(sessions) {
  const conflicting = new Set();
  for (let i = 0; i < sessions.length; i++) {
    for (let j = i + 1; j < sessions.length; j++) {
      if (overlaps(sessions[i], sessions[j])) {
        conflicting.add(sessions[i].session_id);
        conflicting.add(sessions[j].session_id);
      }
    }
  }
  return conflicting;
}

/* ── DOM helpers ─────────────────────────────────────────────────────── */

let toastTimer = null;
function showToast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

/** Build a session card DOM element. */
function buildSessionCard(session, day, { showConflict = false } = {}) {
  const isStarred = starred.has(session.session_id);

  const card = document.createElement('div');
  card.className = 'session-card' +
    (isStarred   ? ' starred'  : '') +
    (showConflict ? ' conflict' : '');
  card.dataset.sessionId = session.session_id;
  card.setAttribute('role', 'listitem');

  // ── Star button
  const starBtn = document.createElement('button');
  starBtn.className = 'star-btn' + (isStarred ? ' on' : '');
  starBtn.setAttribute('aria-label', (isStarred ? 'Unstar' : 'Star') + ' session');
  starBtn.title = isStarred ? 'Unstar session' : 'Star session';
  starBtn.textContent = '★';

  // ── Meta
  const meta = document.createElement('div');
  meta.className = 'session-meta';

  const titleEl = document.createElement('div');
  titleEl.className = 'session-title';
  if (session.url) {
    const a = document.createElement('a');
    a.href = session.url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.textContent = session.title;
    titleEl.appendChild(a);
  } else {
    titleEl.textContent = session.title;
  }

  const info = document.createElement('div');
  info.className = 'session-info';

  if (session.start) {
    const timeSpan = document.createElement('span');
    timeSpan.textContent = session.end
      ? `🕐 ${session.start} – ${session.end}`
      : `🕐 ${session.start}`;
    info.appendChild(timeSpan);
  }
  if (session.track) {
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = session.track;
    info.appendChild(badge);
  }
  if (session.room) {
    const roomSpan = document.createElement('span');
    roomSpan.textContent = `📍 ${session.room}`;
    info.appendChild(roomSpan);
  }
  if (showConflict) {
    const cb = document.createElement('span');
    cb.className = 'conflict-badge';
    cb.textContent = '⚠ Conflict';
    info.appendChild(cb);
  }

  meta.appendChild(titleEl);
  meta.appendChild(info);

  // ── Items (expandable)
  if (session.items && session.items.length > 0) {
    const expandBtn = document.createElement('button');
    expandBtn.className = 'expand-btn';
    expandBtn.textContent = `▶ ${session.items.length} paper${session.items.length !== 1 ? 's' : ''}`;

    const itemsDiv = document.createElement('div');
    itemsDiv.className = 'session-items';

    const ol = document.createElement('ol');
    for (const item of session.items) {
      const li = document.createElement('li');
      const titleDiv = document.createElement('div');
      titleDiv.className = 'item-title';
      if (item.url) {
        const a = document.createElement('a');
        a.href = item.url;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = item.title;
        titleDiv.appendChild(a);
      } else {
        titleDiv.textContent = item.title;
      }
      li.appendChild(titleDiv);
      if (item.authors) {
        const auth = document.createElement('span');
        auth.className = 'item-authors';
        auth.textContent = item.authors;
        li.appendChild(auth);
      }
      ol.appendChild(li);
    }
    itemsDiv.appendChild(ol);

    expandBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const open = itemsDiv.classList.toggle('open');
      expandBtn.textContent = (open ? '▼ ' : '▶ ') +
        `${session.items.length} paper${session.items.length !== 1 ? 's' : ''}`;
    });

    meta.appendChild(expandBtn);
    card.appendChild(itemsDiv); // append items outside header flow
  }

  // ── Header row (star + meta)
  const header = document.createElement('div');
  header.className = 'session-header';
  header.appendChild(starBtn);
  header.appendChild(meta);
  card.insertBefore(header, card.firstChild);

  // ── Star interaction
  starBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleStar(session.session_id);
    // update this card in place
    const nowStarred = starred.has(session.session_id);
    starBtn.className  = 'star-btn' + (nowStarred ? ' on' : '');
    starBtn.setAttribute('aria-label', (nowStarred ? 'Unstar' : 'Star') + ' session');
    starBtn.title = nowStarred ? 'Unstar session' : 'Star session';
    card.classList.toggle('starred', nowStarred);
    // also refresh agenda if visible
    if (document.getElementById('view-agenda').classList.contains('active')) {
      renderAgenda();
    }
  });

  return card;
}

/* ── Star management ─────────────────────────────────────────────────── */

function toggleStar(sessionId) {
  if (starred.has(sessionId)) {
    starred.delete(sessionId);
  } else {
    starred.add(sessionId);
  }
  persist();
}

function persist() {
  const ids = [...starred];
  saveStarredToLS(ids);
  writeHash(ids);
}

/* ── Browse view ─────────────────────────────────────────────────────── */

let browseQuery  = '';
let browseDay    = '';
let browseStarred = false;

function renderBrowse() {
  const container = document.getElementById('browse-list');
  container.innerHTML = '';

  if (!programData) return;

  const q = browseQuery.toLowerCase().trim();
  let totalShown = 0;

  for (const day of programData.days) {
    if (browseDay && day.day_id !== browseDay) continue;

    const filtered = day.sessions.filter(s => {
      if (browseStarred && !starred.has(s.session_id)) return false;
      if (!q) return true;
      if (s.title.toLowerCase().includes(q)) return true;
      if (s.track.toLowerCase().includes(q)) return true;
      if (s.room.toLowerCase().includes(q)) return true;
      if (s.items.some(i =>
        i.title.toLowerCase().includes(q) ||
        (i.authors && i.authors.toLowerCase().includes(q))
      )) return true;
      return false;
    });

    if (filtered.length === 0) continue;
    totalShown += filtered.length;

    const group = document.createElement('div');
    group.className = 'day-group';

    const h2 = document.createElement('h2');
    h2.textContent = day.label;
    group.appendChild(h2);

    for (const session of filtered) {
      group.appendChild(buildSessionCard(session, day));
    }
    container.appendChild(group);
  }

  document.getElementById('session-count').textContent =
    `${totalShown} session${totalShown !== 1 ? 's' : ''}`;
}

/* ── Agenda view ─────────────────────────────────────────────────────── */

function renderAgenda() {
  const container = document.getElementById('agenda-list');
  const emptyMsg  = document.getElementById('agenda-empty');
  const conflictBadge = document.getElementById('conflict-badge');
  container.innerHTML = '';

  if (!programData) return;

  let hasAny = false;
  let hasConflicts = false;

  for (const day of programData.days) {
    const daySessions = day.sessions.filter(s => starred.has(s.session_id));
    if (daySessions.length === 0) continue;
    hasAny = true;

    // Sort by start time
    daySessions.sort((a, b) => {
      const am = toMinutes(a.start) ?? 0;
      const bm = toMinutes(b.start) ?? 0;
      return am - bm;
    });

    const conflicts = findConflicts(daySessions);
    if (conflicts.size > 0) hasConflicts = true;

    const group = document.createElement('div');
    group.className = 'day-group';

    const h2 = document.createElement('h2');
    h2.textContent = day.label;
    group.appendChild(h2);

    for (const session of daySessions) {
      group.appendChild(
        buildSessionCard(session, day, { showConflict: conflicts.has(session.session_id) })
      );
    }
    container.appendChild(group);
  }

  emptyMsg.classList.toggle('hidden', hasAny);
  conflictBadge.classList.toggle('hidden', !hasConflicts);
}

/* ── Initialisation ──────────────────────────────────────────────────── */

async function init() {
  // 1. Load data
  let json;
  try {
    const resp = await fetch(DATA_URL);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    json = await resp.json();
  } catch (err) {
    document.getElementById('browse-list').innerHTML =
      `<p style="color:#dc2626;padding:1rem">Failed to load program data: ${err.message}</p>`;
    return;
  }
  programData = json;

  // 2. Resolve starred: URL hash wins; fall back to localStorage
  const fromHash = readHashStarred();
  if (fromHash !== null && fromHash.length > 0) {
    starred = new Set(fromHash);
    saveStarredToLS(fromHash); // sync to LS
  } else {
    starred = new Set(loadStarredFromLS());
  }

  // 3. Populate day filter
  const dayFilter = document.getElementById('day-filter');
  for (const day of programData.days) {
    const opt = document.createElement('option');
    opt.value = day.day_id;
    opt.textContent = day.label;
    dayFilter.appendChild(opt);
  }

  // 4. Wire up controls
  document.getElementById('search-input').addEventListener('input', e => {
    browseQuery = e.target.value;
    renderBrowse();
  });

  dayFilter.addEventListener('change', e => {
    browseDay = e.target.value;
    renderBrowse();
  });

  document.getElementById('filter-starred').addEventListener('change', e => {
    browseStarred = e.target.checked;
    renderBrowse();
  });

  document.getElementById('btn-browse').addEventListener('click', () => switchView('browse'));
  document.getElementById('btn-agenda').addEventListener('click', () => switchView('agenda'));

  document.getElementById('btn-share').addEventListener('click', shareLink);
  document.getElementById('btn-reset').addEventListener('click', resetAgenda);

  // 5. Initial render
  renderBrowse();
  renderAgenda();

  // 6. If hash pointed to agenda, switch to agenda view
  if (readHashStarred() !== null) {
    switchView('agenda');
  }
}

/* ── View switching ──────────────────────────────────────────────────── */

function switchView(name) {
  const isBrowse = name === 'browse';
  document.getElementById('view-browse').classList.toggle('active', isBrowse);
  document.getElementById('view-agenda').classList.toggle('active', !isBrowse);
  document.getElementById('btn-browse').classList.toggle('active', isBrowse);
  document.getElementById('btn-agenda').classList.toggle('active', !isBrowse);
  document.getElementById('btn-browse').setAttribute('aria-pressed', String(isBrowse));
  document.getElementById('btn-agenda').setAttribute('aria-pressed', String(!isBrowse));
  if (!isBrowse) renderAgenda();
}

/* ── Share link ──────────────────────────────────────────────────────── */

function shareLink() {
  const url = window.location.href;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(
      () => showToast('✓ Link copied to clipboard'),
      () => fallbackCopy(url)
    );
  } else {
    fallbackCopy(url);
  }
}

function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity  = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try {
    document.execCommand('copy');
    showToast('✓ Link copied to clipboard');
  } catch {
    showToast('Copy failed – please copy the URL manually');
  }
  document.body.removeChild(ta);
}

/* ── Reset agenda ────────────────────────────────────────────────────── */

function resetAgenda() {
  if (!confirm('Remove all starred sessions?')) return;
  starred.clear();
  persist();
  renderBrowse();
  renderAgenda();
  showToast('Agenda cleared');
}

/* ── Boot ────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', init);
