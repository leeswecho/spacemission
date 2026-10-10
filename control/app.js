// Control page for nbody_sim.  Sends JSON commands to /api/command, which the
// control server forwards to the simulation over ZeroMQ.

const G0 = 9.80665;
const POLL_MS = 1000;
const RUN_TICK_MS = 100;
const MAX_LOG = 200;
const J2000_UTC_MS = Date.UTC(2000, 0, 1, 11, 58, 55, 816); // 2000-01-01 12:00 TT

const $ = (id) => document.getElementById(id);
const ui = {
  status: $('status'), time: $('sim-time'), date: $('sim-date'), steps: $('step-count'),
  viewerLink: $('viewer-link'),
  stepDt: $('step-dt'), stepBtn: $('step-btn'), presets: $('step-presets'),
  warp: $('warp'), maxStep: $('max-step'), runBtn: $('run-btn'), pauseOnEvent: $('pause-on-event'),
  runStats: $('run-stats'), resetBtn: $('reset-btn'),
  craftSelect: $('craft-select'), telemetry: $('telemetry'),
  thrustTarget: $('thrust-target'), stageTarget: $('stage-target'),
  hThrust: $('h-thrust'), hPitch: $('h-pitch'), hHeading: $('h-heading'), hBody: $('h-body'),
  hFlowMode: $('h-flow-mode'), hIsp: $('h-isp'), hRate: $('h-rate'),
  hIspField: $('h-isp-field'), hRateField: $('h-rate-field'), hPreview: $('h-preview'), hApply: $('h-apply'),
  rFrame: $('r-frame'), rBody: $('r-body'), rBodyField: $('r-body-field'),
  rF: [$('r-f0'), $('r-f1'), $('r-f2')], rL: [$('r-l0'), $('r-l1'), $('r-l2')], rRate: $('r-rate'), rApply: $('r-apply'),
  sFrame: $('s-frame'), sBody: $('s-body'), sBodyField: $('s-body-field'),
  sChildren: $('s-children'), sAdd: $('s-add'), sMassCheck: $('s-mass-check'), sApply: $('s-apply'),
  consoleText: $('console-text'), consoleSend: $('console-send'), consoleExamples: $('console-examples'),
  log: $('log'),
};

let bodyIds = [];
let state = null;        // latest get_state / step state
let selectedId = null;
let running = false;
let viewerBase = null;

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

const norm = (v) => Math.hypot(v[0], v[1], v[2]);
const fmtNum = (x, digits = 2) => x.toLocaleString(undefined, { maximumFractionDigits: digits });
const fmtSci = (x, unit) => `${Math.abs(x) >= 1e7 || (Math.abs(x) < 1e-2 && x !== 0) ? x.toExponential(3) : fmtNum(x)} ${unit}`;

function fmtLen(m) {
  const a = Math.abs(m);
  if (a < 1e4) return `${m.toFixed(a < 10 ? 2 : 1)} m`;
  if (a < 1e10) return `${fmtNum(m / 1e3, a < 1e7 ? 2 : 0)} km`;
  return `${(m / 1.495978707e11).toFixed(4)} AU`;
}
const fmtSpeed = (v) => (Math.abs(v) < 1e4 ? `${v.toFixed(2)} m/s` : `${(v / 1e3).toFixed(3)} km/s`);

function fmtDuration(t) {
  const sign = t < 0 ? '-' : '';
  t = Math.abs(t);
  const d = Math.floor(t / 86400);
  const h = Math.floor((t % 86400) / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  return `${sign}${d ? `${d}d ` : ''}${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${s.toFixed(2).padStart(5, '0')}`;
}

function setStatus(kind, text) {
  ui.status.className = `status status-${kind}`;
  ui.status.textContent = text;
}

// ---------------------------------------------------------------------------
// Command transport and log
// ---------------------------------------------------------------------------

async function post(req) {
  try {
    const res = await fetch('/api/command', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    });
    return await res.json();
  } catch (err) {
    return { ok: false, error: `control server unreachable (${err.message})`, transport: true };
  }
}

// Send a command; log it unless {log: false}.  Updates connection status and,
// when the reply carries state, the telemetry.
async function send(req, { log = true } = {}) {
  const entry = log ? logRequest(req) : null;
  const reply = await post(req);
  if (entry) logReply(entry, reply);
  if (reply.ok) {
    setStatus(running ? 'run' : 'ok', running ? 'Running' : 'Live');
    if (reply.time !== undefined) updateClock(reply.time, reply.step_count);
    if (reply.state) applyState(reply.state);
    if (reply.events?.length) logEvents(reply.events);
  } else if (reply.transport) {
    setStatus('bad', 'Control server unreachable');
  } else if (reply.timeout) {
    setStatus('bad', 'Simulation not responding');
  }
  return reply;
}

function timestamp() {
  return new Date().toLocaleTimeString(undefined, { hour12: false });
}

function addLogItem(li) {
  ui.log.prepend(li);
  while (ui.log.children.length > MAX_LOG) ui.log.lastChild.remove();
}

function logRequest(req) {
  const li = document.createElement('li');
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = timestamp();
  const r = document.createElement('span');
  r.className = 'req';
  r.textContent = JSON.stringify(req);
  li.append(t, r);
  addLogItem(li);
  return li;
}

function summarise(reply) {
  if (!reply.ok) return `error: ${reply.error}`;
  const parts = [`ok · t = ${fmtDuration(reply.time ?? 0)}`];
  if (reply.wall_time !== undefined) parts.push(`${(reply.wall_time * 1000).toFixed(0)} ms`);
  if (reply.created) parts.push(`created ${reply.created.join(', ')}`);
  if (reply.events?.length) parts.push(`${reply.events.length} event(s)`);
  return parts.join(' · ');
}

function logReply(li, reply) {
  const s = document.createElement('div');
  s.className = reply.ok ? 'ok' : 'err';
  s.textContent = `→ ${summarise(reply)}`;
  const details = document.createElement('details');
  const summary = document.createElement('summary');
  summary.textContent = 'reply';
  const pre = document.createElement('pre');
  const shown = { ...reply };
  if (shown.state) shown.state = '(omitted)';
  pre.textContent = JSON.stringify(shown, null, 2);
  details.append(summary, pre);
  li.append(s, details);
}

function logNote(text, cls = 'event') {
  const li = document.createElement('li');
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = timestamp();
  const n = document.createElement('span');
  n.className = cls;
  n.textContent = text;
  li.append(t, n);
  addLogItem(li);
}

function logEvents(events) {
  for (const e of events) {
    const extra = e.body ? ` (${e.body})` : e.reason ? ` (${e.reason})` : '';
    logNote(`event at t = ${fmtDuration(e.time)}: ${e.spacecraft} ${e.event}${extra}`);
  }
}

// ---------------------------------------------------------------------------
// State and telemetry
// ---------------------------------------------------------------------------

function updateClock(t, stepCount) {
  ui.time.textContent = `t = ${fmtDuration(t)}`;
  ui.date.textContent = `${new Date(J2000_UTC_MS + t * 1000).toISOString().replace('T', ' ').slice(0, 19)} UTC`;
  if (stepCount !== undefined) ui.steps.textContent = `${stepCount} steps`;
}

async function loadInfo() {
  const info = await send({ cmd: 'info' }, { log: false });
  if (!info.ok) return false;
  bodyIds = info.bodies;
  for (const sel of document.querySelectorAll('.body-select')) {
    const keep = sel.value;
    sel.replaceChildren(...bodyIds.map((id) => new Option(id, id)));
    if (bodyIds.includes(keep)) sel.value = keep;
  }
  updateClock(info.time, info.step_count);
  return true;
}

async function refresh() {
  const reply = await send({ cmd: 'get_state', bodies: false }, { log: false });
  if (reply.ok) applyState(reply);
  return reply;
}

function crafts() {
  return state?.spacecraft ?? [];
}

function selectedCraft() {
  return crafts().find((s) => s.id === selectedId) ?? null;
}

function applyState(s) {
  state = s;
  updateClock(s.time, s.step_count);
  const ids = crafts().map((c) => c.id);
  const current = [...ui.craftSelect.options].map((o) => o.value);
  if (ids.join('\n') !== current.join('\n')) {
    ui.craftSelect.replaceChildren(...ids.map((id) => new Option(id, id)));
    if (!ids.length) ui.craftSelect.append(new Option('(no spacecraft)', ''));
  }
  if (!ids.includes(selectedId)) selectCraft(ids[0] ?? null);
  else ui.craftSelect.value = selectedId;
  renderTelemetry();
}

function selectCraft(id) {
  const changed = id !== selectedId;
  selectedId = id;
  ui.craftSelect.value = id ?? '';
  const c = selectedCraft();
  ui.thrustTarget.textContent = id ? `→ ${id}` : '';
  ui.stageTarget.textContent = id ? `→ ${id}` : '';
  for (const b of [ui.hApply, ui.rApply, ui.sApply, ...document.querySelectorAll('.cut-btn')]) b.disabled = !id;
  if (changed && c) {
    // Default local frames to the craft's nearest body.
    for (const sel of [ui.hBody, ui.rBody, ui.sBody]) sel.value = c.nearest_body.id;
    resetStageRows();
  }
  updateViewerLink();
  renderTelemetry();
  updatePreview();
}

function renderTelemetry() {
  const c = selectedCraft();
  if (!c) {
    ui.telemetry.replaceChildren();
    return;
  }
  const nb = c.nearest_body;
  const enu = nb.surface_relative_velocity_enu;
  const thrustMag = norm(c.thrust.force);
  const burnLeft = c.thrust.mass_rate < 0 ? (c.mass - c.dry_mass) / -c.thrust.mass_rate : null;
  const rows = [
    ['Mass', fmtSci(c.mass, 'kg')],
    ['Dry mass', fmtSci(c.dry_mass, 'kg')],
    ['Nearest body', nb.id],
    ['Altitude', fmtLen(nb.altitude)],
    ['Lat / Lon', `${nb.latitude.toFixed(4)}°, ${nb.longitude.toFixed(4)}°`],
    ['Surface-relative speed', fmtSpeed(nb.airspeed)],
    ['Vertical speed', fmtSpeed(enu[2])],
    ['Horizontal speed', fmtSpeed(Math.hypot(enu[0], enu[1]))],
    ['In contact', nb.in_contact ? 'yes' : 'no'],
    ['Thrust', c.thrust.active
      ? `${fmtSci(thrustMag, 'N')} ${c.thrust.frame}${c.thrust.body ? ` (${c.thrust.body})` : ''}` : 'off', c.thrust.active],
    ['Mass rate', `${c.thrust.mass_rate.toFixed(2)} kg/s`, c.thrust.active],
    ['Burn time left', burnLeft !== null ? fmtDuration(burnLeft) : '—'],
    ['Acceleration', `${(norm(c.acceleration.total) / G0).toFixed(3)} g`],
    ['Drag decel.', `${(norm(c.acceleration.drag) / G0).toFixed(3)} g`],
    ['Dynamic pressure', fmtSci(nb.dynamic_pressure, 'Pa')],
    ['Air density', fmtSci(nb.density, 'kg/m³')],
  ];
  if (nb.drag_coefficient !== null) rows.push(['Cd', nb.drag_coefficient.toFixed(3)]);
  ui.telemetry.replaceChildren(...rows.flatMap(([k, v, on]) => {
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = v;
    if (on) dd.className = 'on';
    return [dt, dd];
  }));
}

function updateViewerLink() {
  if (!viewerBase) return;
  const url = new URL(viewerBase);
  if (selectedId) url.searchParams.set('focus', selectedId);
  ui.viewerLink.href = url.toString();
}

ui.craftSelect.addEventListener('change', () => selectCraft(ui.craftSelect.value || null));

// ---------------------------------------------------------------------------
// Time control
// ---------------------------------------------------------------------------

async function stepOnce(dt) {
  if (!(dt > 0)) {
    logNote('step size must be > 0', 'err');
    return;
  }
  await send({ cmd: 'step', dt, return_state: true });
}

ui.stepBtn.addEventListener('click', () => stepOnce(Number(ui.stepDt.value)));
ui.presets.addEventListener('click', (e) => {
  const dt = e.target.closest('button')?.dataset.dt;
  if (!dt) return;
  ui.stepDt.value = dt;
  stepOnce(Number(dt));
});

function setRunning(on, reason) {
  running = on;
  ui.runBtn.textContent = on ? '❚❚ Pause' : '▶ Run';
  ui.runBtn.classList.toggle('running', on);
  ui.stepBtn.disabled = on;
  for (const b of ui.presets.querySelectorAll('button')) b.disabled = on;
  ui.resetBtn.disabled = on;
  if (on) {
    setStatus('run', 'Running');
    logNote(`run started (×${fmtNum(Number(ui.warp.value), 0)}, max step ${ui.maxStep.value} s)`, 'req');
    runLoop();
  } else {
    ui.runStats.textContent = 'Paused';
    if (reason) logNote(`run paused: ${reason}`, 'req');
  }
}

async function runLoop() {
  let last = performance.now();
  const startWall = last;
  const startSim = state?.time ?? 0;
  let steps = 0;
  while (running) {
    const tickStart = performance.now();
    const wall = Math.max((tickStart - last) / 1000, RUN_TICK_MS / 1000);
    last = tickStart;
    const warp = Number(ui.warp.value);
    const maxStep = Math.max(Number(ui.maxStep.value) || 1, 1e-3);
    const dt = Math.min(warp * wall, maxStep);
    const reply = await send({ cmd: 'step', dt, return_state: true }, { log: false });
    if (!running) break;
    if (!reply.ok) {
      logNote(`step failed: ${reply.error}`, 'err');
      setRunning(false, 'error');
      break;
    }
    steps++;
    const elapsed = (performance.now() - startWall) / 1000;
    const achieved = elapsed > 0 ? (reply.time - startSim) / elapsed : 0;
    ui.runStats.textContent = `Running · achieved ×${fmtNum(achieved, 1)} · last dt ${dt.toFixed(3)} s · `
      + `${(reply.wall_time * 1000).toFixed(0)} ms/step · ${steps} steps`;
    if (reply.events?.length && ui.pauseOnEvent.checked) {
      setRunning(false, `event (${reply.events.map((e) => e.event).join(', ')})`);
      break;
    }
    const wait = RUN_TICK_MS - (performance.now() - tickStart);
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
  }
}

ui.runBtn.addEventListener('click', () => setRunning(!running, running ? 'by user' : undefined));

ui.resetBtn.addEventListener('click', async () => {
  if (!confirm('Reset the simulation to its initial state? All progress will be lost.')) return;
  const reply = await send({ cmd: 'reset' });
  if (reply.ok) {
    selectedId = null;
    await loadInfo();
    await refresh();
  }
});

// ---------------------------------------------------------------------------
// Thrust
// ---------------------------------------------------------------------------

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => {
    for (const t of document.querySelectorAll('.tab')) {
      const on = t === tab;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', String(on));
      $(`tab-${t.dataset.tab}`).classList.toggle('hidden', !on);
    }
  });
}

function helperCommand() {
  const thrustN = Number(ui.hThrust.value) * 1e3;
  const pitch = (Number(ui.hPitch.value) * Math.PI) / 180;
  const heading = (Number(ui.hHeading.value) * Math.PI) / 180;
  // Round to mN so sin/cos noise (e.g. cos 90° = 6e-17) doesn't clutter commands.
  const force = [
    thrustN * Math.cos(pitch) * Math.sin(heading),
    thrustN * Math.cos(pitch) * Math.cos(heading),
    thrustN * Math.sin(pitch),
  ].map((x) => Math.round(x * 1e3) / 1e3 || 0);
  let massRate;
  if (ui.hFlowMode.value === 'isp') {
    const isp = Number(ui.hIsp.value);
    massRate = isp > 0 ? -thrustN / (isp * G0) : 0;
  } else {
    massRate = Number(ui.hRate.value);
  }
  return { cmd: 'set_thrust', id: selectedId, force, mass_rate: massRate, frame: 'enu', body: ui.hBody.value };
}

function updatePreview() {
  const cmd = helperCommand();
  if (![...cmd.force, cmd.mass_rate].every(Number.isFinite)) {
    ui.hPreview.textContent = 'Enter numeric values.';
    return;
  }
  const c = selectedCraft();
  let extra = '';
  if (c && cmd.mass_rate < 0) extra = ` · burn to dry ≈ ${fmtDuration((c.mass - c.dry_mass) / -cmd.mass_rate)}`;
  if (c) extra += ` · T/W ≈ ${(norm(cmd.force) / (c.mass * G0)).toFixed(2)} (1 g)`;
  ui.hPreview.textContent = `ENU force = [${cmd.force.map((x) => fmtNum(x, 0)).join(', ')}] N · `
    + `dm/dt = ${cmd.mass_rate.toFixed(2)} kg/s${extra}`;
}

for (const el of [ui.hThrust, ui.hPitch, ui.hHeading, ui.hIsp, ui.hRate, ui.hBody]) {
  el.addEventListener('input', updatePreview);
}
ui.hFlowMode.addEventListener('change', () => {
  const isp = ui.hFlowMode.value === 'isp';
  ui.hIspField.classList.toggle('hidden', !isp);
  ui.hRateField.classList.toggle('hidden', isp);
  updatePreview();
});

ui.hApply.addEventListener('click', async () => {
  const cmd = helperCommand();
  if (![...cmd.force, cmd.mass_rate].every(Number.isFinite)) return;
  await send(cmd);
  await refresh();
});

function updateRawFrame() {
  const enu = ui.rFrame.value === 'enu';
  ui.rBodyField.classList.toggle('hidden', !enu);
  const names = enu ? ['F east', 'F north', 'F up'] : ['F x', 'F y', 'F z'];
  ui.rL.forEach((l, i) => { l.textContent = names[i]; });
}
ui.rFrame.addEventListener('change', updateRawFrame);

ui.rApply.addEventListener('click', async () => {
  const force = ui.rF.map((i) => Number(i.value));
  const massRate = Number(ui.rRate.value);
  if (![...force, massRate].every(Number.isFinite)) {
    logNote('thrust values must be numbers', 'err');
    return;
  }
  const cmd = { cmd: 'set_thrust', id: selectedId, force, mass_rate: massRate, frame: ui.rFrame.value };
  if (ui.rFrame.value === 'enu') cmd.body = ui.rBody.value;
  await send(cmd);
  await refresh();
});

for (const b of document.querySelectorAll('.cut-btn')) {
  b.addEventListener('click', async () => {
    await send({ cmd: 'set_thrust', id: selectedId, force: [0, 0, 0], mass_rate: 0 });
    await refresh();
  });
}

// ---------------------------------------------------------------------------
// Staging
// ---------------------------------------------------------------------------

function numberInput(value = '', placeholder = '') {
  const i = document.createElement('input');
  i.type = 'number';
  i.step = 'any';
  i.value = value;
  i.placeholder = placeholder;
  i.addEventListener('input', updateMassCheck);
  return i;
}

function labelled(text, control) {
  const label = document.createElement('label');
  label.className = 'field';
  const span = document.createElement('span');
  span.textContent = text;
  label.append(span, control);
  return { label, span };
}

function addStageRow(id = '', dv = [0, 0, 0]) {
  const row = document.createElement('div');
  row.className = 'child';
  const idIn = document.createElement('input');
  idIn.className = 'id';
  idIn.value = id;
  idIn.placeholder = 'id';
  const mass = numberInput('', 'kg');
  const dry = numberInput('', '0');
  const dvWrap = document.createElement('div');
  dvWrap.className = 'dv';
  const dvIns = dv.map((x) => numberInput(x));
  dvWrap.append(...dvIns);
  const drag = document.createElement('input');
  drag.type = 'checkbox';
  drag.checked = true;
  const dragLabel = document.createElement('label');
  dragLabel.className = 'check';
  dragLabel.title = 'Keep the parent\'s drag coefficient table';
  dragLabel.append(drag, document.createTextNode('Drag'));
  const remove = document.createElement('button');
  remove.className = 'remove';
  remove.textContent = '✕';
  remove.title = 'Remove child';
  remove.setAttribute('aria-label', 'Remove child');
  remove.addEventListener('click', () => {
    row.remove();
    updateMassCheck();
  });
  const dvField = labelled('', dvWrap);
  dvField.span.className = 'dv-label';
  row.append(
    labelled('New ID', idIn).label,
    labelled('Mass (kg)', mass).label,
    labelled('Dry mass (kg)', dry).label,
    remove,
    dvField.label,
    dragLabel,
  );
  row.fields = { idIn, mass, dry, dvIns, drag };
  ui.sChildren.append(row);
  updateStageFrame();
  updateMassCheck();
}

function resetStageRows() {
  ui.sChildren.replaceChildren();
  if (!selectedId) return;
  addStageRow(`${selectedId}_s1`, [0, 0, -0.5]);
  addStageRow(`${selectedId}_s2`, [0, 0, 0.5]);
}

function updateMassCheck() {
  const c = selectedCraft();
  const total = [...ui.sChildren.children].reduce((sum, row) => sum + (Number(row.fields.mass.value) || 0), 0);
  ui.sMassCheck.textContent = c ? `children total ${fmtNum(total, 1)} kg / current mass ${fmtNum(c.mass, 1)} kg` : '';
}

function updateStageFrame() {
  const enu = ui.sFrame.value === 'enu';
  ui.sBodyField.classList.toggle('hidden', !enu);
  for (const span of ui.sChildren.querySelectorAll('.dv-label')) {
    span.textContent = enu ? 'Δv east, north, up (m/s)' : 'Δv x, y, z (m/s)';
  }
}
ui.sFrame.addEventListener('change', updateStageFrame);
ui.sAdd.addEventListener('click', () => addStageRow());

ui.sApply.addEventListener('click', async () => {
  const children = [];
  for (const row of ui.sChildren.children) {
    const f = row.fields;
    const child = { id: f.idIn.value.trim(), mass: Number(f.mass.value), delta_v: f.dvIns.map((i) => Number(i.value) || 0) };
    if (!child.id || !(child.mass > 0)) {
      logNote('each child needs an id and a mass > 0', 'err');
      return;
    }
    if (f.dry.value !== '') child.dry_mass = Number(f.dry.value);
    if (!f.drag.checked) child.drag_coefficient = null;
    children.push(child);
  }
  if (children.length < 2) {
    logNote('staging needs at least two children', 'err');
    return;
  }
  const cmd = { cmd: 'stage', id: selectedId, children, frame: ui.sFrame.value };
  if (ui.sFrame.value === 'enu') cmd.body = ui.sBody.value;
  const reply = await send(cmd);
  if (reply.ok) {
    // Follow the heaviest child (usually the upper stage + payload).
    const heaviest = children.reduce((a, b) => (b.mass > a.mass ? b : a));
    await refresh();
    selectCraft(heaviest.id);
  }
});

// ---------------------------------------------------------------------------
// Console
// ---------------------------------------------------------------------------

const EXAMPLES = () => {
  const id = selectedId ?? 'falcon9';
  const body = selectedCraft()?.nearest_body.id ?? 'earth';
  return {
    'info': { cmd: 'info' },
    'ping': { cmd: 'ping' },
    'get_spacecraft': { cmd: 'get_spacecraft', id, relative_to: body },
    'get_body': { cmd: 'get_body', id: body },
    'get_state (spacecraft only)': { cmd: 'get_state', bodies: false },
    'step 10 s': { cmd: 'step', dt: 10 },
    'set_thrust (ENU, straight up)': { cmd: 'set_thrust', id, force: [0, 0, 7.607e6], mass_rate: -2750, frame: 'enu', body },
    'cut engine': { cmd: 'set_thrust', id, force: [0, 0, 0], mass_rate: 0 },
    'stage': { cmd: 'stage', id, frame: 'enu', body, children: [
      { id: `${id}_s1`, mass: 25600, delta_v: [0, 0, -0.5] },
      { id: `${id}_s2`, mass: 127754, dry_mass: 35084, delta_v: [0, 0, 0.5] }] },
    'reset': { cmd: 'reset' },
  };
};

ui.consoleExamples.append(...Object.keys(EXAMPLES()).map((k) => new Option(k, k)));
ui.consoleExamples.addEventListener('change', () => {
  const ex = EXAMPLES()[ui.consoleExamples.value];
  if (ex) ui.consoleText.value = JSON.stringify(ex, null, 1).replace(/\n\s*/g, ' ');
  ui.consoleExamples.value = '';
});

async function sendConsole() {
  let req;
  try {
    req = JSON.parse(ui.consoleText.value);
  } catch (err) {
    logNote(`invalid JSON: ${err.message}`, 'err');
    return;
  }
  if (!req || typeof req !== 'object' || Array.isArray(req)) {
    logNote('command must be a JSON object with a "cmd" field', 'err');
    return;
  }
  const reply = await send(req);
  if (reply.ok && ['reset', 'stage', 'set_thrust', 'step'].includes(req.cmd)) {
    if (req.cmd === 'reset') await loadInfo();
    await refresh();
  }
}

ui.consoleSend.addEventListener('click', sendConsole);
ui.consoleText.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    sendConsole();
  }
});

// ---------------------------------------------------------------------------
// Startup and polling
// ---------------------------------------------------------------------------

async function poll() {
  if (!running) {
    if (!bodyIds.length) await loadInfo();
    if (bodyIds.length) await refresh();
  }
  setTimeout(poll, POLL_MS);
}

(async () => {
  try {
    const cfg = await (await fetch('/api/config')).json();
    viewerBase = cfg.viewer_url || `${location.protocol}//${location.hostname}:8080/`;
  } catch {
    viewerBase = `${location.protocol}//${location.hostname}:8080/`;
  }
  updateViewerLink();
  updateRawFrame();
  updateStageFrame();
  selectCraft(null);
  poll();
})();
