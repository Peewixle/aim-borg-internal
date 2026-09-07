/* ==========================================================================
   pds_studio.js — the authoring layer.

   Separate from browser_shell.html on purpose. The browser inspects; the
   studio edits. Growing the browser file to hold both would put read-only
   display and durable mutation in one place, and the demo build needs the
   first without the second.

   WHAT IS HERE AND WHAT IS NOT
   ============================
   No checks. No change-set derivation. Both live on the server, because two
   implementations of these checks demonstrably diverged and a third in
   JavaScript would be the same mistake again. This file sends actions and
   renders what comes back.

   DEMO MODE
   =========
   The studio renders in demo, and every authoring action is unavailable.
   PDS being visible is the point — a prospect sees the Studio exists and how
   it works — but nothing persists, because there is nothing to persist to.
   Read-only is decided by the payload, not by feature detection: a failed
   request must not be mistaken for demo, or a dropped connection silently
   turns the product into a demo.
   ========================================================================== */
'use strict';

const PDS = (() => {

  const AUTOSAVE_MS = 1500;   // idle window; a burst collapses into one save

  const state = {
    enabled:   false,   // studio present at all
    writable:  false,   // this session holds the single-writer claim
    design:    null,    // design id
    base:      null,    // base snapshot id
    heldBy:    null,
    pending:   [],      // actions made but not yet written
    inflight:  false,
    lastSaved: null,
    error:     null,
    changes:   null,    // last change set from the server
    findings:  null,    // last findings from the server
    readme:    '',
    timer:     null,
  };

  const listeners = [];
  const onChange = fn => listeners.push(fn);
  const emit = () => listeners.forEach(f => { try { f(state); } catch (e) {} });

  /* ---------------------------------------------------------------- init */

  function init(payload) {
    // The payload decides. A demo build carries studio.mode === 'demo' and no
    // design; the product carries a design id and writable state from the
    // server. Never inferred from whether a fetch succeeds.
    const s = (payload && payload.studio) || null;
    state.enabled  = !!s;
    state.design   = s ? s.design : null;
    state.base     = s ? s.base : null;
    state.writable = !!(s && s.mode === 'live' && s.writable);
    state.heldBy   = s ? s.heldBy : null;
    state.readme   = (s && s.readme) || '';
    state.name     = s ? s.name : null;
    state.mode     = s ? s.mode : 'none';
    emit();
    return state;
  }

  /* ------------------------------------------------------------ actions */

  function can(action) {
    if (!state.enabled) return { ok: false, why: 'the studio is not available' };
    // ORDER: most specific reason first. Demo has no design either, so
    // checking for a design before checking the mode told a demo user to
    // create one — which they cannot do, and which is not why they are
    // blocked.
    //
    // Checked before the writable gate for the MESSAGE, not as a second
    // barrier: writable can never be true in demo, so this never changes
    // whether an action is refused, only what the person is told.
    if (state.mode === 'demo')
      return { ok: false, why: 'demo mode — the studio is read-only and nothing is saved' };
    // No design open is a distinct state from read-only. The reason has to say
    // so, or the only control that IS available — creating one — reads as
    // broken rather than as the next step.
    if (!state.design)
      return { ok: false, why: 'no design is open — create one to start authoring' };
    if (!state.writable)
      return { ok: false, why: state.heldBy
        ? `${state.heldBy} is editing this design` : 'this design is read-only' };
    return { ok: true };
  }

  /** Queue a move. Nothing is sent yet: consecutive changes without a pause
   *  are written as one save, so twenty rules dragged to System is one entry
   *  in the history rather than twenty a second apart. */
  function queue(action) {
    const g = can(action.action);
    if (!g.ok) return g;
    state.pending.push(action);
    state.error = null;
    schedule();
    emit();
    return { ok: true };
  }

  const promote = (rule, tier) => queue({ rule, action: 'promote', tier });
  const demote  = (rule, tier) => queue({ rule, action: 'demote',  tier });
  const remove  = rule         => queue({ rule, action: 'delete' });
  const revert  = rule         => queue({ rule, action: 'revert' });

  function schedule() {
    if (state.timer) clearTimeout(state.timer);
    state.timer = setTimeout(flush, AUTOSAVE_MS);
  }

  /** Write whatever is queued. Called on idle, and forced before the design is
   *  left, closed or marked complete, so the debounce window cannot outlive
   *  the session. */
  async function flush() {
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    if (!state.pending.length || state.inflight) return { ok: true, applied: 0 };
    const batch = state.pending.slice();
    state.inflight = true;
    emit();
    try {
      const r = await post('/api/design/apply',
                           { design: state.design, actions: batch });
      // Only clear what was actually sent: anything queued during the request
      // must survive to the next save.
      state.pending = state.pending.slice(batch.length);
      state.lastSaved = new Date();
      state.error = null;
      state.summary = r.summary;
      return { ok: true, applied: r.applied };
    } catch (e) {
      // The change stays queued. The indicator must never report saved while
      // a write is pending or has failed — silent failure under autosave is
      // worse than under a button, because nobody is watching for it.
      state.error = e.message || String(e);
      return { ok: false, error: state.error };
    } finally {
      state.inflight = false;
      emit();
    }
  }

  /* -------------------------------------------------------------- status */

  /** BG-61: the state of the design relative to the store is visible at all
   *  times, and a failure to persist is surfaced rather than silent. */
  function status() {
    if (!state.enabled)            return { kind: 'none',    text: '' };
    if (state.mode === 'demo')     return { kind: 'demo',    text: 'Demo — changes are not saved' };
    if (state.error)               return { kind: 'error',   text: 'Not saved — ' + state.error };
    if (state.inflight)            return { kind: 'saving',  text: 'Saving…' };
    if (state.pending.length)      return { kind: 'unsaved', text: 'Unsaved changes' };
    if (!state.writable)           return { kind: 'readonly',
                                            text: state.heldBy ? `Read-only — ${state.heldBy} is editing`
                                                               : 'Read-only' };
    if (state.lastSaved)           return { kind: 'saved',
                                            text: 'Saved ' + state.lastSaved.toLocaleTimeString() };
    return { kind: 'clean', text: 'No changes' };
  }

  /* ------------------------------------------------------- server calls */

  async function post(path, body) {
    const res = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || data.blocked || `HTTP ${res.status}`);
    return data;
  }

  async function get(path) {
    const res = await fetch(path);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  async function changeSet() {
    await flush();                       // never show a stale change set
    const r = await get(`/api/design/changeset?design=${state.design}`);
    state.changes = r;
    emit();
    return r;
  }

  async function runChecks() {
    await flush();                       // check what is stored, not what was typed
    const r = await get(`/api/design/checks?design=${state.design}`);
    state.findings = r;
    emit();
    return r;
  }

  async function history() {
    return get(`/api/design/history?design=${state.design}`);
  }

  async function saveReadme(text) {
    state.readme = text;
    await post('/api/design/readme', { design: state.design, text });
    emit();
  }

  /** BG-57. A design is created from a production dump; the studio then edits
   *  the copy. Reloads afterwards, because the server decides writability and
   *  the claim, and the page has to be told rather than assume. */
  async function createDesign(base, name) {
    const r = await post('/api/design/create', { base, name });
    if (typeof location !== 'undefined') location.reload();
    return r;
  }

  async function openDesign(id) {
    await post('/api/design/claim', { design: id });
    if (typeof location !== 'undefined') location.reload();
  }

  async function designs() {
    return get('/api/designs');
  }

  async function complete() {
    // Forced write first: marking complete with a change still in the debounce
    // window would finish a design that does not include it.
    await flush();
    const r = await post('/api/design/complete', { design: state.design });
    state.writable = false;
    state.summary = r.summary;
    emit();
    return r;
  }

  async function exportScript() {
    const r = await post('/api/design/export', { design: state.design });
    return r;
  }

  /* --------------------------------------------------------- rendering */

  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  /** Controls for one rule row. Rendered in every mode: the studio must be
   *  visible in demo, just inert. Disabled with a reason rather than hidden,
   *  so a prospect sees what the product does. */
  function ruleControls(rule, tier) {
    const g = can('promote');
    const dis = g.ok ? '' : ' disabled';
    const title = g.ok ? '' : ` title="${esc(g.why)}"`;
    const TIERS = ['System', 'Organization', 'Customer', 'Agency'];
    const i = TIERS.indexOf(tier);
    const up = TIERS.slice(0, i), down = TIERS.slice(i + 1);
    return `<span class="pds-ctl"${title}>` +
      up.map(t => `<button class="pds-up" data-rule="${esc(rule)}" data-tier="${t}"${dis}
                    title="Promote to ${t}">&uarr;${t[0]}</button>`).join('') +
      down.map(t => `<button class="pds-down" data-rule="${esc(rule)}" data-tier="${t}"${dis}
                    title="Demote to ${t}">&darr;${t[0]}</button>`).join('') +
      `<button class="pds-del" data-rule="${esc(rule)}"${dis} title="Delete from the design">&times;</button>` +
      `</span>`;
  }

  /** The design bar: which design is open, or the control to start one.
   *  Rendered whenever a studio payload exists, so a product instance with no
   *  design shows the way forward rather than an empty tab strip. */
  function designBarHtml() {
    if (!state.enabled) return '';
    if (state.mode === 'demo')
      return `<div class="pds-bar">Demo — the studio is shown over invented data.
        Nothing is saved.</div>`;
    if (!state.design)
      return `<div class="pds-bar">No design is open.
        <button id="pdsNew" class="pds-primary">New design from the current dump</button>
        <span class="dim">A design copies the dump and is edited in place;
        the dump itself is never modified.</span></div>`;
    return `<div class="pds-bar">Design <b>${esc(state.name || state.design)}</b>
      ${state.writable ? '' : '<span class="dim">read-only</span>'}
      <button id="pdsChanges" class="pds-primary">Changes</button>
      <button id="pdsChecks" class="pds-primary">Run checks</button>
      ${state.writable ? '<button id="pdsComplete" class="pds-primary">Mark complete</button>' : ''}
      </div>`;
  }

  function statusHtml() {
    const s = status();
    if (s.kind === 'none') return '';
    return `<span class="pds-status pds-${s.kind}">${esc(s.text)}</span>`;
  }

  function changeSetHtml(cs) {
    if (!cs || !cs.changes) return '';
    if (!cs.changes.length)
      return `<div class="pds-empty">No changes yet. The design matches its base dump.</div>`;
    const rows = cs.changes.map(c => `<tr>
        <td><span class="pds-act pds-${c.action}">${esc(c.action)}</span></td>
        <td>${esc(c.profile)}</td>
        <td>${esc(c.field)} <span class="dim">(${esc(c.tab)} tab)</span></td>
        <td>${esc(c.from_tier)} &rarr; ${esc(c.to_tier || '—')}</td></tr>`).join('');
    const n = cs.summary ? cs.summary.counts : {};
    return `<div class="pds-cs"><div class="pds-cs-head">${
        Object.entries(n).map(([k, v]) => `${v} ${k}`).join(' · ') ||
        'no change'}</div>
      <table class="rules"><thead><tr><th>Action</th><th>Profile</th>
      <th>Destination</th><th>Level</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  /** BG-64. Scoped to the open design, states its base, names the check and
   *  the story behind each finding, and lists what did not run. */
  function findingsHtml(f) {
    if (!f) return '';
    const counts = Object.entries(f.counts || {})
      .map(([k, v]) => `${v} ${k}`).join(' · ') || 'none';
    const rows = (f.findings || []).map(x => `<tr>
        <td><span class="fbadge ${esc(x.severity)}">${esc(x.severity)}</span></td>
        <td>${esc(x.check)}</td>
        <td>${esc(x.entity || '')}</td>
        <td>${esc(x.subject)}</td>
        <td class="dim">${esc(x.text)}${x.attributed
            ? `<div class="pds-attr">from save ${x.attributed.save} by ${esc(x.attributed.by)}</div>`
            : (x.involves ? `<div class="pds-attr">involves ${x.involves.length} rules; cause not established</div>` : '')}</td>
      </tr>`).join('');
    return `<div class="pds-find">
      <div class="pds-cs-head">Design ${esc(f.design)} · base snapshot ${esc(f.base)}
        · ${esc(counts)} · checked ${esc(f.ranAt || '')}</div>
      <table class="rules"><thead><tr><th>Severity</th><th>Check</th>
      <th>Entity</th><th>Subject</th><th>Finding</th></tr></thead>
      <tbody>${rows}</tbody></table></div>`;
  }

  /* ------------------------------------------------------------ wiring */

  /** Delegated, because rule rows are re-rendered on every view change and
   *  per-button handlers would be lost each time. */
  function bind(root) {
    root.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b || b.disabled) return;
      const rule = b.dataset.rule;
      if (!rule) return;
      if (b.classList.contains('pds-up'))   promote(rule, b.dataset.tier);
      if (b.classList.contains('pds-down')) demote(rule, b.dataset.tier);
      if (b.classList.contains('pds-del'))  remove(rule);
    });

    // The design bar's own buttons carry no data-rule, so they are matched by
    // id rather than falling through the rule handler above.
    root.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b || b.disabled) return;
      if (b.id === 'pdsNew') {
        const name = (typeof prompt === 'function')
          ? prompt('Name this design', 'Standard convergence') : 'New design';
        // The base is the snapshot the browser is currently showing, which the
        // payload already knows. state.baseChoice was never wired to anything,
        // so this posted null and the server rejected it.
        if (name) createDesign(state.base, name).catch(err => {
          state.error = err.message; emit();
        });
      }
    });
    // Force the write before the tab goes away, so the debounce window cannot
    // outlive the session.
    if (typeof window !== 'undefined') {
      window.addEventListener('beforeunload', () => { if (state.pending.length) flush(); });
    }
  }

  return { init, queue, promote, demote, remove, revert, flush, status, can,
           createDesign, openDesign, designs, designBarHtml,
           changeSet, runChecks, history, saveReadme, complete, exportScript,
           ruleControls, statusHtml, changeSetHtml, findingsHtml, bind,
           onChange, state, AUTOSAVE_MS };
})();

// Exported both ways: `module.exports` for the node test harness, and on
// `window` for the browser, because a top-level const in a script tag is not a
// global and the shell looks it up by name.
if (typeof module !== 'undefined' && module.exports) module.exports = PDS;
if (typeof window !== 'undefined') window.PDS = PDS;
