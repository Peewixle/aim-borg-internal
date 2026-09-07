/* ==========================================================================
   pds_selection.js — BG-66 and BG-67.

   Two surfaces, one module. They share a shape — bands, entities, priority
   order — and differ in what a rule holds, so the rendering of a rule row is
   the only thing that varies. Two modules would have duplicated the banding.

   These are NOT the profile browser. The Payer Selection Rules and Profile
   Selection Rules nodes used to open it, which was wrong: the browser is for
   profiles. A selection rule appears in the browser only in the one case where
   it answers a question about a profile — BG-38's "Selected when" block, which
   stays.
   ========================================================================== */
'use strict';

const SELRULES = (() => {

  const state = { kind: null, data: null, open: false, onClose: null,
                  onProfile: null };

  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  async function get(path) {
    const r = await fetch(path);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  /** kind is 'payer' or 'profile'. onProfile is called when a reader selects a
   *  resolved target, so navigation stays the caller's business. */
  async function open(kind, snapshot, onClose, onProfile) {
    state.kind = kind;
    state.onClose = onClose || null;
    state.onProfile = onProfile || null;
    state.open = true;
    state.data = await get(`/api/selection/${kind}?snapshot=${snapshot}`);
    return state.data;
  }

  function close() {
    state.open = false;
    if (state.onClose) state.onClose();
  }

  /* ---------------------------------------------------------- rendering */

  function header() {
    const d = state.data;
    if (!d) return '';
    return `<div class="tr-head">
      <h2>${esc(d.title)}</h2>
      <span class="tr-meta">${d.total} rule${d.total === 1 ? '' : 's'}${
        d.kind === 'profile' && d.ambiguous
          ? ` · <b class="sel-warn">${d.ambiguous} with an ambiguous target</b>` : ''}${
        d.kind === 'profile' && d.missing
          ? ` · ${d.missing} targeting a missing profile` : ''}</span>
      <span class="tr-prov ${d.snapshot_kind === 'design' ? 'tr-design' : ''}">${
        esc(d.source)} — ${esc(d.snapshot_kind)}, snapshot ${d.snapshot}</span>
    </div>
    <div class="tr-tabs"><button class="tr-close" id="selClose">Close &nbsp;esc</button></div>
    <div class="tr-note-box">${esc(d.note)}${
      d.kind === 'payer'
        ? ` The payer is shown as the rule records it; payer resolution is ${esc(d.payer_resolution)}.`
        : ` ${esc(d.default_profile_note)}`}</div>`;
  }

  /** A payer rule row. Timing is rendered as the rule holds it — never
   *  inferred — because every rule in the estate carries the same value and a
   *  surface built to that could hard-code what is actually an attribute. */
  function payerRow(r) {
    return `<tr>
      <td class="num">${esc(r.priority)}</td>
      <td class="tr-field">${esc(r.payer)}</td>
      <td>${esc(r.form_type)}</td>
      <td class="${/before/i.test(r.timing) ? 'sel-before' : 'sel-after'}">${
        esc(r.timing)}</td>
      <td><span class="tr-rule" data-cond="${esc(r.condition)}">${
        esc(r.condition.slice(0, 70))}${r.condition.length > 70 ? '…' : ''}</span></td>
      <td class="dim">${esc(r.description)}</td></tr>`;
  }

  /** A profile rule row. The target is a NAME, and names are not unique, so
   *  resolution is one of three states and an ambiguous one is refused rather
   *  than opened to the first match. */
  function profileRow(r) {
    const res = r.resolution || { status: 'missing', candidates: [] };
    let target;
    if (res.status === 'resolved') {
      target = `<a class="sel-target" data-profile="${esc(res.profile)}"
                   title="open this profile">${esc(r.target)}</a>`;
    } else if (res.status === 'ambiguous') {
      target = `<span class="sel-amb" title="the target names more than one profile">${
        esc(r.target)}</span>
        <div class="sel-cands">${res.candidates.length} profiles carry this name —
          the rule does not say which:
          <ul>${res.candidates.map(c => `<li>${esc(c.tier)} ·
            ${esc(c.entity || 'AIM (System)')} · ${esc(c.kind)}</li>`).join('')}</ul>
          Not opened, because opening one of several would be a guess.</div>`;
    } else {
      target = `<span class="sel-missing" title="no profile of this name exists">${
        esc(r.target || '(none)')}</span>`;
    }
    return `<tr>
      <td class="num">${esc(r.priority)}</td>
      <td>${target}</td>
      <td><span class="tr-rule" data-cond="${esc(r.condition)}">${
        esc(r.condition.slice(0, 80))}${r.condition.length > 80 ? '…' : ''}</span></td>
      <td class="dim">${esc(r.description)}</td></tr>`;
  }

  function bands() {
    const d = state.data;
    if (!d) return '';
    const cols = d.kind === 'payer'
      ? ['Pri', 'Payer selected', 'Form type', 'Timing', 'Condition', 'Description']
      : ['Pri', 'Profile selected', 'Condition', 'Description'];
    const row = d.kind === 'payer' ? payerRow : profileRow;

    return d.bands.map(b => {
      // An empty band is rendered, not suppressed. In this estate three of
      // four are empty, and that is the information: these rules can exist at
      // any level, and here they do not.
      if (!b.count) {
        return `<div class="sel-band sel-${b.tier}">
          <div class="sel-bandhead">${esc(b.tier)}
            <span class="dim">no rules at this level</span></div></div>`;
      }
      const groups = b.entities.map(e => `
        <div class="sel-ent">${esc(e.entity)}
          <span class="dim">${e.count} rule${e.count === 1 ? '' : 's'},
            priority order</span></div>
        <table class="rules"><thead><tr>${
          cols.map(c => `<th>${c}</th>`).join('')}</tr></thead>
        <tbody>${e.rules.map(row).join('')}</tbody></table>`).join('');
      return `<div class="sel-band sel-${b.tier}">
        <div class="sel-bandhead">${esc(b.tier)}
          <span class="dim">${b.count} rule${b.count === 1 ? '' : 's'}</span></div>
        ${groups}</div>`;
    }).join('');
  }

  const render = () => header() + `<div class="tr-scroll">${bands()}</div>`;

  return { open, close, render, state, get isOpen() { return state.open; } };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = SELRULES;
if (typeof window !== 'undefined') window.SELRULES = SELRULES;
