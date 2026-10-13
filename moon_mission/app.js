// Moon mission page: one tab per mission part -> server-side autopilot -> live progress.

import { installGlossary } from '/shared/glossary.js';

const POLL_MS = 500;
const G0 = 9.80665;
const PARTS = ['launch', 'transfer', 'landing', 'return'];
const STATE_LABELS = {
  idle: ['Not launched', 'idle'],
  running: ['In flight', 'run'],
  succeeded: ['Succeeded', 'ok'],
  failed: ['Failed', 'bad'],
  aborted: ['Aborted', 'bad'],
};
const EARTH_PRESETS = [
  ['Parking orbit', 200, 200, 28.7, '200 km circular, 28.7° (due east from the Cape)'],
  ['ISS', 420, 420, 51.6, 'About 420 km circular, 51.6°'],
  ['Polar', 800, 800, 90, '800 km circular, 90°'],
  ['GTO', 200, 35786, 28.7, 'Geostationary transfer orbit: 200 × 35,786 km, 28.7°'],
];
const LUNAR_PRESETS = [
  ['Apollo', 110, 110, 10, '110 km circular, near-equatorial (10°)'],
  ['Low polar', 100, 100, 90, '100 km circular, 90°'],
  ['Elliptical polar', 100, 3000, 90, '100 × 3,000 km, 90°'],
  ['High', 1000, 1000, 45, '1,000 km circular, 45°'],
];
// Editable rocket fields (tab 1): [key, label as in server messages, unit, scale from display to SI].
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
// Engine figures: [key, label, unit, scale from display to SI].
const ENGINE_FIELDS = [['thrust', 'Thrust (each, vacuum)', 'kN', 1000], ['isp', 'Isp (vacuum)', 's', 1],
  ['mass', 'Mass (each)', 'kg', 1], ['min_throttle', 'Lowest throttle', '%', 0.01]];
const STORE_KEY = 'moon-mission-forms-v1';

const $ = (id) => document.getElementById(id);
const ui = {
  simStatus: $('sim-status'), viewerLink: $('viewer-link'),
  state: $('mission-state'), flightPart: $('flight-part'), result: $('result'), phases: $('phases'),
  telemetry: $('telemetry'), telVehicle: $('tel-vehicle'), abort: $('abort'), pause: $('pause'), log: $('log'),
};
const forms = Object.fromEntries(PARTS.map((p) => [p, document.querySelector(`form[data-part="${p}"]`)]));
const role = (form, name) => form.querySelector(`[data-role="${name}"]`);

let config = {};
let viewerBase = null;
let running = false;
let activePart = 'launch';
let lastLogKey = '';
let lastResultKey = '';
let lastSnap = null;
const tabState = Object.fromEntries(PARTS.map((p) => [p, { errors: [], estimate: null, phases: [] }]));

const fmt = (x, digits = 0) => (x === null || x === undefined || !Number.isFinite(x)
  ? '—' : x.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits }));
const roundFor = (x) => Number(x.toPrecision(6));

function fmtT(seconds) {
  const sign = seconds < 0 ? '-' : '+';
  let s = Math.abs(seconds);
  const d = Math.floor(s / 86400);
  s -= d * 86400;
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  s -= m * 60;
  const hms = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${s.toFixed(1).padStart(4, '0')}`;
  return `T${sign}${d ? `${d}d ` : ''}${hms}`;
}

function el(tag, props = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (k in e) e[k] = v;
    else e.setAttribute(k, v);
  }
  e.append(...children);
  return e;
}

function numberField(labelText, unit, value, attrs = {}) {
  const input = el('input', { type: 'number', inputMode: 'decimal', step: attrs.step ?? 'any', value });
  if (attrs.min !== undefined) input.min = attrs.min;
  if (attrs.max !== undefined) input.max = attrs.max;
  input.dataset.default = String(value);
  const wrap = el('label', { class: 'field' }, el('span', { class: 'field-name', text: labelText }),
    el('span', { class: 'field-row' }, input, el('span', { class: 'unit', text: unit })));
  return { wrap, input };
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
// Tab 1: rocket choice and editable figures (as on the Earth-orbit page)
// ---------------------------------------------------------------------------

const rocket = { key: null, inputs: [], blocks: [] };

function renderRockets() {
  const form = forms.launch;
  const box = role(form, 'rockets');
  box.replaceChildren(...Object.entries(config.vehicles ?? {}).map(([key, v]) => {
    const input = el('input', { type: 'radio', name: 'vehicle', value: key, checked: key === rocket.key,
      onchange: () => selectRocket(key) });
    return el('label', {}, input, el('span', {}, el('strong', { text: v.name }),
      el('small', { text: `${v.maker} · ${v.summary}` })));
  }));
}

function renderStages(saved) {
  const form = forms.launch;
  const v = config.vehicles?.[rocket.key];
  rocket.inputs = [];
  rocket.blocks = [];
  if (!v) return;
  for (const block of ['boosters', 'core', 'upper']) {
    const st = v[block];
    if (!st) continue;
    const box = el('section', { class: 'stage' });
    const grid = el('div', { class: 'stage-grid' });
    const derived = el('p', { class: 'derived' });
    box.append(el('h4', { text: block === 'boosters' ? 'Boosters (figures per booster)' : STAGE_NAMES[block] }), grid, derived);
    if (block === 'boosters') {
      const { wrap, input } = numberField('Number of boosters', `of ${st.max_count} max`, st.count,
        { step: 1, min: 0, max: st.max_count });
      grid.append(wrap);
      rocket.inputs.push({ input, block, key: 'count', scale: 1, prefixes: ['Number of boosters'] });
    }
    for (const [key, label, unit, scale] of STAGE_FIELDS[block]) {
      const { wrap, input } = numberField(label[0].toUpperCase() + label.slice(1), unit, roundFor(st[key] / scale));
      grid.append(wrap);
      const stage = STAGE_NAMES[block];
      const prefixes = [`${stage} ${label}`];
      if (key.startsWith('thrust') || key === 'mdot') prefixes.push(`${stage} thrust and propellant flow`);
      if (key === 'thrust_sl' || key === 'thrust_vac') prefixes.push(`${stage} sea-level thrust cannot`);
      rocket.inputs.push({ input, block, key, scale, prefixes });
    }
    if (block === 'core' && st.boost_throttle !== undefined) {
      const { wrap, input } = numberField('Throttle while boosters burn', '%', Math.round(st.boost_throttle * 100),
        { step: 1, min: 30, max: 100 });
      grid.append(wrap);
      rocket.inputs.push({ input, block, key: 'boost_throttle', scale: 0.01, prefixes: ['Core throttle'] });
    }
    rocket.blocks.push({ block, derived, box });
  }
  const other = el('section', { class: 'stage' });
  const grid = el('div', { class: 'stage-grid' });
  other.append(el('h4', { text: 'Other' }), grid);
  for (const [key, label, unit, prefix] of [['fairing', 'Fairing mass', 'kg', 'Fairing mass'],
    ['kick_angle', 'Pitch kick at 60 m/s', '°', 'Pitch kick']]) {
    const { wrap, input } = numberField(label, unit, v[key]);
    grid.append(wrap);
    rocket.inputs.push({ input, block: null, key, scale: 1, prefixes: [prefix] });
  }
  rocket.blocks.push({ block: null, box: other });
  role(form, 'stages').replaceChildren(...rocket.blocks.map((b) => b.box));
  if (saved) {
    for (const r of rocket.inputs) {
      const x = r.block ? saved[r.block]?.[r.key] : saved[r.key];
      if (x !== undefined && x !== null) r.input.value = roundFor(x / r.scale);
    }
  }
  updateRocketDerived();
}

function rocketConfig() {
  const out = {};
  for (const { input, block, key, scale } of rocket.inputs) {
    const x = input.value === '' ? null : Number(input.value) * scale;
    if (block) (out[block] ??= {})[key] = x;
    else out[key] = x;
  }
  return out;
}

function updateRocketDerived() {
  const cfg = rocketConfig();
  const edited = rocket.inputs.some(({ input }) => input.value !== input.dataset.default);
  role(forms.launch, 'vehicle-edited').classList.toggle('hidden', !edited);
  for (const b of rocket.blocks) {
    if (!b.block) continue;
    const st = cfg[b.block];
    const isp = (st.thrust_vac ?? st.thrust) / (st.mdot * G0);
    const burn = st.prop / st.mdot;
    b.derived.textContent = Number.isFinite(isp) && Number.isFinite(burn)
      ? `Vacuum Isp ${fmt(isp)} s · burns for ${fmt(burn)} s at full flow` : '';
  }
}

function selectRocket(key, saved) {
  rocket.key = key;
  for (const r of role(forms.launch, 'rockets').querySelectorAll('input')) r.checked = r.value === key;
  renderStages(saved);
  changed(forms.launch);
}

// ---------------------------------------------------------------------------
// Engine picker (tabs 2-4)
// ---------------------------------------------------------------------------

const pickers = {};

function makeEnginePicker(form) {
  const box = role(form, 'engines');
  const landing = box.dataset.landing === '1';
  const select = el('select', { 'aria-label': 'Engine type' });
  for (const [key, e] of Object.entries(config.engines ?? {})) {
    select.append(el('option', { value: key, text: e.name }));
  }
  select.append(el('option', { value: 'custom', text: 'Custom engine (enter figures below)' }));
  const count = numberField('Number of engines', '', Number(box.dataset.defaultCount ?? 1), { step: 1, min: 1, max: 30 });
  const fields = ENGINE_FIELDS.map(([key, label, unit, scale]) => ({ key, scale, ...numberField(label, unit, 0) }));
  const use = el('p', { class: 'derived' });
  const derived = el('p', { class: 'derived' });
  const grid = el('div', { class: 'stage-grid' }, count.wrap, ...fields.map((f) => f.wrap));
  box.append(el('label', { class: 'field wide' }, el('span', { class: 'field-name', text: landing
    ? 'Engines (historical, in development, or your own figures)' : 'Engines' }), select), use, grid, derived);

  const picker = {
    select, count: count.input, fields, landing,
    fill(key) {
      const e = config.engines?.[key];
      if (!e) return;
      for (const f of fields) {
        f.input.value = roundFor(e[f.key] / f.scale);
        f.input.dataset.default = f.input.value;
      }
    },
    value() {
      const custom = {};
      for (const f of fields) custom[f.key] = f.input.value === '' ? null : Number(f.input.value) * f.scale;
      return { key: select.value, count: Number(count.input.value), custom };
    },
    set(v) {
      if (!v) return;
      if ([...select.options].some((o) => o.value === v.key)) select.value = v.key;
      picker.fill(select.value);
      if (v.count) count.input.value = v.count;
      for (const f of fields) {
        const x = v.custom?.[f.key];
        if (x !== undefined && x !== null) f.input.value = roundFor(x / f.scale);
      }
      picker.update();
    },
    update() {
      const e = config.engines?.[select.value];
      use.textContent = e ? `${e.use} · ${e.propellants}` : 'Your own figures.';
      const v = picker.value();
      const n = v.count;
      const c = v.custom;
      const flow = (c.thrust * n) / (c.isp * G0);
      derived.textContent = Number.isFinite(flow)
        ? `${n} × ${fmt(c.thrust / 1000, 1)} kN = ${fmt((n * c.thrust) / 1000, 1)} kN in total · engines weigh `
          + `${fmt(n * c.mass)} kg · ${fmt(flow, 1)} kg/s of propellant at full thrust`
          + (landing ? ` · throttles down to ${fmt(c.min_throttle * c.thrust / 1000, 1)} kN with one engine` : '')
        : '';
    },
    inputs() {
      // Server messages start with these words (engines.py and the estimates in moon_autopilot.py).
      const names = { thrust: 'Engine thrust', isp: 'Engine Isp', mass: 'Engine mass', min_throttle: 'Engine minimum throttle' };
      return [
        { input: count.input, prefixes: ['Engine count'] },
        ...fields.map((f) => ({ input: f.input, prefixes: [names[f.key]] })),
        { input: select, prefixes: ['Engine:'] },
      ];
    },
  };
  select.addEventListener('change', () => {
    if (select.value !== 'custom') picker.fill(select.value);
    picker.update();
  });
  select.value = box.dataset.defaultKey ?? config.default_engine;
  picker.fill(select.value);
  picker.update();
  return picker;
}

// ---------------------------------------------------------------------------
// Forms: values, presets, persistence
// ---------------------------------------------------------------------------

function formValues(part) {
  const form = forms[part];
  const out = { part };
  for (const e of form.querySelectorAll('[data-key]')) {
    const k = e.dataset.key;
    if (e.type === 'radio') {
      if (e.checked) out[k] = e.value;
    } else if (e.type === 'checkbox') {
      out[k] = e.checked;
    } else if (e.type === 'number') {
      out[k] = e.value === '' ? null : Number(e.value);
    } else {
      out[k] = e.value;
    }
  }
  if (part === 'launch') {
    out.vehicle = rocket.key;
    out.vehicle_config = rocketConfig();
  } else {
    out.engine = pickers[part].value();
  }
  return out;
}

function setField(form, key, value) {
  const e = form.querySelector(`[data-key="${key}"]`);
  if (e && value !== undefined && value !== null) e.value = typeof value === 'number' ? roundFor(value) : value;
}

function renderPresets(form) {
  for (const box of form.querySelectorAll('[data-presets]')) {
    const list = box.dataset.presets === 'earth' ? EARTH_PRESETS : LUNAR_PRESETS;
    const prefix = box.dataset.prefix;
    const keys = prefix === '' ? ['perigee_km', 'apogee_km', 'inclination_deg']
      : [`${prefix}periapsis_km`, `${prefix}apoapsis_km`, `${prefix}inclination_deg`];
    box.replaceChildren(el('span', { class: 'muted', text: 'Presets:' }), ...list.map(([name, p, a, i, title]) => el('button', {
      type: 'button', text: name, title,
      onclick: () => {
        keys.forEach((k, j) => setField(form, k, [p, a, i][j]));
        changed(form);
      },
    })));
  }
}

function renderSites() {
  const form = forms.landing;
  const box = role(form, 'sites');
  box.replaceChildren(el('span', { class: 'muted', text: 'Sites:' }), ...Object.values(config.sites ?? {}).map((s) => el('button', {
    type: 'button', text: s.name.split(' - ')[0], title: `${s.name}: ${s.lat}°, ${s.lon}°`,
    onclick: () => {
      setField(form, 'site_lat', s.lat);
      setField(form, 'site_lon', s.lon);
      setField(form, 'site_name', s.name);
      role(form, 'site-name').textContent = s.name;
      changed(form);
    },
  })));
  for (const k of ['site_lat', 'site_lon']) {
    form.querySelector(`[data-key="${k}"]`).addEventListener('input', () => {
      setField(form, 'site_name', 'Custom site');
      role(form, 'site-name').textContent = 'Custom site';
    });
  }
}

function updateSplit() {
  const v = formValues('landing');
  const lPay = (v.payload_kg * v.lander_payload_pct) / 100;
  const lProp = (v.propellant_kg * v.lander_propellant_pct) / 100;
  role(forms.landing, 'split').textContent = Number.isFinite(lPay + lProp)
    ? `Lander: ${fmt(lPay)} kg payload + ${fmt(lProp)} kg propellant = ${fmt(lPay + lProp)} kg. `
      + `Stays in orbit: ${fmt(v.payload_kg - lPay)} kg payload + ${fmt(v.propellant_kg - lProp)} kg propellant.`
    : '';
}

function saveForms() {
  try {
    const data = { active: activePart };
    for (const p of PARTS) data[p] = formValues(p);
    localStorage.setItem(STORE_KEY, JSON.stringify(data));
  } catch { /* storage unavailable: forms just start from defaults next time */ }
}

function loadForms() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) ?? 'null');
  } catch {
    return null;
  }
}

function restoreForm(part, v) {
  if (!v) return;
  const form = forms[part];
  for (const e of form.querySelectorAll('[data-key]')) {
    const x = v[e.dataset.key];
    if (x === undefined || x === null) continue;
    if (e.type === 'radio') e.checked = e.value === x;
    else if (e.type === 'checkbox') e.checked = Boolean(x);
    else e.value = x;
  }
  if (part === 'landing') role(form, 'site-name').textContent = v.site_name ?? '';
  if (part !== 'launch') pickers[part].set(v.engine);
}

// ---------------------------------------------------------------------------
// Validation and feasibility
// ---------------------------------------------------------------------------

function fieldInputs(part) {
  const form = forms[part];
  const out = [...form.querySelectorAll('[data-err]')].map((input) => ({ input, prefixes: input.dataset.err.split('|') }));
  if (part === 'launch') out.push(...rocket.inputs.map(({ input, prefixes }) => ({ input, prefixes })));
  else out.push(...pickers[part].inputs());
  return out;
}

function renderErrors(part, errors) {
  const form = forms[part];
  role(form, 'errors').replaceChildren(...errors.map((e) => el('li', { text: e })));
  const bad = (label) => errors.some((e) => e.startsWith(label));
  let rocketBad = false;
  for (const { input, prefixes } of fieldInputs(part)) {
    const own = prefixes.some(bad) || (part === 'launch' && rocket.inputs.some((r) => r.input === input)
      && bad('The first stage would run out'));
    input.setAttribute('aria-invalid', String(own));
    if (own && rocket.inputs.some((r) => r.input === input)) rocketBad = true;
  }
  if (rocketBad) role(form, 'vehicle-details').open = true;
}

function feasibilityRows(part, est) {
  if (part === 'launch') {
    return [
      ['Liftoff mass', `${fmt(est.liftoff_mass)} kg`],
      ['Liftoff thrust-to-weight', fmt(est.liftoff_tw, 2)],
      ['Δv needed (incl. ~1.7 km/s of losses)', `${fmt(est.dv_needed / 1000, 2)} km/s`],
      ['Δv available (ideal, all stages)', `${fmt(est.dv_available / 1000, 2)} km/s`],
      ...est.stages.map((st) => [`· ${st.label}`, `${fmt(st.dv / 1000, 2)} km/s`]),
      ['Margin', `${est.margin >= 0 ? '+' : ''}${fmt(est.margin)} m/s`],
      ['Estimated max payload for this orbit', `≈ ${fmt(est.max_payload)} kg`],
    ];
  }
  const rows = [[part === 'landing' ? 'Lander mass' : 'Vehicle mass', `${fmt(est.mass)} kg`]];
  if (part === 'landing') rows.push(['Stays in orbit', `${fmt(est.orbiter_mass)} kg`]);
  rows.push(['Engines', `${fmt(est.thrust / 1000, 1)} kN, ${fmt(est.engine_mass)} kg, Isp ${fmt(est.isp)} s`]);
  rows.push(['Starting acceleration', `${fmt(est.accel, 2)} m/s²`
    + (est.lunar_tw ? ` (thrust-to-weight ${fmt(est.lunar_tw, 2)} on the Moon)` : '')]);
  for (const b of est.burns) {
    rows.push([`· ${b.label}`, `${fmt(b.dv)} m/s, ${fmt(b.propellant)} kg, ${fmt(b.burn_time)} s`]);
  }
  rows.push(['Δv in total', `${fmt(est.dv_total)} m/s`]);
  rows.push(['Propellant left at the end', `≈ ${fmt(est.propellant_left)} kg`]);
  return rows;
}

function renderFeasibility(part, est) {
  const box = role(forms[part], 'feasibility');
  box.className = 'feasibility';
  if (!est) {
    box.replaceChildren();
    return;
  }
  let kind;
  if (part === 'launch') kind = est.margin >= 300 ? 'good' : est.margin >= 0 ? 'tight' : 'bad';
  else kind = est.margin_fraction >= 0.05 ? 'good' : est.propellant_left >= 0 ? 'tight' : 'bad';
  box.classList.add(kind);
  const verdict = {
    good: 'Looks achievable.',
    tight: 'Marginal: very little propellant to spare.',
    bad: part === 'launch' ? 'Probably not enough propellant: reduce the payload or lower the orbit.'
      : 'Probably not enough propellant: add propellant, lighten the payload or use more efficient engines.',
  }[kind];
  const dl = el('dl');
  for (const [k, v] of feasibilityRows(part, est)) {
    dl.append(el('dt', { text: k, class: k.startsWith('·') ? 'sub' : '' }), el('dd', { text: v }));
  }
  const note = part === 'launch' ? (est.note ?? '') : 'A rough estimate from impulsive burns with allowances; '
    + 'the flight itself decides.';
  box.replaceChildren(el('p', { class: 'verdict', text: verdict }), dl, el('p', { class: 'note', text: note }));
}

const validateTimers = {};
const validateSeq = {};
function scheduleValidate(part) {
  clearTimeout(validateTimers[part]);
  validateTimers[part] = setTimeout(async () => {
    const seq = (validateSeq[part] = (validateSeq[part] ?? 0) + 1);
    try {
      const reply = await postJSON('/api/validate', formValues(part));
      if (seq !== validateSeq[part]) return;
      tabState[part] = { errors: reply.errors ?? [], estimate: reply.estimate, phases: reply.phases ?? [] };
      renderErrors(part, tabState[part].errors);
      renderFeasibility(part, reply.estimate);
      updateLaunchButtons();
      if (!lastSnap || lastSnap.state === 'idle') renderPhases(idlePhases());
    } catch {
      renderErrors(part, ['Mission server unreachable.']);
    }
  }, 200);
}

function changed(form) {
  const part = form.dataset.part;
  if (part === 'launch') updateRocketDerived();
  else pickers[part].update();
  if (part === 'landing') updateSplit();
  saveForms();
  scheduleValidate(part);
}

function updateLaunchButtons() {
  for (const p of PARTS) role(forms[p], 'launch').disabled = running || tabState[p].errors.length > 0;
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

function showTab(part) {
  activePart = part;
  for (const b of document.querySelectorAll('[role="tab"]')) {
    const on = b.dataset.part === part;
    b.setAttribute('aria-selected', String(on));
    b.tabIndex = on ? 0 : -1;
  }
  for (const p of PARTS) forms[p].classList.toggle('hidden', p !== part);
  if (!lastSnap || lastSnap.state === 'idle') renderPhases(idlePhases());
  saveForms();
}

function idlePhases() {
  return (tabState[activePart].phases ?? []).map((p) => ({ ...p, state: 'pending' }));
}

const tabButtons = [...document.querySelectorAll('[role="tab"]')];
tabButtons.forEach((b, i) => {
  b.addEventListener('click', () => showTab(b.dataset.part));
  b.addEventListener('keydown', (e) => {
    const d = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
    if (!d) return;
    const next = tabButtons[(i + d + tabButtons.length) % tabButtons.length];
    next.focus();
    showTab(next.dataset.part);
  });
});

// ---------------------------------------------------------------------------
// Launch / abort
// ---------------------------------------------------------------------------

for (const part of PARTS) {
  const form = forms[part];
  form.addEventListener('input', () => changed(form));
  form.addEventListener('change', () => changed(form));
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (running) return;
    const values = formValues(part);
    if (values.reset === false && !confirm('Fly without resetting the simulation? Other craft are left alone, '
      + 'but none may already use this flight\'s IDs.')) return;
    role(form, 'launch').disabled = true;
    try {
      const reply = await postJSON('/api/start', values);
      if (!reply.ok) renderErrors(part, reply.errors ?? ['Launch rejected.']);
    } catch {
      renderErrors(part, ['Mission server unreachable.']);
    }
    poll(true);
  });
}

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

function setPill(e, text, kind) {
  e.textContent = text;
  e.className = `pill pill-${kind}`;
}

function renderPhases(phases) {
  ui.phases.replaceChildren(...phases.map((p) => el('li', { class: p.state, text: p.title })));
}

function missionStart(snap) {
  if (snap.part === 'launch') {
    const e = snap.log.find((x) => x.text.startsWith('Ignition and liftoff'));
    return e ? e.t : null;
  }
  return snap.log.length ? snap.log[0].t : null;
}

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
  const alt = (x) => `${fmt(x, Math.abs(x) < 1000 ? 1 : 0)} km`;
  let cells;
  if (tel.body) {
    const near = tel.body;
    cells = [
      ['Mission time', t0 !== null ? fmtT(tel.t - t0) : '—'],
      [`Altitude above the ${near}`, alt(tel.altitude_km)],
      [`Speed (relative to the ${near})`, `${fmt(tel.speed)} m/s`],
      ['Vertical speed', `${fmt(tel.vertical_speed, 1)} m/s`],
      [near === 'Moon' ? 'Perilune' : 'Perigee', alt(tel.periapsis_km)],
      [near === 'Moon' ? 'Apolune' : 'Apogee', tel.apoapsis_km === null ? 'escape' : alt(tel.apoapsis_km)],
      ['Inclination', `${fmt(tel.inclination, 2)}°`],
      ['Mass', `${fmt(tel.mass)} kg`],
      ['Propellant', propellantText(tel)],
      ['Thrust', `${fmt(tel.thrust_kn, 1)} kN`],
      ['Acceleration from thrust', `${fmt(tel.accel_g, 3)} g`],
    ];
  } else {
    cells = [
      ['Mission time', t0 !== null ? fmtT(tel.t - t0) : '—'],
      ['Altitude', alt(tel.altitude_km)],
      ['Speed (inertial)', `${fmt(tel.speed)} m/s`],
      ['Vertical speed', `${fmt(tel.vertical_speed)} m/s`],
      ['Perigee', alt(tel.perigee_km)],
      ['Apogee', tel.apogee_km === null ? 'escape' : alt(tel.apogee_km)],
      ['Inclination', `${fmt(tel.inclination, 2)}°`],
      ['Mass', `${fmt(tel.mass)} kg`],
      ['Propellant', propellantText(tel)],
      ['Thrust', `${fmt(tel.thrust_kn)} kN`],
      ['Acceleration', `${fmt(tel.accel_g, 2)} g`],
      ['Dynamic pressure', `${fmt(tel.dynamic_pressure / 1000, 1)} kPa`],
    ];
  }
  ui.telemetry.replaceChildren(...cells.map(([k, v]) => el('div', {}, el('dt', { text: k }), el('dd', { text: v }))));
}

function renderLog(log, t0) {
  const key = log.length ? `${log.length}:${log[log.length - 1].wall}` : '0';
  if (key === lastLogKey) return;
  lastLogKey = key;
  ui.log.replaceChildren(...log.slice().reverse().map((e) => el('li', {},
    el('span', { class: 't', text: t0 !== null ? fmtT(e.t - t0) : `t=${fmt(e.t, 1)} s` }),
    el('span', { class: e.kind, text: e.text }))));
}

function orbitTable(r, names) {
  const table = el('table');
  table.append(el('tr', {}, ...['', 'Target', 'Achieved', 'Error'].map((c) => el('th', { text: c }))));
  const rows = [
    [names[0], r.target[names[3]], r[names[3]], 'km', 1],
    [names[1], r.target[names[4]], r[names[4]], 'km', 1],
    ['Inclination', r.target.inclination_deg, r.inclination_deg, '°', 2],
  ];
  for (const [label, target, got, unit, d] of rows) {
    const err = got === null || got === undefined ? null : got - target;
    table.append(el('tr', {}, el('th', { text: label }), el('td', { text: `${fmt(target, d)} ${unit}` }),
      el('td', { text: got === null ? 'escape' : `${fmt(got, d)} ${unit}` }),
      el('td', { text: err === null ? '—' : `${err >= 0 ? '+' : ''}${fmt(err, d === 1 ? 1 : 3)} ${unit}` })));
  }
  return table;
}

function deliveredBox(title, rows) {
  const dl = el('dl', { class: 'delivered' });
  for (const [k, v] of rows) dl.append(el('dt', { text: k }), el('dd', { text: v }));
  return el('div', { class: 'delivered-box' }, el('h4', { text: title }), dl);
}

// "Use in tab N": copy what a finished part delivered into another tab's starting point.
function handOff(fromPart, toPart, d, add) {
  const form = forms[toPart];
  const pay = d.payload_kg ?? 0;
  const prop = d.propellant_kg ?? 0;
  const cur = formValues(toPart);
  setField(form, 'payload_kg', Math.round((add ? cur.payload_kg ?? 0 : 0) + pay));
  setField(form, 'propellant_kg', Math.round((add ? cur.propellant_kg ?? 0 : 0) + prop));
  if (d.perigee_km !== undefined) {
    setField(form, 'start_periapsis_km', Math.round(d.perigee_km));
    setField(form, 'start_apoapsis_km', Math.round(d.apogee_km));
    setField(form, 'start_inclination_deg', Number(d.inclination_deg.toFixed(2)));
  } else if (d.perilune_km !== undefined) {
    setField(form, 'start_periapsis_km', Math.round(d.perilune_km));
    setField(form, 'start_apoapsis_km', Math.round(d.apolune_km));
    setField(form, 'start_inclination_deg', Number(d.inclination_deg.toFixed(2)));
  }
  showTab(toPart);
  changed(form);
  forms[toPart].scrollIntoView({ behavior: 'smooth', block: 'start' });
}

const NEXT = {
  launch: [['transfer', 'Use as tab 2\'s vehicle', false], ['transfer', 'Add to tab 2\'s vehicle', true]],
  transfer: [['landing', 'Use in tab 3', false], ['return', 'Use in tab 4', false]],
  landing: [['return', 'Use in tab 4', false]],
  return: [['transfer', 'Use as tab 2\'s vehicle', false]],
};

function renderResult(snap) {
  const done = ['succeeded', 'failed', 'aborted'].includes(snap.state);
  ui.result.classList.toggle('hidden', !done);
  if (!done) {
    lastResultKey = '';
    return;
  }
  const key = JSON.stringify([snap.state, snap.message, snap.result, snap.log.length]);
  if (key === lastResultKey) return;
  lastResultKey = key;
  ui.result.className = `result ${snap.state === 'succeeded' ? 'success' : 'failure'}`;
  const parts = [el('h3', { text: snap.message ?? snap.state })];
  const r = snap.result;
  const d = r?.delivered;
  if (snap.part === 'launch' && r) {
    if (d) {
      parts.push(deliveredBox('Delivered to Earth orbit', [
        ['Payload', `${fmt(d.payload_kg)} kg`], ['Propellant (in the upper stage)', `${fmt(d.propellant_kg)} kg`]]));
    }
    if (r.target) parts.push(orbitTable(r, ['Perigee', 'Apogee', null, 'perigee_km', 'apogee_km']));
  } else if (r) {
    if (d && snap.part === 'transfer') {
      parts.push(deliveredBox('In lunar orbit', [['Payload', `${fmt(d.payload_kg)} kg`],
        ['Propellant remaining', `${fmt(d.propellant_kg)} kg`]]));
    } else if (d && snap.part === 'landing') {
      parts.push(deliveredBox('In lunar orbit afterwards', [
        ['Payload', `${fmt(d.payload_kg)} kg`], ['Propellant', `${fmt(d.propellant_kg)} kg`],
        ['· lander', `${fmt(d.lander_payload_kg)} kg + ${fmt(d.lander_propellant_kg)} kg propellant`],
        ['· stayed in orbit', `${fmt(d.orbiter_payload_kg)} kg + ${fmt(d.orbiter_propellant_kg)} kg propellant`]]));
      parts.push(deliveredBox('On the surface', [
        ['Payload', `${fmt(r.surface.payload_kg)} kg`], ['Propellant', `${fmt(r.surface.propellant_kg)} kg`],
        ['Landing', `${fmt(r.landing.site_error_km, 2)} km from the site, ${fmt(r.landing.vertical, 2)} m/s down`],
        ['Time on the surface', `${fmt(r.landing.surface_hours, 1)} h`]]));
    } else if (d && snap.part === 'return') {
      parts.push(deliveredBox('In Earth orbit', [['Payload', `${fmt(d.payload_kg)} kg`],
        ['Propellant remaining', `${fmt(d.propellant_kg)} kg`]]));
    }
    if (r.orbit) {
      const moon = r.orbit.body === 'luna';
      parts.push(orbitTable(r.orbit, [moon ? 'Perilune' : 'Perigee', moon ? 'Apolune' : 'Apogee', null,
        'periapsis_km', 'apoapsis_km']));
    }
    if (r.burns?.length) {
      const dl = el('dl', { class: 'burns' });
      for (const b of r.burns) dl.append(el('dt', { text: b.label }), el('dd', { text: `${fmt(b.dv, 1)} m/s · ${fmt(b.propellant)} kg` }));
      parts.push(el('h4', { text: 'Burns' }), dl);
    }
    if (!d && r.propellant_kg !== undefined) {
      parts.push(el('p', { class: 'muted small', text: `${r.craft}: ${fmt(r.mass_kg)} kg, ${fmt(r.propellant_kg)} kg propellant.` }));
    }
  }
  const actions = el('div', { class: 'actions wrap' });
  if (snap.state === 'succeeded' && d) {
    for (const [to, label, add] of NEXT[snap.part] ?? []) {
      actions.append(el('button', { type: 'button', text: label, onclick: () => handOff(snap.part, to, d, add) }));
    }
  }
  actions.append(el('a', { class: 'button', href: '/api/mission/commands.txt', download: '', text: 'Download command log',
    title: 'Every command the autopilot sent to the simulation, with times, as a text file' }));
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
    lastSnap = snap;
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
    ui.flightPart.textContent = snap.part ? `· ${config.parts?.[snap.part] ?? snap.part}` : '';
    for (const fs of document.querySelectorAll('.part-form fieldset, .presets button')) fs.disabled = running;
    ui.abort.disabled = !running;
    updateLaunchButtons();
    const t0 = snap.part ? missionStart(snap) : null;
    renderPhases(snap.state === 'idle' ? idlePhases() : snap.phases);
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
  const template = $('pace-template');
  for (const p of PARTS.slice(1)) {
    const block = role(forms[p], 'pace-block');
    block.append(template.content.cloneNode(true));
    for (const r of block.querySelectorAll('input[type="radio"]')) r.name = `pace-${p}`;
    pickers[p] = makeEnginePicker(forms[p]);
  }
  for (const p of PARTS) renderPresets(forms[p]);
  renderSites();
  const saved = loadForms();
  rocket.key = saved?.launch?.vehicle && config.vehicles?.[saved.launch.vehicle]
    ? saved.launch.vehicle : config.default_vehicle ?? Object.keys(config.vehicles ?? {})[0];
  renderRockets();
  renderStages(saved?.launch?.vehicle_config);
  role(forms.launch, 'vehicle-reset').addEventListener('click', () => {
    renderStages();
    changed(forms.launch);
  });
  for (const p of PARTS) restoreForm(p, saved?.[p]);
  updateSplit();
  showTab(saved?.active && PARTS.includes(saved.active) ? saved.active : 'launch');
  installGlossary();
  for (const p of PARTS) scheduleValidate(p);
  updateViewerLink(null);
  poll();
})();
