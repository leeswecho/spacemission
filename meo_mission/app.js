// Falcon 9 to MEO mission page: questionnaire -> server-side autopilot -> live progress.

const POLL_MS = 500;
const PRESETS = {
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

const $ = (id) => document.getElementById(id);
const ui = {
  simStatus: $('sim-status'), viewerLink: $('viewer-link'),
  form: $('form'), perigee: $('perigee'), apogee: $('apogee'), inclination: $('inclination'), payload: $('payload'),
  reset: $('reset'), launch: $('launch'), feasibility: $('feasibility'), errors: $('errors'),
  meoRange: $('meo-range'), incHelp: $('inc-help'),
  state: $('mission-state'), result: $('result'), phases: $('phases'), telemetry: $('telemetry'),
  telVehicle: $('tel-vehicle'), abort: $('abort'), log: $('log'),
};

let config = null;
let viewerBase = null;
let running = false;
let lastLogLength = -1;
let lastLogKey = '';

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

function formValues() {
  return {
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
  ui.payload.setAttribute('aria-invalid', String(bad('Payload')));
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
    ['Upper stage Δv needed', `${fmt(est.dv_needed / 1000, 2)} km/s`],
    ['Upper stage Δv available', `${fmt(est.s2_capacity / 1000, 2)} km/s`],
    ['Margin', `${est.margin >= 0 ? '+' : ''}${fmt(est.margin)} m/s`],
    ['Estimated max payload for this orbit', `≈ ${fmt(est.max_payload)} kg`],
  ];
  const dl = document.createElement('dl');
  for (const [k, v] of rows) {
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = v;
    dl.append(dt, dd);
  }
  ui.feasibility.replaceChildren(verdict, dl);
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

ui.form.addEventListener('input', scheduleValidate);
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
  if (values.reset === false && !confirm('Launch without resetting? The Falcon 9 must still be on the pad.')) return;
  ui.launch.disabled = true;
  try {
    const reply = await postJSON('/api/start', values);
    if (!reply.ok) renderErrors(reply.errors ?? ['Launch rejected.']);
  } catch {
    renderErrors(['Mission server unreachable.']);
  }
  poll(true);
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
    ['Propellant', `${fmt(tel.propellant)} kg`],
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

function renderResult(snap) {
  const done = ['succeeded', 'failed', 'aborted'].includes(snap.state);
  ui.result.classList.toggle('hidden', !done);
  if (!done) return;
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
    setPill(ui.state, snap.abort_requested && running ? 'Aborting…' : label, kind);
    for (const fs of ui.form.querySelectorAll('fieldset, .presets button')) fs.disabled = running;
    ui.abort.disabled = !running;
    updateLaunchButton();

    const t0 = launchTime(snap.log);
    renderPhases(snap.phases);
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
  const min = config.meo_min_km ?? 2000;
  const max = config.meo_max_km ?? 35786;
  ui.meoRange.textContent = `${fmt(min)} km and ${fmt(max)} km (geostationary altitude). GPS satellites fly at 20,180 km`;
  const lat = config.launch_latitude ?? 28.6;
  const minInc = Math.ceil(lat * 10) / 10;
  ui.incHelp.textContent = `Launching from Cape Canaveral (${lat.toFixed(1)}° N), the inclination must be between `
    + `${minInc}° and ${(180 - minInc).toFixed(1)}°. GPS uses 55°, Galileo 56°, GLONASS 64.8°.`;
  if (!config.phases) renderPhases([]);
  else renderPhases(config.phases.map((p) => ({ ...p, state: 'pending' })));
  updateViewerLink(null);
  scheduleValidate();
  poll();
})();
