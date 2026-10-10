// 3D viewer for nbody_sim.  Polls /api/state and renders the current state.
//
// Rendering notes
// * Scene units are kilometres.  A floating origin keeps the focused object at
//   (0,0,0) so float32 GPU precision is fine from AU scales down to metres.
// * Bodies are drawn as ray-traced sphere impostors (exact surface, no
//   tessellation error), writing logarithmic depth to match three.js.
// * Everything is true scale; HTML markers keep small objects findable.

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/OrbitControls.js';

const KM = 1000;
const AU = 1.495978707e11;
const POLL_MS = 500;
const NEAR = 1e-5;          // km
const FAR = 1e12;           // km
const LOG_DEPTH_FC = 2.0 / Math.log2(FAR + 1.0);
const MIN_ALT_KM = 0.002;   // closest approach of the camera to a surface
const MAX_DIST_KM = 5e10;
const J2000_UTC_MS = Date.UTC(2000, 0, 1, 11, 58, 55, 816); // 2000-01-01 12:00 TT

const BODY_COLORS = {
  sun: '#ffd27a', mercury: '#9c9690', venus: '#e6d3a3', earth: '#3f7fd9', luna: '#b9b6b0',
  mars: '#c1440e', jupiter: '#d8b48a', saturn: '#e3cf98', uranus: '#9fdde4', neptune: '#4f74e3',
};
const CRAFT_COLOR = '#ff9f43';

function colorFor(id) {
  if (BODY_COLORS[id]) return BODY_COLORS[id];
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360}, 45%, 60%)`;
}

function rgbVector(css) {
  const c = document.createElement('canvas').getContext('2d');
  c.fillStyle = css;
  const hex = c.fillStyle; // normalised to #rrggbb
  return new THREE.Vector3(
    parseInt(hex.slice(1, 3), 16) / 255,
    parseInt(hex.slice(3, 5), 16) / 255,
    parseInt(hex.slice(5, 7), 16) / 255,
  );
}

// ---------------------------------------------------------------------------
// Renderer, scene, camera
// ---------------------------------------------------------------------------

const canvas = document.getElementById('scene');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, logarithmicDepthBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x02030a);

const camera = new THREE.PerspectiveCamera(50, 1, NEAR, FAR);
camera.up.set(0, 0, 1);
let controls = null;

scene.add(new THREE.AmbientLight(0xffffff, 0.15));
const sunLight = new THREE.PointLight(0xffffff, 2.5, 0, 0);
scene.add(sunLight);

// Background stars: fixed directions, kept centred on the camera.
const stars = (() => {
  const n = 2500;
  const pos = new Float32Array(n * 3);
  for (let i = 0; i < n; i++) {
    const z = Math.random() * 2 - 1;
    const a = Math.random() * Math.PI * 2;
    const r = Math.sqrt(1 - z * z);
    pos.set([r * Math.cos(a) * 1e10, r * Math.sin(a) * 1e10, z * 1e10], i * 3);
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  const m = new THREE.PointsMaterial({ color: 0x8890a8, size: 1.3, sizeAttenuation: false });
  const p = new THREE.Points(g, m);
  p.frustumCulled = false;
  scene.add(p);
  return p;
})();

// ---------------------------------------------------------------------------
// Sphere impostor material
// ---------------------------------------------------------------------------

const BODY_VERT = /* glsl */ `
varying vec3 vViewPos;
void main() {
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  vViewPos = mv.xyz;
  gl_Position = projectionMatrix * mv;
}`;

const BODY_FRAG = /* glsl */ `
uniform vec3 uCenter;      // sphere centre, view space (km)
uniform float uRadius;     // km
uniform float uCamAlt;     // camera distance to the surface (km), computed in float64
uniform vec3 uSunDir;      // view-space unit vector from the body toward the Sun
uniform vec3 uColor;
uniform mat3 uViewToBody;  // view space -> body-fixed (x: prime meridian, z: pole)
uniform float uEmissive;
uniform float uGrid;
uniform float uLogDepthFC;
varying vec3 vViewPos;

float gridLevel(float lon, float lon2, float lat, float spacing, float lonFade) {
  vec2 g = vec2(lon, lat) / spacing;
  vec2 w = vec2(min(fwidth(lon), fwidth(lon2)), fwidth(lat)) / spacing;
  vec2 f = abs(fract(g - 0.5) - 0.5) / max(w, vec2(1e-7));
  float lines = max((1.0 - min(f.x, 1.0)) * lonFade, 1.0 - min(f.y, 1.0));
  return lines * (1.0 - smoothstep(0.06, 0.25, w.y));
}

void main() {
  vec3 dir = normalize(vViewPos);
  float b = dot(dir, uCenter);
  float c = uCamAlt * (uCamAlt + 2.0 * uRadius);  // |C|^2 - R^2 without cancellation
  float disc = b * b - c;
  if (disc < 0.0 || c <= 0.0) discard;
  float t = b - sqrt(disc);
  if (t <= 0.0) discard;
  vec3 hit = dir * t;
  vec3 n = normalize(hit - uCenter);

  vec3 col;
  if (uEmissive > 0.5) {
    col = uColor * (0.55 + 0.45 * max(dot(n, -dir), 0.0));
  } else {
    float diff = max(dot(n, uSunDir), 0.0);
    col = uColor * (0.10 + 0.90 * diff);
    if (uGrid > 0.0) {
      vec3 nb = uViewToBody * n;
      float lat = asin(clamp(nb.z, -1.0, 1.0));
      float lon = atan(nb.y, nb.x);
      float lon2 = atan(-nb.y, -nb.x);
      float lonFade = 1.0 - smoothstep(0.85, 0.97, abs(nb.z));
      float g = 0.0;
      g = max(g, 0.45 * gridLevel(lon, lon2, lat, radians(30.0), lonFade));
      g = max(g, 0.30 * gridLevel(lon, lon2, lat, radians(5.0), lonFade));
      g = max(g, 0.25 * gridLevel(lon, lon2, lat, radians(1.0), lonFade));
      g = max(g, 0.20 * gridLevel(lon, lon2, lat, radians(0.1), lonFade));
      float key = max(gridLevel(lon, lon2, lat, radians(360.0), lonFade),
                      gridLevel(lon, lon2, lat, radians(180.0), 0.0));
      float light = 0.35 + 0.65 * diff;
      col = mix(col, vec3(1.0) * light, g * uGrid);
      col = mix(col, vec3(1.0, 0.75, 0.35) * light, 0.7 * key * uGrid);
    }
  }
  gl_FragColor = vec4(col, 1.0);
  gl_FragDepth = log2(1.0 - hit.z) * uLogDepthFC * 0.5;
}`;

const boxGeometry = new THREE.BoxGeometry(2, 2, 2);

function makeBodyMesh(color, emissive) {
  const mat = new THREE.ShaderMaterial({
    vertexShader: BODY_VERT,
    fragmentShader: BODY_FRAG,
    side: THREE.BackSide,
    uniforms: {
      uCenter: { value: new THREE.Vector3() },
      uRadius: { value: 1 },
      uCamAlt: { value: 1 },
      uSunDir: { value: new THREE.Vector3(1, 0, 0) },
      uColor: { value: rgbVector(color) },
      uViewToBody: { value: new THREE.Matrix3() },
      uEmissive: { value: emissive ? 1 : 0 },
      uGrid: { value: emissive ? 0 : 1 },
      uLogDepthFC: { value: LOG_DEPTH_FC },
    },
  });
  const mesh = new THREE.Mesh(boxGeometry, mat);
  mesh.frustumCulled = false;
  return mesh;
}

const craftGeometry = new THREE.SphereGeometry(1, 20, 14);

// ---------------------------------------------------------------------------
// Object registry
// ---------------------------------------------------------------------------

const objects = new Map();   // id -> record
let latest = null;           // latest simulation state
let focusId = null;
let firstState = true;

const labelLayer = document.getElementById('labels');
const ui = {
  status: document.getElementById('status'),
  time: document.getElementById('sim-time'),
  date: document.getElementById('sim-date'),
  focus: document.getElementById('focus-name'),
  range: document.getElementById('cam-range'),
  bodyList: document.getElementById('body-list'),
  craftList: document.getElementById('craft-list'),
  details: document.getElementById('details'),
  detailsTitle: document.getElementById('details-title'),
  panel: document.getElementById('panel'),
  panelToggle: document.getElementById('panel-toggle'),
};

function makeLabel(id, kind, color) {
  const el = document.createElement('div');
  el.className = `label ${kind === 'craft' ? 'craft' : ''}`;
  const dot = document.createElement('span');
  dot.className = 'dot';
  dot.style.background = color;
  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = id;
  el.append(dot, name);
  el.addEventListener('click', () => setFocus(id));
  labelLayer.appendChild(el);
  return el;
}

function makeListItem(list, id, color) {
  const li = document.createElement('li');
  const btn = document.createElement('button');
  const sw = document.createElement('span');
  sw.className = 'swatch';
  sw.style.background = color;
  btn.append(sw, document.createTextNode(id));
  btn.addEventListener('click', () => setFocus(id));
  li.appendChild(btn);
  list.appendChild(li);
  return li;
}

function bodyFrame(pole, primeMeridianDeg) {
  const p = new THREE.Vector3(...pole).normalize();
  let ref = new THREE.Vector3().crossVectors(p, new THREE.Vector3(0, 0, 1));
  if (ref.lengthSq() < 1e-24) ref = new THREE.Vector3().crossVectors(p, new THREE.Vector3(0, 1, 0));
  ref.normalize();
  const refY = new THREE.Vector3().crossVectors(p, ref);
  const w = THREE.MathUtils.degToRad(primeMeridianDeg);
  const e1 = ref.clone().multiplyScalar(Math.cos(w)).addScaledVector(refY, Math.sin(w));
  const e2 = new THREE.Vector3().crossVectors(p, e1);
  return new THREE.Matrix3().set(e1.x, e1.y, e1.z, e2.x, e2.y, e2.z, p.x, p.y, p.z);
}

function syncObjects(state) {
  const seen = new Set();

  for (const b of state.bodies || []) {
    seen.add(b.id);
    let o = objects.get(b.id);
    if (!o) {
      const color = colorFor(b.id);
      o = {
        id: b.id, kind: 'body', color,
        mesh: makeBodyMesh(color, b.id === 'sun'),
        label: makeLabel(b.id, 'body', color),
        li: makeListItem(ui.bodyList, b.id, color),
        rel: new THREE.Vector3(),
      };
      scene.add(o.mesh);
      objects.set(b.id, o);
    }
    o.data = b;
    o.abs = b.position;
    o.radiusKm = b.radius / KM;
    o.pole = new THREE.Vector3(...b.pole);
    o.bodyFromWorld = bodyFrame(b.pole, b.prime_meridian);
    o.mesh.scale.setScalar(Math.max(o.radiusKm, 1e-6) * 1.02);
    o.mesh.material.uniforms.uRadius.value = o.radiusKm;
  }

  for (const s of state.spacecraft || []) {
    seen.add(s.id);
    let o = objects.get(s.id);
    if (!o) {
      // Partly self-lit so craft stay visible on a body's night side.
      const mat = new THREE.MeshStandardMaterial({ color: 0xf2f2f2, emissive: 0x6a4a28, roughness: 0.6 });
      o = {
        id: s.id, kind: 'craft', color: CRAFT_COLOR,
        mesh: new THREE.Mesh(craftGeometry, mat),
        label: makeLabel(s.id, 'craft', CRAFT_COLOR),
        li: makeListItem(ui.craftList, s.id, CRAFT_COLOR),
        rel: new THREE.Vector3(),
      };
      scene.add(o.mesh);
      objects.set(s.id, o);
    }
    o.data = s;
    o.abs = s.position;
    // True-scale sphere with the craft's reference diameter (at least 1 m).
    const radiusM = Math.max(Math.sqrt((s.reference_area || 0) / Math.PI), 0.5);
    o.radiusKm = 0;
    o.mesh.scale.setScalar(radiusM / KM);
  }

  for (const [id, o] of objects) {
    if (seen.has(id)) continue;
    scene.remove(o.mesh);
    if (o.kind === 'body') o.mesh.material.dispose();
    o.label.remove();
    o.li.remove();
    objects.delete(id);
  }

  ui.craftList.querySelector('.empty')?.remove();
  if (!(state.spacecraft || []).length) {
    const li = document.createElement('li');
    li.className = 'empty';
    li.textContent = 'none';
    ui.craftList.appendChild(li);
  }
}

// Positions relative to the focused object (floating origin), in km.
function updateRelativePositions() {
  const f = objects.get(focusId);
  if (!f) return;
  const [ox, oy, oz] = f.abs;
  for (const o of objects.values()) {
    o.rel.set((o.abs[0] - ox) / KM, (o.abs[1] - oy) / KM, (o.abs[2] - oz) / KM);
    o.mesh.position.copy(o.rel);
  }
  const sun = objects.get('sun');
  if (sun) sunLight.position.copy(sun.rel);
}

// ---------------------------------------------------------------------------
// Focus and camera
// ---------------------------------------------------------------------------

function makeControls() {
  if (controls) controls.dispose();
  controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, 0, 0);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;
  controls.enablePan = false;
  controls.enableZoom = false;   // custom altitude-proportional zoom below
  controls.rotateSpeed = 0.6;
  controls.update();
}

function sunDirectionFrom(o) {
  const sun = objects.get('sun');
  if (!sun || sun === o) return new THREE.Vector3(1, 0, 0);
  return sun.rel.clone().sub(o.rel).normalize();
}

function setFocus(id, distanceKm) {
  const o = objects.get(id);
  if (!o) return;
  focusId = id;
  updateRelativePositions();

  let up = new THREE.Vector3(0, 0, 1);
  let dir;
  let dist;
  if (o.kind === 'body') {
    if (id !== 'sun') up = o.pole.clone();
    // Look at the lit hemisphere from slightly "above" the equator.
    dir = id === 'sun' ? new THREE.Vector3(1, -1, 0.6) : sunDirectionFrom(o).addScaledVector(up, 0.35);
    dist = distanceKm ?? o.radiusKm * 4;
  } else {
    const nb = objects.get(o.data.nearest_body.id);
    up = o.rel.clone().sub(nb.rel).normalize();
    let east = new THREE.Vector3().crossVectors(nb.pole, up);
    if (east.lengthSq() < 1e-12) east = new THREE.Vector3(1, 0, 0);
    east.normalize();
    dir = east.multiplyScalar(-1).addScaledVector(up, 0.35);
    dist = distanceKm ?? 0.25;
  }
  camera.up.copy(up);
  camera.position.copy(dir.normalize().multiplyScalar(dist));
  makeControls();
  for (const r of objects.values()) {
    r.label.classList.toggle('focus', r.id === focusId);
    r.li.querySelector('button').classList.toggle('active', r.id === focusId);
  }
  ui.focus.textContent = id;
  updateDetails();
}

function focusRadiusKm() {
  return objects.get(focusId)?.radiusKm ?? 0;
}

function zoomBy(factor) {
  const r = focusRadiusKm();
  const d = camera.position.length();
  const alt = Math.min(Math.max((d - r) * factor, MIN_ALT_KM), MAX_DIST_KM);
  camera.position.setLength(r + alt);
}

canvas.addEventListener('wheel', (e) => {
  e.preventDefault();
  const dy = e.deltaMode === 1 ? e.deltaY * 30 : e.deltaY;
  zoomBy(Math.exp(dy * 0.0015));
}, { passive: false });

window.addEventListener('keydown', (e) => {
  if (e.target instanceof HTMLInputElement) return;
  if (e.key === '+' || e.key === '=') zoomBy(0.8);
  else if (e.key === '-' || e.key === '_') zoomBy(1.25);
  else if (e.key === 'f' || e.key === 'F') setFocus(focusId);
});

ui.panelToggle.addEventListener('click', () => {
  const collapsed = ui.panel.classList.toggle('collapsed');
  ui.panelToggle.setAttribute('aria-expanded', String(!collapsed));
  ui.panelToggle.textContent = collapsed ? 'Objects ▸' : 'Objects ▾';
});

// ---------------------------------------------------------------------------
// Formatting and panel
// ---------------------------------------------------------------------------

function fmtLen(m) {
  const a = Math.abs(m);
  if (a < 1e4) return `${m.toFixed(a < 10 ? 2 : 1)} m`;
  if (a < 1e10) return `${(m / KM).toLocaleString(undefined, { maximumFractionDigits: a < 1e7 ? 2 : 0 })} km`;
  return `${(m / AU).toFixed(4)} AU`;
}
const fmtSpeed = (v) => (Math.abs(v) < 1e4 ? `${v.toFixed(2)} m/s` : `${(v / KM).toFixed(3)} km/s`);
const fmtSci = (x, unit) => `${Math.abs(x) >= 1e6 || (Math.abs(x) < 1e-2 && x !== 0) ? x.toExponential(3) : x.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${unit}`;
const norm = (v) => Math.hypot(v[0], v[1], v[2]);
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];

function fmtElapsed(t) {
  const sign = t < 0 ? '-' : '';
  t = Math.abs(t);
  const d = Math.floor(t / 86400);
  const h = Math.floor((t % 86400) / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const hms = `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${s.toFixed(2).padStart(5, '0')}`;
  return `${sign}${d ? `${d}d ` : ''}${hms}`;
}

function updateDetails() {
  const o = objects.get(focusId);
  if (!o) return;
  const rows = [];
  if (o.kind === 'body') {
    const b = o.data;
    const sun = objects.get('sun');
    rows.push(['Radius', fmtLen(b.radius)], ['Mass', fmtSci(b.mass, 'kg')]);
    if (sun && sun !== o) {
      rows.push(['Sun distance', fmtLen(norm(sub(b.position, sun.data.position)))]);
      rows.push(['Speed (Sun)', fmtSpeed(norm(sub(b.velocity, sun.data.velocity)))]);
    }
    if (b.rotation_period) rows.push(['Rotation', `${(b.rotation_period / 3600).toFixed(2)} h`]);
    rows.push(['Prime meridian', `${b.prime_meridian.toFixed(2)}°`]);
    rows.push(['Atmosphere', b.has_atmosphere ? 'yes' : 'no']);
  } else {
    const s = o.data;
    const nb = s.nearest_body;
    const enu = nb.surface_relative_velocity_enu;
    const thrust = norm(s.thrust.force);
    rows.push(
      ['Mass', fmtSci(s.mass, 'kg')],
      ['Nearest body', nb.id],
      ['Altitude', fmtLen(nb.altitude)],
      ['Lat / Lon', `${nb.latitude.toFixed(4)}°, ${nb.longitude.toFixed(4)}°`],
      ['Surface speed', fmtSpeed(nb.airspeed)],
      ['Vertical speed', fmtSpeed(enu[2])],
      ['Horizontal speed', fmtSpeed(Math.hypot(enu[0], enu[1]))],
      ['In contact', nb.in_contact ? 'yes' : 'no'],
      ['Thrust', s.thrust.active ? `${fmtSci(thrust, 'N')} (${s.thrust.frame})` : 'off'],
      ['Mass rate', `${s.thrust.mass_rate.toFixed(2)} kg/s`],
      ['Accel (total)', `${(norm(s.acceleration.total) / 9.80665).toFixed(3)} g`],
      ['Dyn. pressure', fmtSci(nb.dynamic_pressure, 'Pa')],
      ['Density', fmtSci(nb.density, 'kg/m³')],
    );
    if (nb.drag_coefficient !== null) rows.push(['Cd', nb.drag_coefficient.toFixed(3)]);
  }
  ui.detailsTitle.textContent = `Details — ${o.id}`;
  ui.details.replaceChildren(...rows.flatMap(([k, v]) => {
    const dt = document.createElement('dt');
    dt.textContent = k;
    const dd = document.createElement('dd');
    dd.textContent = v;
    return [dt, dd];
  }));
}

function setStatus(kind, text) {
  ui.status.className = `status status-${kind}`;
  ui.status.textContent = text;
}

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------

async function poll() {
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    const doc = await res.json();
    if (doc.state) applyState(doc.state);
    if (doc.connected) setStatus('ok', 'Live');
    else setStatus(doc.state ? 'bad' : 'wait', doc.state ? 'Sim unreachable — last state' : 'Waiting for simulation');
  } catch (err) {
    setStatus('bad', 'Viz server unreachable');
  }
  setTimeout(poll, POLL_MS);
}

function applyState(state) {
  latest = state;
  const prev = objects.get(focusId);
  const fallback = prev?.kind === 'craft' ? prev.data.nearest_body.id : 'sun';
  syncObjects(state);
  if (!objects.has(focusId)) {
    // First state, or the focused craft was removed (e.g. staged).
    focusId = null;
    if (firstState) {
      firstState = false;
      const params = new URLSearchParams(location.search);
      const wanted = params.get('focus');
      const dist = params.has('dist') ? Number(params.get('dist')) : undefined;
      if (wanted && objects.has(wanted)) setFocus(wanted, dist);
      else if (objects.has('sun')) setFocus('sun', dist ?? 4 * AU / KM);
      else setFocus(objects.keys().next().value, dist);
    } else {
      setFocus(objects.has(fallback) ? fallback : objects.keys().next().value);
    }
  }
  updateRelativePositions();
  ui.time.textContent = `t = ${fmtElapsed(state.time)}`;
  ui.date.textContent = `${new Date(J2000_UTC_MS + state.time * 1000).toISOString().replace('T', ' ').slice(0, 19)} UTC (approx.)`;
  updateDetails();
}

// ---------------------------------------------------------------------------
// Per-frame updates
// ---------------------------------------------------------------------------

const tmpV = new THREE.Vector3();
const worldFromView = new THREE.Matrix3();

function updateBodyUniforms() {
  worldFromView.setFromMatrix4(camera.matrixWorld);
  for (const o of objects.values()) {
    if (o.kind !== 'body') continue;
    const u = o.mesh.material.uniforms;
    u.uCenter.value.copy(o.rel).applyMatrix4(camera.matrixWorldInverse);
    u.uCamAlt.value = o.rel.distanceTo(camera.position) - o.radiusKm;
    u.uSunDir.value.copy(sunDirectionFrom(o)).transformDirection(camera.matrixWorldInverse);
    u.uViewToBody.value.copy(o.bodyFromWorld).multiply(worldFromView);
  }
}

const labelOrder = (a, b) => {
  const rank = (o) => (o.id === focusId ? 0 : o.kind === 'body' ? 1 : 2);
  return rank(a) - rank(b) || (b.data.mass ?? 0) - (a.data.mass ?? 0);
};

function updateLabels() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  const focalPx = (h / 2) / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
  const bodies = [...objects.values()].filter((o) => o.kind === 'body');
  const bodyView = new Map(bodies.map((b) => [b, b.rel.clone().applyMatrix4(camera.matrixWorldInverse)]));
  const placed = [];

  for (const o of [...objects.values()].sort(labelOrder)) {
    const p = bodyView.get(o) ?? tmpV.copy(o.rel).applyMatrix4(camera.matrixWorldInverse).clone();
    const dist = p.length();
    let visible = p.z < 0;
    if (visible) {
      // Hidden behind another body?  (Craft get a tolerance: they rest just below the surface.)
      const tol = o.kind === 'craft' ? 0.5 : 0;
      for (const b of bodies) {
        if (b === o) continue;
        const c = bodyView.get(b);
        const tca = c.dot(p) / dist;
        const d2 = c.lengthSq() - tca * tca;
        const r2 = b.radiusKm * b.radiusKm;
        if (d2 < r2 && tca - Math.sqrt(r2 - d2) > 0 && tca - Math.sqrt(r2 - d2) < dist - tol) {
          visible = false;
          break;
        }
      }
    }
    const appPx = o.kind === 'body' ? (o.radiusKm / dist) * focalPx : 0;
    if (visible && appPx > h * 0.75) visible = false; // body fills the view
    o.label.style.display = visible ? '' : 'none';
    if (!visible) continue;

    const x = w / 2 + (focalPx * p.x) / -p.z;
    const y = h / 2 - (focalPx * p.y) / -p.z;
    o.label.style.transform = `translate(${x}px, ${y - 7}px)`;
    o.label.classList.toggle('no-dot', appPx > 4);
    const crowded = placed.some(([px, py]) => Math.abs(px - x) < 70 && Math.abs(py - y) < 14);
    o.label.classList.toggle('no-name', crowded);
    if (!crowded) placed.push([x, y]);
  }
}

function resize() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  if (canvas.width !== Math.floor(w * renderer.getPixelRatio()) || canvas.height !== Math.floor(h * renderer.getPixelRatio())) {
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
}

function frame() {
  requestAnimationFrame(frame);
  resize();
  if (controls) controls.update();
  camera.updateMatrixWorld();
  camera.matrixWorldInverse.copy(camera.matrixWorld).invert();
  stars.position.copy(camera.position);
  if (latest) {
    updateBodyUniforms();
    updateLabels();
    const r = focusRadiusKm();
    ui.range.textContent = fmtLen((camera.position.length() - r) * KM) + (r ? ' alt' : '');
  }
  renderer.render(scene, camera);
}

poll();
frame();
