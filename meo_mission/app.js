// Rocket to Earth orbit mission page: questionnaire -> server-side autopilot -> live progress.

import { installGlossary } from '/shared/glossary.js';

const POLL_MS = 500;
const PRESETS = {
  iss: { perigee: 420, apogee: 420, inclination: 51.6 },
  starlink: { perigee: 550, apogee: 550, inclination: 53 },
  polar: { perigee: 800, apogee: 800, inclination: 90 },
  sso: { perigee: 700, apogee: 700, inclination: 98.2 },
  gto: { perigee: 200, apogee: 35786, inclination: 28.7 },
  molniya: { perigee: 500, apogee: 39800, inclination: 63.4 },
  gps: { perigee: 20180, apogee: 20180, inclination: 55 },
  galileo: { perigee: 23222, apogee: 23222, inclination: 56 },
  glonass: { perigee: 19100, apogee: 19100, inclination: 64.8 },
};
const STATE_LABELS = {
  idle: ['Not launched', 'idle'],
  running: ['In flight', 'run'],
  succeeded: ['Succeeded', 'ok'],
  failed: ['Failed', 'bad'],
  aborted: ['Aborted', 'bad'],
};

// Editable vehicle fields: [key, label (as in server messages), unit, scale from display to SI].
const STAGE_FIELDS = {
  boosters: [['dry', 'dry mass', 'kg', 1], ['prop', 'propellant', 'kg', 1],
    ['thrust_sl', 'sea-level thrust', 'kN', 1000], ['thrust_vac', 'vacuum thrust', 'kN', 1000],
    ['mdot', 'propellant flow', 'kg/s', 1]],
  core: [['dry', 'dry mass', 'kg', 1], ['prop', 'propellant', 'kg', 1],
    ['thrust_sl', 'sea-level thrust', 'kN', 1000], ['thrust_vac', 'vacuum thrust', 'kN', 1000],
    ['mdot', 'propellant flow', 'kg/s', 1]],
  upper: [['dry', 'dry mass', 'kg', 1], ['prop', 'propellant', 'kg', 1],
    ['thrust', 'thrust', 'kN', 1000], ['mdot', 'propellant flow', 'kg/s', 1]],
};
const STAGE_NAMES = { boosters: 'Booster', core: 'First stage', upper: 'Upper stage' };
const G0 = 9.80665;

const $ = (id) => document.getElementById(id);
const ui = {
  simStatus: $('sim-status'), viewerLink: $('viewer-link'),
  form: $('form'), perigee: $('perigee'), apogee: $('apogee'), inclination: $('inclination'), payload: $('payload'),
  reset: $('reset'), launch: $('launch'), feasibility: $('feasibility'), errors: $('errors'),
  orbitRange: $('orbit-range'), incHelp: $('inc-help'),
  rockets: $('rockets'), stages: $('stages'), vehicleDetails: $('vehicle-details'),
  vehicleEdited: $('vehicle-edited'), vehicleReset: $('vehicle-reset'),
  state: $('mission-state'), result: $('result'), phases: $('phases'), telemetry: $('telemetry'),
  telVehicle: $('tel-vehicle'), abort: $('abort'), pause: $('pause'), log: $('log'),
};

let config = null;
let viewerBase = null;
let running = false;
let lastLogLength = -1;
let lastLogKey = '';
let vehicleKey = null;
let vehicleInputs = [];  // {input, block, key, scale, errorPrefixes}
let stageBlocks = [];
let idlePhases = [];

const fmt = (x, digits = 0) => (x === null || x === undefined || !Number.isFinite(x)
  ? '—' : x.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }));

function fmtT(seconds) {
  const sign = seconds < 0 ? '-' : '+';
  let s = Math.abs(seconds);
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  s -= m * 60;
  return `T${sign}${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`;
}

// ---------------------------------------------------------------------------
// Rocket choice and editable figures
// ---------------------------------------------------------------------------

function vehicles() {
  return config?.vehicles ?? {};
}

function renderRockets() {
  const entries = Object.entries(vehicles());
  ui.rockets.replaceChildren(...entries.map(([key, v]) => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'radio';
    input.name = 'vehicle';
    input.value = key;
    input.checked = key === vehicleKey;
    input.addEventListener('change', () => selectVehicle(key));
    const text = document.createElement('span');
    const name = document.createElement('strong');
    name.textContent = v.name;
    const small = document.createElement('small');
    small.textContent = `${v.maker} · ${v.summary}`;
    text.append(name, small);
    label.append(input, text);
    return label;
  }));
}

const roundFor = (x) => Number(x.toPrecision(6));

function numberField(labelText, unit, value, attrs = {}) {
  const wrap = document.createElement('label');
  wrap.className = 'field';
  const name = document.createElement('span');
  name.className = 'field-name';
  name.textContent = labelText;
  const row = document.createElement('span');
  row.className = 'field-row';
  const input = document.createElement('input');
  input.type = 'number';
  input.inputMode = 'decimal';
  input.step = attrs.step ?? 'any';
  if (attrs.min !== undefined) input.min = attrs.min;
  if (attrs.max !== undefined) input.max = attrs.max;
  input.value = value;
  input.dataset.default = String(value);
  const u = document.createElement('span');
  u.className = 'unit';
  u.textContent = unit;
  row.append(input, u);
  wrap.append(name, row);
  return { wrap, input };
}

function stageBlock(title) {
  const box = document.createElement('section');
  box.className = 'stage';
  const h = document.createElement('h4');
  h.textContent = title;
  const grid = document.createElement('div');
  grid.className = 'stage-grid';
  const derived = document.createElement('p');
  derived.className = 'derived';
  box.append(h, grid, derived);
  return { box, grid, derived, block: null };
}

function renderStages() {
  const v = vehicles()[vehicleKey];
  vehicleInputs = [];
  stageBlocks = [];
  if (!v) {
    ui.stages.replaceChildren();
    return;
  }
  for (const block of ['boosters', 'core', 'upper']) {
    const st = v[block];
    if (!st) continue;
    const b = stageBlock(block === 'boosters' ? 'Boosters (figures per booster)' : STAGE_NAMES[block]);
    b.block = block;
    if (block === 'boosters') {
      const { wrap, input } = numberField('Number of boosters', `of ${st.max_count} max`, st.count,
        { step: 1, min: 0, max: st.max_count });
      b.grid.append(wrap);
      vehicleInputs.push({ input, block, key: 'count', scale: 1, errorPrefixes: ['Number of boosters'] });
    }
    for (const [key, label, unit, scale] of STAGE_FIELDS[block]) {
      const name = label[0].toUpperCase() + label.slice(1);
      const { wrap, input } = numberField(name, unit, roundFor(st[key] / scale));
      b.grid.append(wrap);
      const stage = STAGE_NAMES[block];
      const prefixes = [`${stage} ${label}`];
      if (key.startsWith('thrust') || key === 'mdot') prefixes.push(`${stage} thrust and propellant flow`);
      if (key === 'thrust_sl' || key === 'thrust_vac') prefixes.push(`${stage} sea-level thrust cannot`);
      vehicleInputs.push({ input, block, key, scale, errorPrefixes: prefixes });
    }
    if (block === 'core' && st.boost_throttle !== undefined) {
      const { wrap, input } = numberField('Throttle while boosters burn', '%', Math.round(st.boost_throttle * 100),
        { step: 1, min: 30, max: 100 });
      b.grid.append(wrap);
      vehicleInputs.push({ input, block, key: 'boost_throttle', scale: 0.01, errorPrefixes: ['Core throttle'] });
    }
    stageBlocks.push(b);
  }
  const other = stageBlock('Other');
  for (const [key, label, unit, prefix] of [['fairing', 'Fairing mass', 'kg', 'Fairing mass'],
    ['kick_angle', 'Pitch kick at 60 m/s', '°', 'Pitch kick']]) {
    const { wrap, input } = numberField(label, unit, v[key]);
    other.grid.append(wrap);
    vehicleInputs.push({ input, block: null, key, scale: 1, errorPrefixes: [prefix] });
  }
  other.derived.textContent = 'Rockets with a low liftoff thrust-to-weight need a smaller pitch kick.';
  stageBlocks.push(other);
  ui.stages.replaceChildren(...stageBlocks.map((b) => b.box));
  updateDerived();
}

function vehicleConfig() {
  const out = {};
  for (const { input, block, key, scale } of vehicleInputs) {
    const x = input.value === '' ? null : Number(input.value) * scale;
    if (block) (out[block] ??= {})[key] = x;
    else out[key] = x;
  }
  return out;
}

function updateDerived() {
  const cfg = vehicleConfig();
  const edited = vehicleInputs.some(({ input }) => input.value !== input.dataset.default);
  ui.vehicleEdited.classList.toggle('hidden', !edited);
  for (const b of stageBlocks) {
    if (!b.block) continue;
    const st = cfg[b.block];
    const isp = (st.thrust_vac ?? st.thrust) / (st.mdot * G0);
    const burn = st.prop / st.mdot;
    b.derived.textContent = Number.isFinite(isp) && Number.isFinite(burn)
      ? `Vacuum Isp ${fmt(isp)} s · burns for ${fmt(burn)} s at full flow`
      : '';
  }
}

function selectVehicle(key) {
  vehicleKey = key;
  for (const r of ui.rockets.querySelectorAll('input')) r.checked = r.value === key;
  renderStages();
  scheduleValidate();
}

ui.vehicleReset.addEventListener('click', () => {
  renderStages();
  scheduleValidate();
});

function formValues() {
  return {
    vehicle: vehicleKey,
    vehicle_config: vehicleConfig(),
    perigee_km: Number(ui.perigee.value),
    apogee_km: Number(ui.apogee.value),
    inclination_deg: Number(ui.inclination.value),
    payload_kg: Number(ui.payload.value),
    pace: ui.form.querySelector('input[name="pace"]:checked').value,
    reset: ui.reset.checked,
  };
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
  return res.json();
}

// ---------------------------------------------------------------------------
// Questionnaire: live validation and feasibility estimate
// ---------------------------------------------------------------------------

function renderErrors(errors) {
  ui.errors.replaceChildren(...errors.map((e) => {
    const li = document.createElement('li');
    li.textContent = e;
    return li;
  }));
  const bad = (label) => errors.some((e) => e.startsWith(label));
  ui.perigee.setAttribute('aria-invalid', String(bad('Perigee')));
  ui.apogee.setAttribute('aria-invalid', String(bad('Apogee')));
  ui.inclination.setAttribute('aria-invalid', String(bad('From Cape') || bad('Inclination')));
  ui.payload.setAttribute('aria-invalid', String(bad('Payload') || bad('Liftoff thrust')));
  let vehicleBad = false;
  for (const { input, errorPrefixes } of vehicleInputs) {
    const own = errorPrefixes.some(bad) || bad('The first stage would run out');
    input.setAttribute('aria-invalid', String(own));
    vehicleBad ||= own;
  }
  if (vehicleBad) ui.vehicleDetails.open = true;
}

function renderFeasibility(est) {
  ui.feasibility.className = 'feasibility';
  if (!est) {
    ui.feasibility.replaceChildren();
    return;
  }
  const kind = est.margin >= 300 ? 'good' : est.margin >= 0 ? 'tight' : 'bad';
  ui.feasibility.classList.add(kind);
  const verdict = document.createElement('p');
  verdict.className = 'verdict';
  verdict.textContent = {
    good: 'Looks achievable.',
    tight: 'Marginal: very little propellant to spare.',
    bad: 'Probably not enough propellant: reduce the payload or lower the orbit.',
  }[kind];
  const rows = [
    ['Liftoff mass', `${fmt(est.liftoff_mass)} kg`],
    ['Liftoff thrust-to-weight', fmt(est.liftoff_tw, 2)],
    ['Δv needed (incl. ~1.7 km/s of losses)', `${fmt(est.dv_needed / 1000, 2)} km/s`],
    ['Δv available (ideal, all stages)', `${fmt(est.dv_available / 1000, 2)} km/s`],
    ...est.stages.map((st) => [`· ${st.label}`, `${fmt(st.dv / 1000, 2)} km/s`]),
    ['Margin', `${est.margin >= 0 ? '+' : ''}${fmt(est.margin)} m/s`],
    ['Estimated max payload for this orbit', `≈ ${fmt(est.max_payload)} kg`],
  ];
  const dl = document.createElement('dl');
  for (const [k, v] of rows) {
    const dt = document.createElement('dt');
    dt.textContent = k;
    if (k.startsWith('·')) dt.className = 'sub';
    const dd = document.createElement('dd');
    dd.textContent = v;
    dl.append(dt, dd);
  }
  const note = document.createElement('p');
  note.className = 'note';
  note.textContent = 'A rough estimate: actual losses depend on the rocket and its trajectory.';
  ui.feasibility.replaceChildren(verdict, dl, note);
}

let validateTimer = null;
let validateSeq = 0;
function scheduleValidate() {
  clearTimeout(validateTimer);
  validateTimer = setTimeout(async () => {
    const seq = ++validateSeq;
    try {
      const reply = await postJSON('/api/validate', formValues());
      if (seq !== validateSeq) return;  // a newer edit is already being checked
      renderErrors(reply.errors ?? []);
      renderFeasibility(reply.estimate);
      if (reply.phases) idlePhases = reply.phases.map((p) => ({ ...p, state: 'pending' }));
      updateLaunchButton(reply.errors?.length ?? 1);
    } catch {
      renderErrors(['Mission server unreachable.']);
    }
  }, 200);
}

let formErrorCount = 0;
function updateLaunchButton(errorCount = formErrorCount) {
  formErrorCount = errorCount;
  ui.launch.disabled = running || errorCount > 0;
}

ui.form.addEventListener('input', (e) => {
  if (ui.stages.contains(e.target)) updateDerived();
  scheduleValidate();
});
for (const btn of document.querySelectorAll('[data-preset]')) {
  btn.addEventListener('click', () => {
    const p = PRESETS[btn.dataset.preset];
    ui.perigee.value = p.perigee;
    ui.apogee.value = p.apogee;
    ui.inclination.value = p.inclination;
    scheduleValidate();
  });
}

ui.form.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (running) return;
  const values = formValues();
  if (values.reset === false && !confirm('Launch without resetting? Whatever stands on the LC-39A pad is replaced by the '
    + 'new rocket. Other craft are left alone, but none may already use this flight\'s IDs.')) return;
  ui.launch.disabled = true;
  try {
    const reply = await postJSON('/api/start', values);
    if (!reply.ok) renderErrors(reply.errors ?? ['Launch rejected.']);
  } catch {
    renderErrors(['Mission server unreachable.']);
  }
  poll(true);
});

let paused = false;
ui.pause.addEventListener('click', async () => {
  ui.pause.disabled = true;
  try {
    await postJSON('/api/pause', { paused: !paused });
  } finally {
    poll(true);
  }
});

ui.abort.addEventListener('click', async () => {
  if (!confirm('Abort the mission? Engines will be shut down immediately.')) return;
  await postJSON('/api/abort');
});

// ---------------------------------------------------------------------------
// Flight progress
// ---------------------------------------------------------------------------

function setPill(el, text, kind) {
  el.textContent = text;
  el.className = `pill pill-${kind}`;
}

function renderPhases(phases) {
  ui.phases.replaceChildren(...phases.map((p) => {
    const li = document.createElement('li');
    li.className = p.state;
    li.textContent = p.title;
    return li;
  }));
}

function launchTime(log) {
  const e = log.find((x) => x.text.startsWith('Ignition and liftoff'));
  return e ? e.t : null;
}

// Remaining propellant, with the share of the attached stages' full load when known.
function propellantText(tel) {
  const kg = `${fmt(tel.propellant)} kg`;
  if (!tel.propellant_full) return kg;
  const pct = (100 * tel.propellant) / tel.propellant_full;
  return `${kg} (${fmt(pct, pct < 10 ? 1 : 0)}%)`;
}

function renderTelemetry(tel, t0) {
  if (!tel || tel.t === undefined) {
    ui.telemetry.replaceChildren();
    ui.telVehicle.textContent = '';
    return;
  }
  ui.telVehicle.textContent = `${tel.vehicle} · ${tel.note}`;
  const cells = [
    ['Mission time', t0 !== null ? fmtT(tel.t - t0) : '—'],
    ['Altitude', `${fmt(tel.altitude_km, tel.altitude_km < 1000 ? 1 : 0)} km`],
    ['Speed (inertial)', `${fmt(tel.speed)} m/s`],
    ['Vertical speed', `${fmt(tel.vertical_speed)} m/s`],
    ['Perigee', `${fmt(tel.perigee_km)} km`],
    ['Apogee', tel.apogee_km === null ? 'escape' : `${fmt(tel.apogee_km)} km`],
    ['Inclination', `${fmt(tel.inclination, 2)}°`],
    ['Mass', `${fmt(tel.mass)} kg`],
    ['Propellant', propellantText(tel)],
    ['Thrust', `${fmt(tel.thrust_kn)} kN`],
    ['Acceleration', `${fmt(tel.accel_g, 2)} g`],
    ['Dynamic pressure', `${fmt(tel.dynamic_pressure / 1000, 1)} kPa`],
  ];
  ui.telemetry.replaceChildren(...cells.map(([k, v]) => {
    const div = document.createElement('div');
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = v;
    div.append(dt, dd);
    return div;
  }));
}

function renderLog(log, t0) {
  const key = log.length ? `${log.length}:${log[log.length - 1].wall}` : '0';
  if (key === lastLogKey) return;
  lastLogKey = key;
  ui.log.replaceChildren(...log.slice().reverse().map((e) => {
    const li = document.createElement('li');
    const t = document.createElement('span');
    t.className = 't';
    t.textContent = t0 !== null ? fmtT(e.t - t0) : `t=${fmt(e.t, 1)} s`;
    const msg = document.createElement('span');
    msg.className = e.kind;
    msg.textContent = e.text;
    li.append(t, msg);
    return li;
  }));
}

let lastResultKey = '';
function renderResult(snap) {
  const done = ['succeeded', 'failed', 'aborted'].includes(snap.state);
  ui.result.classList.toggle('hidden', !done);
  if (!done) {
    lastResultKey = '';
    return;
  }
  // Rebuild only when the outcome changes, so the download link isn't replaced under the pointer.
  const key = JSON.stringify([snap.state, snap.message, snap.result, snap.log.length]);
  if (key === lastResultKey) return;
  lastResultKey = key;
  ui.result.className = `result ${snap.state === 'succeeded' ? 'success' : 'failure'}`;
  const h = document.createElement('h3');
  h.textContent = snap.message ?? snap.state;
  const parts = [h];
  const r = snap.result;
  if (r) {
    const table = document.createElement('table');
    const head = document.createElement('tr');
    for (const c of ['', 'Target', 'Achieved', 'Error']) {
      const th = document.createElement('th');
      th.textContent = c;
      head.append(th);
    }
    table.append(head);
    const rows = [
      ['Perigee', r.target.perigee_km, r.perigee_km, 'km', 0],
      ['Apogee', r.target.apogee_km, r.apogee_km, 'km', 0],
      ['Inclination', r.target.inclination_deg, r.inclination_deg, '°', 2],
    ];
    for (const [label, target, got, unit, d] of rows) {
      const tr = document.createElement('tr');
      const err = got === null ? null : got - target;
      for (const text of [label, `${fmt(target, d)} ${unit}`, got === null ? 'escape' : `${fmt(got, d)} ${unit}`,
        err === null ? '—' : `${err >= 0 ? '+' : ''}${fmt(err, d === 0 ? 1 : 3)} ${unit}`]) {
        const td = document.createElement(text === label ? 'th' : 'td');
        td.textContent = text;
        tr.append(td);
      }
      table.append(tr);
    }
    parts.push(table);
    if (r.propellant_left_kg !== undefined && r.propellant_left_kg !== null) {
      const p = document.createElement('p');
      p.className = 'muted small';
      p.textContent = `Upper-stage propellant remaining: ${fmt(r.propellant_left_kg)} kg. Craft: ${r.craft}.`;
      parts.push(p);
    }
  }
  const actions = document.createElement('div');
  actions.className = 'actions';
  const download = document.createElement('a');
  download.className = 'button';
  download.href = '/api/mission/commands.txt';
  download.download = '';
  download.textContent = 'Download command log';
  download.title = 'Every command the autopilot sent to the simulation, with times, as a text file';
  actions.append(download);
  parts.push(actions);
  ui.result.replaceChildren(...parts);
}

function updateViewerLink(vehicle) {
  if (!viewerBase) return;
  const url = new URL(viewerBase);
  if (vehicle) url.searchParams.set('focus', vehicle);
  ui.viewerLink.href = url.toString();
}

async function poll(once = false) {
  try {
    const snap = await (await fetch('/api/mission', { cache: 'no-store' })).json();
    if (snap.sim?.connected) setPill(ui.simStatus, 'Simulation online', 'ok');
    else setPill(ui.simStatus, 'Simulation unreachable', 'bad');

    running = snap.state === 'running';
    const [label, kind] = STATE_LABELS[snap.state] ?? [snap.state, 'idle'];
    paused = running && Boolean(snap.paused);
    if (snap.abort_requested && running) setPill(ui.state, 'Aborting…', kind);
    else if (paused) setPill(ui.state, 'Paused', 'wait');
    else setPill(ui.state, label, kind);
    ui.pause.disabled = !running || Boolean(snap.abort_requested);
    ui.pause.textContent = paused ? 'Resume' : 'Pause';
    for (const fs of ui.form.querySelectorAll('fieldset, .presets button')) fs.disabled = running;
    ui.abort.disabled = !running;
    updateLaunchButton();

    const t0 = launchTime(snap.log);
    renderPhases(snap.state === 'idle' ? idlePhases : snap.phases);
    renderTelemetry(snap.telemetry, t0);
    renderLog(snap.log, t0);
    renderResult(snap);
    updateViewerLink(snap.vehicle);
  } catch {
    setPill(ui.simStatus, 'Mission server unreachable', 'bad');
  }
  if (!once) setTimeout(poll, POLL_MS);
}

(async () => {
  try {
    config = await (await fetch('/api/config')).json();
  } catch {
    config = {};
  }
  viewerBase = config.viewer_url || `${location.protocol}//${location.hostname}:8080/`;
  const min = config.orbit_min_km ?? 200;
  const max = config.orbit_max_km ?? 40000;
  ui.orbitRange.textContent = `${fmt(min)} km (the parking orbit) and ${fmt(max)} km. `
    + 'For reference: the ISS flies at about 420 km, GPS at 20,180 km, geostationary satellites at 35,786 km';
  const lat = config.launch_latitude ?? 28.6;
  const minInc = Math.ceil(lat * 10) / 10;
  ui.incHelp.textContent = `Launching from Cape Canaveral (${lat.toFixed(1)}° N), the inclination must be between `
    + `${minInc}° and ${(180 - minInc).toFixed(1)}°. The ISS uses 51.6°, GPS 55°, polar orbits 90° and sun-synchronous ones about 98°.`;
  vehicleKey = config.default_vehicle ?? Object.keys(vehicles())[0] ?? null;
  renderRockets();
  renderStages();
  installGlossary();
  updateViewerLink(null);
  scheduleValidate();
  poll();
})();
