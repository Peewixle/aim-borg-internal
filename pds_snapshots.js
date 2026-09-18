/* ==========================================================================
   pds_snapshots.js — BG-71 (Snapshot Management from the Store).

   Opened from the main menu (BG-72), not from the picker. The picker lists
   snapshots and selects one; it carries no load or delete control, because a
   delete sitting next to a selection control is how a snapshot gets removed by
   accident.

   THE LIST IS READ WHEN IT IS SHOWN. Not baked into the page, not fetched once
   at startup. A snapshot loaded a moment ago is in it.
   ========================================================================== */
'use strict';

const SNAPMGR = (() => {

  const state = { open: false, snapshots: [], files: [], load: null,
                  busy: false, error: null, confirming: null };

  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  async function api(path, body) {
    const r = await fetch(path, body ? {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    } : undefined);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  async function refresh() {
    const [a, b] = await Promise.all([
      api('/api/snapshots'), api('/api/snapshots/files')
    ]);
    state.snapshots = a.snapshots || [];
    state.files = b.files || [];
    state.dir = b.dir;
  }

  async function open() {
    state.open = true; state.error = null; state.confirming = null;
    paint('<div class="snap-note">Reading the store\u2026</div>');
    try { await refresh(); } catch (e) { state.error = e.message; }
    paint();
  }

  function close() {
    state.open = false;
    const p = document.getElementById('snapPanel');
    if (p) p.classList.remove('on');
  }

  /* ------------------------------------------------------------- loading */

  /** Starts a load and polls until it finishes. Does not hold the interface:
   *  the surface stays usable and the load reports as it goes. */
  async function load(file) {
    state.busy = true; state.error = null;
    state.load = { file, state: 'running', output: [] };
    paint();
    try {
      const { load: id } = await api('/api/snapshots/load', { file });
      for (;;) {
        await new Promise(r => setTimeout(r, 900));
        const st = await api(`/api/snapshots/load-status?load=${encodeURIComponent(id)}`);
        state.load = st;
        paint();
        if (st.state !== 'running') break;
      }
      // The list carries the new snapshot when the load finishes — here, and
      // in the picker behind this surface, which is read from the same store.
      if (state.load.state === 'done') {
        await refresh();
        if (typeof window !== 'undefined' && window.AIM_REFRESH_SNAPSHOTS)
          await window.AIM_REFRESH_SNAPSHOTS();
      }
    } catch (e) {
      state.error = e.message;
    }
    state.busy = false;
    paint();
  }

  async function del(id) {
    state.busy = true; state.error = null; paint();
    try {
      await api('/api/snapshots/delete', { snapshot: id });
      state.confirming = null;
      await refresh();
    } catch (e) {
      // A design edits it or derives from it. The refusal NAMES the design, so
      // it is shown as given rather than reduced to "could not delete".
      state.error = e.message;
      state.confirming = null;
    }
    state.busy = false; paint();
  }

  async function retire(id, on) {
    state.busy = true; state.error = null; paint();
    try { await api('/api/snapshots/retire', { snapshot: id, retired: on });
          await refresh(); }
    catch (e) { state.error = e.message; }
    state.busy = false; paint();
  }

  /* ----------------------------------------------------------- rendering */

  function snapshotRows() {
    if (!state.snapshots.length)
      return '<tr><td colspan="7" class="snap-note">The store holds no snapshots.</td></tr>';
    return state.snapshots.map(s => {
      const confirming = state.confirming === s.id;
      // Confirmation names the snapshot and its counts, so what is about to be
      // removed is on screen at the moment of confirming rather than remembered
      // from the row above.
      if (confirming) {
        return `<tr class="snap-confirm"><td colspan="7">
          Delete <b>${esc(s.source)}</b> &mdash; ${s.profiles} profiles,
          ${s.rules} rules, and the findings computed against it?
          <button class="snap-danger" data-del="${s.id}">Delete</button>
          <button class="snap-plain" data-cancel="1">Cancel</button></td></tr>`;
      }
      return `<tr${s.retired ? ' class="snap-retired"' : ''}>
        <td class="num">${s.id}</td>
        <td>${esc(s.source)}${s.retired
              ? ' <span class="snap-tag">superseded</span>' : ''}</td>
        <td>${esc(s.kind)}</td>
        <td class="num">${s.profiles}</td>
        <td class="num">${s.rules}</td>
        <td class="dim">${esc(s.exportedAt || '')}</td>
        <td class="snap-acts">
          <button class="snap-plain" data-retire="${s.id}" data-on="${s.retired ? 0 : 1}"
            >${s.retired ? 'Restore' : 'Retire'}</button>
          ${s.deletable
            ? `<button class="snap-plain" data-confirm="${s.id}">Delete</button>`
            : `<span class="snap-held" title="${esc((s.heldBy[0] || {}).name || '')}"
                >in use</span>`}
        </td></tr>`;
    }).join('');
  }

  function fileRows() {
    if (!state.files.length)
      return `<div class="snap-note">No exports on ${esc(state.dir || 'the data drive')}.</div>`;
    return `<table class="rules"><thead><tr><th>File</th><th>Modified</th>
      <th class="num">Size</th><th></th></tr></thead><tbody>${
      state.files.map(f => `<tr>
        <td>${esc(f.name)}${f.alreadyLoaded
              ? ' <span class="snap-tag">already loaded</span>' : ''}</td>
        <td class="dim">${esc(f.modified)}</td>
        <td class="num dim">${Math.round(f.bytes / 1024)} KB</td>
        <td><button class="snap-plain" data-load="${esc(f.name)}"
              ${state.busy ? 'disabled' : ''}>Load</button></td>
      </tr>`).join('')}</tbody></table>`;
  }

  /** What the loader says reaches the screen. Its refusals are the most useful
   *  thing it produces — the missing columns it named, the banner date it could
   *  not read, the reconciliation count — and none of it reached a screen
   *  before. */
  function loadPanel() {
    const l = state.load;
    if (!l) return '';
    const cls = l.state === 'failed' ? 'snap-fail'
              : l.state === 'done' ? 'snap-done' : 'snap-running';
    return `<div class="snap-load ${cls}">
      <div class="snap-load-head">${esc(l.file)} &mdash; ${
        l.state === 'running' ? 'loading\u2026'
        : l.state === 'done' ? `loaded as snapshot ${l.snapshot}` : 'refused'}</div>
      ${(l.output || []).length
        ? `<pre class="snap-out">${(l.output || []).map(esc).join('\n')}</pre>` : ''}
      ${l.error ? `<div class="snap-err">${esc(l.error)}</div>` : ''}</div>`;
  }

  function paint(override) {
    const panel = document.getElementById('snapPanel');
    const card = document.getElementById('snapCard');
    if (!panel || !card) return;
    panel.classList.toggle('on', state.open);
    if (override) { card.innerHTML = override; return; }
    card.innerHTML = `
      <div class="snap-head">
        <h2>Snapshot Management</h2>
        <button class="snap-close" id="snapClose">Close &nbsp;esc</button>
      </div>
      ${state.error ? `<div class="snap-err">${esc(state.error)}</div>` : ''}
      <div class="snap-scroll">
        <div class="snap-sec">In the store</div>
        <table class="rules"><thead><tr><th>#</th><th>Source</th><th>Kind</th>
          <th class="num">Profiles</th><th class="num">Rules</th>
          <th>Exported</th><th></th></tr></thead>
          <tbody>${snapshotRows()}</tbody></table>
        <div class="snap-sec">On the data drive
          <span class="dim">placed there outside the Borg</span></div>
        ${fileRows()}
        ${loadPanel()}
      </div>`;
  }

  return { open, close, load, del, retire, paint, state,
           get isOpen() { return state.open; } };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = SNAPMGR;
if (typeof window !== 'undefined') window.SNAPMGR = SNAPMGR;
