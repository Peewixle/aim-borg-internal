/* ==========================================================================
   pds_trace.js — BG-56, the trace UI.

   Its own module, like the studio. The browser inspects a profile's rules; the
   trace reads a profile ACROSS destinations. Different question, different
   shape, and the browser file is already large.

   A ROW IS (tab, field). NOTHING MERGES.
   ======================================
   `Signature Provided` on Payers and `Signature Provided (12)` on HCFA 1500
   are two destinations. 22 profiles set the first and not the second, so a
   merged row would show them writing a field they do not write. `Emergency`
   needs no special case under this rule, which is the point.

   The server computes the rows. This renders them and posts reviewer entries;
   it derives nothing, for the same reason the studio derives nothing.
   ========================================================================== */
'use strict';

const TRACE = (() => {

  const state = { profile: null, snapshot: null, data: null, rules: null,
                  sources: null, surface: 'grid', open: false, onClose: null };

  const esc = s => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  async function get(path) {
    const r = await fetch(path);
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }
  async function post(path, body) {
    const r = await fetch(path, { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
    return d;
  }

  /* ------------------------------------------------------------- loading */

  async function open(snapshot, profileGuid, onClose) {
    state.snapshot = snapshot;
    state.profile = profileGuid;
    state.onClose = onClose || null;
    state.surface = 'grid';           // BG-56: the trace opens on the Trace Grid
    state.open = true;
    state.data = await get(`/api/trace?snapshot=${snapshot}&profile=${encodeURIComponent(profileGuid)}`);
    return state.data;
  }

  function close() {
    state.open = false;
    if (state.onClose) state.onClose();
  }

  /** BG-56: the tab strip switches surface on a single click, without closing
   *  the trace and without changing the selected profile. Loaded on demand,
   *  because Rule Detail and the Source Map are larger than the grid and most
   *  visits never open them. */
  async function show(surface) {
    state.surface = surface;
    const s = state.snapshot, p = encodeURIComponent(state.profile);
    if (surface === 'rules' && !state.rules)
      state.rules = await get(`/api/trace/rules?snapshot=${s}&profile=${p}`);
    if (surface === 'sources' && !state.sources)
      state.sources = await get(`/api/trace/sources?snapshot=${s}&profile=${p}`);
    return state.surface;
  }

  async function review(tab, field, entry) {
    await post('/api/trace/review', Object.assign(
      { snapshot: state.snapshot, profile: state.profile, tab, field }, entry));
    // Update in place rather than refetching: a full rebuild would lose the
    // reviewer's scroll position halfway down eighty rows.
    const row = state.data.rows.find(r => r.tab === tab && r.field === field);
    if (row) Object.assign(row, entry);
  }

  /* ----------------------------------------------------------- rendering */

  const BANDS = ['Sys', 'Org', 'Cust', 'Agy'];

  function header() {
    const d = state.data;
    if (!d) return '';
    // BG-56: every surface names the profile, and the view states the snapshot
    // and whether it is design or production. A trace of a proposal read as a
    // trace of production would be the worst possible misreading.
    return `<div class="tr-head">
      <h2>${esc(d.profile)}</h2>
      <span class="tr-meta">${esc(d.entity || '')} · ${esc(d.tier || '')}
        · ${d.row_count} destination fields</span>
      <span class="tr-prov ${d.kind === 'design' ? 'tr-design' : ''}">${
        esc(d.source)} — ${esc(d.kind)}, snapshot ${d.snapshot}</span>
    </div>
    <div class="tr-tabs">
      ${[['grid','Trace Grid'],['rules','Rule Detail'],
         ['sources','ePCR Source Map'],['assume','Assumptions']]
        .map(([k,l]) => `<button class="tr-tab ${state.surface===k?'on':''}"
             data-surface="${k}">${l}</button>`).join('')}
      <button class="tr-close" id="trClose">Close &nbsp;esc</button>
    </div>`;
  }

  function grid() {
    const d = state.data;
    if (!d) return '';
    const head = `<tr>
      <th class="tr-src">ePCR source</th>
      <th>Tab</th><th>Destination field</th>
      ${BANDS.map(b => `<th class="tr-band tr-def">D·${b}</th>`).join('')}
      ${BANDS.map(b => `<th class="tr-band tr-spec">S·${b}</th>`).join('')}
      ${d.outputs.map(o => `<th class="tr-out">${esc(o)}</th>`).join('')}
      <th class="tr-rev">Verdict</th><th class="tr-rev">Disposition</th>
      <th class="tr-rev">Note</th></tr>`;

    const rows = d.rows.map(r => {
      const src = (r.sources.length || r.copied_from.length)
        ? (r.sources.map(s => `<span class="tr-el tr-unmapped"
             title="no NEMSIS element mapping">${esc(s)}</span>`).join(' ') +
           r.copied_from.map(s => `<span class="tr-el tr-copy"
             title="value copied from this ePCR field">${esc(s)}</span>`).join(' '))
        : '<span class="dim">—</span>';

      // Eight cells, always. An empty band is information: it says no rule at
      // that level touches this field.
      const bands = r.bands.map(b => {
        if (!b.count) return `<td class="tr-band tr-empty"></td>`;
        const cell = b.rules.map(x =>
          `<span class="tr-rule" data-cond="${esc(x.condition)}"
             title="priority ${esc(x.priority)} · ${esc(x.entity)}">${
             esc(x.value || x.outcome_type)}</span>`).join('<br>');
        return `<td class="tr-band tr-${b.set}">${cell}</td>`;
      }).join('');

      const outs = r.outputs.map(o => o.lands
        ? `<td class="tr-out ${o.confirmed ? '' : 'tr-unconf'}"
             title="${o.confirmed ? 'confirmed' : 'resolution unconfirmed'}">${
             esc(o.lands)}</td>`
        : `<td class="tr-out tr-empty"></td>`).join('');

      const k = `${esc(r.tab)}|${esc(r.field)}`;
      return `<tr data-tab="${esc(r.tab)}" data-field="${esc(r.field)}">
        <td class="tr-src">${src}</td>
        <td class="tr-tabname">${esc(r.tab)}</td>
        <td class="tr-field">${esc(r.field)}</td>
        ${bands}${outs}
        <td class="tr-rev"><select class="tr-verdict" data-k="${k}">
          ${['', 'pass', 'fail', 'query'].map(v =>
            `<option value="${v}" ${r.verdict===v?'selected':''}>${v||'—'}</option>`).join('')}
        </select></td>
        <td class="tr-rev"><select class="tr-disp" data-k="${k}">
          ${['', 'no action', 'fix rule', 'raise issue'].map(v =>
            `<option value="${v}" ${r.disposition===v?'selected':''}>${v||'—'}</option>`).join('')}
        </select></td>
        <td class="tr-rev"><input class="tr-note" data-k="${k}"
          value="${esc(r.note || '')}" placeholder="note"></td>
      </tr>`;
    }).join('');

    return `<div class="tr-scroll"><table class="tr-grid">
      <thead>${head}</thead><tbody>${rows}</tbody></table></div>
      <div class="tr-legend">
        <span class="tr-el tr-unmapped">source label</span> no NEMSIS element mapping yet ·
        <span class="tr-el tr-copy">copied</span> value taken from an ePCR field ·
        <span class="tr-out tr-unconf">output</span> resolution unconfirmed ·
        D = Default Profile, S = Specific Profile, in firing order
      </div>`;
  }

  function rules() {
    const d = state.rules;
    if (!d) return '<div class="tr-empty-note">Loading…</div>';
    return `<div class="tr-scroll"><table class="tr-grid">
      <thead><tr><th>Tab</th><th>Field</th><th>Set</th><th>Level</th>
        <th>Entity</th><th>Pri</th><th>Outcome</th><th>Value</th>
        <th>Condition</th><th>Description</th></tr></thead>
      <tbody>${d.rules.map(r => `<tr>
        <td>${esc(r.tab)}</td><td class="tr-field">${esc(r.field)}</td>
        <td>${esc(r.rule_set)}</td><td>${esc(r.tier)}</td>
        <td>${esc(r.entity)}</td><td class="num">${esc(r.priority)}</td>
        <td>${esc(r.outcome_type)}</td><td>${esc(r.value)}</td>
        <td><span class="tr-rule" data-cond="${esc(r.condition)}">${
          esc((r.condition || '').slice(0, 60))}${(r.condition||'').length>60?'…':''}</span></td>
        <td class="dim">${esc(r.description)}</td></tr>`).join('')}
      </tbody></table></div>
      <div class="tr-legend">${d.count} rules, sorted by tab, field, band, priority.</div>`;
  }

  function sources() {
    const d = state.sources;
    if (!d) return '<div class="tr-empty-note">Loading…</div>';
    return `<div class="tr-note-box">
      Source fields are shown as the labels the conditions carry. No
      label-to-element lookup exists, so <b>no mapping is verified</b> against
      ${esc(d.nemsis_version)}.
    </div>
    <div class="tr-scroll"><table class="tr-grid">
      <thead><tr><th>Source field</th><th>NEMSIS element</th><th>Mapping</th>
        <th>Read as</th><th>Feeds</th></tr></thead>
      <tbody>${d.sources.map(s => `<tr>
        <td>${esc(s.source)}</td>
        <td class="dim">${esc(s.element || '—')}</td>
        <td><span class="tr-el tr-unmapped">${esc(s.mapping)}</span></td>
        <td>${s.as_condition ? 'condition' : ''}${
              s.as_condition && s.as_value ? ' + ' : ''}${s.as_value ? 'value' : ''}</td>
        <td class="dim">${esc(s.feeds.join(', '))}</td></tr>`).join('')}
      </tbody></table></div>`;
  }

  function assumptions() {
    const a = (state.data && state.data.assumptions) || [];
    return `<div class="tr-note-box">
      Everything inferred in building this trace, so an inference is never
      mistaken for something the product confirmed.
    </div>
    <div class="tr-scroll"><table class="tr-grid">
      <thead><tr><th>Subject</th><th>Assumed</th><th>Basis</th>
        <th>Status</th><th>Owner</th></tr></thead>
      <tbody>${a.map(x => `<tr>
        <td><b>${esc(x.subject)}</b></td>
        <td>${esc(x.assumed)}</td>
        <td class="dim">${esc(x.basis)}</td>
        <td><span class="tr-status ${x.status === 'settled' ? 'tr-settled' : 'tr-open'}">${
          esc(x.status)}</span></td>
        <td>${esc(x.owner || '—')}</td></tr>`).join('')}
      </tbody></table></div>`;
  }

  function render() {
    const body = state.surface === 'grid'    ? grid()
               : state.surface === 'rules'   ? rules()
               : state.surface === 'sources' ? sources()
               :                               assumptions();
    return header() + body;
  }

  /** BG-56: a condition opens in a dialog, with each bracketed value list
   *  rendered one term per line rather than inline. A twelve-value list on one
   *  line is where a reviewer stops reading. */
  function conditionHtml(cond) {
    const text = String(cond || '(always applies)');
    const parts = text.replace(/\[([^\]]+)\]/g, (m, inner) =>
      '\n' + inner.split(',').map(t => '    • ' + t.trim()).join('\n'));
    return `<div class="tr-dlg-inner"><pre>${esc(parts)}</pre></div>`;
  }

  return { open, close, show, render, review, conditionHtml, state,
           get isOpen() { return state.open; } };
})();

if (typeof module !== 'undefined' && module.exports) module.exports = TRACE;
if (typeof window !== 'undefined') window.TRACE = TRACE;
