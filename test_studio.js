#!/usr/bin/env node
/**
 * Tests for pds_studio.js.
 *
 *     node test_studio.js
 *
 * Runs against a fake fetch rather than a live server: the server is already
 * covered by 68 checks, and what needs proving here is the client's own
 * behaviour — debouncing, what happens to a queued change when a save fails,
 * and that demo mode is decided by the payload rather than by whether a
 * request worked.
 */
const assert = require('assert');

let pass = 0; const fail = [];
const chk = (c, m) => { if (c) { pass++; console.log('  ok   ' + m); }
                        else { fail.push(m); console.log('  FAIL ' + m); } };
const section = t => console.log(`\n--- ${t} ---`);
const sleep = ms => new Promise(r => setTimeout(r, ms));

// --- a fetch that records calls and can be made to fail -------------------
let calls = [], failNext = null, reply = {};
global.fetch = async (path, opts = {}) => {
  calls.push({ path, body: opts.body ? JSON.parse(opts.body) : null });
  if (failNext) { const e = failNext; failNext = null;
                  return { ok: false, status: 500, json: async () => ({ error: e }) }; }
  const key = Object.keys(reply).find(k => path.startsWith(k));
  return { ok: true, status: 200, json: async () => (reply[key] || {}) };
};
global.window = { addEventListener() {} };

function load() {
  delete require.cache[require.resolve('./pds_studio.js')];
  calls = [];
  return require('./pds_studio.js');
}

(async () => {

section('demo mode is decided by the payload, not by feature detection');
let P = load();
P.init({ studio: { mode: 'demo', design: null, base: null } });
chk(P.state.enabled === true, 'the studio is present in demo — visible, not hidden');
chk(P.state.writable === false, 'demo is not writable');
let g = P.can('promote');
chk(g.ok === false && /read-only/.test(g.why), `demo refuses authoring: "${g.why}"`);
P.promote('R-1', 'System');
chk(P.state.pending.length === 0, 'a demo action queues nothing');
chk(calls.length === 0, 'a demo action sends nothing to the server');
chk(P.status().kind === 'demo', 'status reports demo');
// The demo gate is not a second barrier — writable is already false in demo,
// so removing it refuses the action anyway. What it provides is the right
// REASON: the mode, not a lock somebody else holds. That is what is tested.
chk(!/editing this design|read-only$/.test(g.why),
    'demo gives the demo reason, not a single-writer reason');
chk(/demo/i.test(g.why), 'and names demo explicitly');
chk(/not saved/i.test(P.status().text), 'and says changes are not saved');

section('a failed request must not be mistaken for demo');
P = load();
P.init({ studio: { mode: 'live', design: 4, writable: true, base: 2 } });
failNext = 'server exploded';
P.promote('R-1', 'System');
await P.flush();
chk(P.state.mode === 'live', 'a failed save leaves the mode live');
chk(P.can('promote').ok === true, 'and authoring stays available');
chk(P.status().kind === 'error', 'status reports the error');
chk(P.state.pending.length === 1, 'THE CHANGE STAYS QUEUED — a failed save loses nothing');
chk(!/saved/i.test(P.status().text) || /not saved/i.test(P.status().text),
    'the indicator never reports saved while a write has failed');

section('autosave collapses a burst into one save');
P = load();
P.init({ studio: { mode: 'live', design: 4, writable: true, base: 2 } });
reply = { '/api/design/apply': { applied: 3, summary: { counts: { promote: 3 } } } };
P.promote('R-1', 'System'); P.promote('R-2', 'System'); P.promote('R-3', 'System');
chk(P.state.pending.length === 3, 'three changes queue');
chk(calls.length === 0, 'nothing is sent while the burst is still arriving');
chk(P.status().kind === 'unsaved', 'status reports unsaved during the window');
await sleep(P.AUTOSAVE_MS + 300);
chk(calls.length === 1, `the burst is written as ONE save (got ${calls.length})`);
chk(calls[0].body.actions.length === 3, 'and carries all three actions');
chk(P.state.pending.length === 0, 'the queue is cleared once written');
chk(P.status().kind === 'saved', 'status reports saved afterwards');

section('a change made during a save is not lost');
P = load();
P.init({ studio: { mode: 'live', design: 4, writable: true, base: 2 } });
let release; 
global.fetch = async (path, opts) => {
  calls.push({ path, body: opts.body ? JSON.parse(opts.body) : null });
  await new Promise(r => { release = r; });
  return { ok: true, status: 200, json: async () => ({ applied: 1, summary: {} }) };
};
P.promote('R-1', 'System');
const inflight = P.flush();
await sleep(20);
P.promote('R-2', 'Organization');       // arrives mid-request
release();
await inflight;
chk(P.state.pending.length === 1 && P.state.pending[0].rule === 'R-2',
    'a change queued during a save survives it');

section('single-writer is surfaced, not silently ignored');
P = load();
P.init({ studio: { mode: 'live', design: 4, writable: false, heldBy: 'mcorey', base: 2 } });
g = P.can('promote');
chk(g.ok === false && /mcorey/.test(g.why), `refused and names the holder: "${g.why}"`);
chk(P.status().kind === 'readonly', 'status reports read-only');
chk(/mcorey/.test(P.status().text), 'and names who is editing');

section('complete forces a write first');
P = load();
P.init({ studio: { mode: 'live', design: 4, writable: true, base: 2 } });
global.fetch = async (path, opts) => {
  calls.push({ path, body: opts.body ? JSON.parse(opts.body) : null });
  return { ok: true, status: 200, json: async () => ({ applied: 1, summary: {} }) };
};
P.promote('R-9', 'System');
await P.complete();
chk(calls.some(c => c.path === '/api/design/apply'),
    'a pending change is written before the design is marked complete');
chk(calls[calls.length - 1].path === '/api/design/complete', 'complete is sent after it');
chk(P.state.writable === false, 'the design becomes read-only once complete');

section('checks and change set never show stale state');
P = load();
P.init({ studio: { mode: 'live', design: 4, writable: true, base: 2 } });
reply = { '/api/design/apply': { applied: 1, summary: {} },
          '/api/design/changeset': { changes: [], summary: { counts: {} } },
          '/api/design/checks': { total: 0, counts: {}, findings: [], design: 4, base: 2 } };
global.fetch = async (path, opts = {}) => {
  calls.push({ path, body: opts.body ? JSON.parse(opts.body) : null });
  const key = Object.keys(reply).find(k => path.startsWith(k));
  return { ok: true, status: 200, json: async () => (reply[key] || {}) };
};
P.promote('R-5', 'System');
await P.changeSet();
chk(calls[0].path === '/api/design/apply',
    'the queue is flushed before the change set is fetched');
calls = [];
P.promote('R-6', 'System');
await P.runChecks();
chk(calls[0].path === '/api/design/apply',
    'the queue is flushed before checks run — they check what is stored');

section('rendering');
P = load();
P.init({ studio: { mode: 'live', design: 4, writable: true, base: 2 } });
let html = P.ruleControls('R-1', 'Customer');
chk(/pds-up[^>]*data-tier="System"/.test(html), 'a Customer rule offers promotion to System');
chk(/pds-down[^>]*data-tier="Agency"/.test(html), 'and demotion to Agency');
chk(!/data-tier="Customer"/.test(html), 'but not a move to its own level');
chk(!/disabled/.test(html), 'controls are enabled when writable');

P = load();
P.init({ studio: { mode: 'demo' } });
html = P.ruleControls('R-1', 'Customer');
chk(/disabled/.test(html), 'controls are present but disabled in demo');
chk(/title="[^"]*read-only/.test(html), 'and say why on hover');

P = load();
P.init({ studio: { mode: 'live', design: 4, writable: true, base: 2 } });
html = P.changeSetHtml({ changes: [
  { action: 'promote', profile: 'DIALYSIS', tab: 'Charges', field: 'Baserate Rate Code',
    from_tier: 'Customer', to_tier: 'System' }], summary: { counts: { promote: 1 } } });
chk(/Baserate Rate Code/.test(html) && /Charges tab/.test(html),
    'the change set names the destination as tab AND field');
chk(/Customer/.test(html) && /System/.test(html), 'and shows the level before and after');

html = P.findingsHtml({ design: 4, base: 2, counts: { High: 1 }, ranAt: '2026-09-05 12:00',
  findings: [{ severity: 'High', check: 'C4 ... (ADO 7643)', entity: 'Azalea',
               subject: 'Default', text: 'MA hides MAR',
               attributed: { save: 7, by: 'mmcintyre', at: 'now' } }] });
chk(/base snapshot 2/.test(html), 'the findings surface names the base dump');
chk(/ADO 7643/.test(html), 'each finding names the story that defines its check');
chk(/save 7 by mmcintyre/.test(html), 'and the save it is attributed to');
chk(/checked 2026-09-05/.test(html), 'and when the checks last ran');

console.log(`\n${pass} passed, ${fail.length} failed`);
process.exit(fail.length ? 1 : 0);
})();
