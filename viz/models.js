// Simple procedural spacecraft models, built from a craft's `model` tag.
//
// The tag comes from the simulation (set by the scenario or the mission autopilot):
//   { vehicle: 'vulcan', parts: ['boosters', 'core', 'upper', 'fairing', 'payload'], boosters: 4 }
// `parts` lists what the craft is made of; separated pieces carry just their own part
// (e.g. { vehicle: 'vulcan', parts: ['core'] } for the spent first stage).
//
// Models are true size, built in metres with +Y pointing toward the nose and the base of the
// lowest part at the origin (a lone payload is centred instead).  app.js scales them to km
// and turns +Y along the direction of travel.  Dimensions are approximate.

import * as THREE from 'three';

const WHITE = 0xeeeeee;
const BLACK = 0x2a2a2a;
const ENGINE = 0x3a3a3f;
const FOIL = 0xc9a24a;
const PANEL = 0x24365e;

// Per vehicle: diameter d and length l in metres.  Boosters are per booster.
const VEHICLES = {
  falcon9: {
    core: { d: 3.66, l: 42.6, color: WHITE, band: BLACK },
    upper: { d: 3.66, l: 13.8, color: WHITE },
    fairing: { d: 5.2, l: 13.1 },
  },
  atlas_v_551: {
    core: { d: 3.81, l: 32.5, color: 0xd28b52 },
    boosters: { d: 1.58, l: 20.0, color: WHITE, nose: 2.5 },
    upper: { d: 3.05, l: 12.7, color: 0xc4c8cc },
    fairing: { d: 5.4, l: 23.4, coversUpper: true },
  },
  vulcan: {
    core: { d: 5.4, l: 33.3, color: 0xc98a4f },
    boosters: { d: 1.6, l: 22.0, color: WHITE, nose: 2.5 },
    upper: { d: 5.4, l: 11.7, color: 0xd9dcdf },
    fairing: { d: 5.4, l: 15.5 },
  },
  delta_iv_heavy: {
    core: { d: 5.1, l: 40.8, color: 0xd9782e },
    boosters: { d: 5.1, l: 40.8, color: 0xd9782e, nose: 1.4 },
    upper: { d: 4.0, l: 13.7, color: WHITE, flare: 5.1 },
    fairing: { d: 5.1, l: 19.1 },
  },
  new_glenn: {
    core: {
      d: 7.0, l: 57.5, color: WHITE,
      // Aft section flaring out to ~8.2 m at the base, topped by a lavender ring.
      aft: [
        { y0: 0, y1: 10.0, d0: 8.2, d1: 7.9, color: 0x5d6067 },
        { y0: 10.0, y1: 11.4, d0: 7.9, d1: 7.0, color: 0x9a9fe0 },
      ],
      // Two strakes low on the stage, and four fins below the interstage; one pair of fins
      // lines up with the strakes.  Outlines are [out from the skin, height above the base].
      surfaces: [
        { count: 2, angle: 0, thickness: 0.4, color: 0xc9cdd3,
          outline: [[0, 4.6], [2.4, 13.2], [2.4, 19.6], [0, 26.4]] },
        { count: 4, angle: 0, thickness: 0.8, color: 0xc9cdd3,
          outline: [[0, 50.7], [1.9, 52.6], [1.9, 54.0], [0, 56.4]] },
      ],
    },
    upper: { d: 7.0, l: 23.4, color: WHITE },
    fairing: { d: 7.0, l: 21.9 },
  },
  sls: {
    core: { d: 8.4, l: 64.6, color: 0xc8692b },
    boosters: { d: 3.71, l: 54.0, color: WHITE, nose: 1.8 },
    upper: { d: 5.0, l: 13.7, color: WHITE, flare: 8.4 },
    fairing: null,
  },
};

function material(color) {
  const c = new THREE.Color(color);
  // A little self-illumination keeps craft visible on a body's night side.
  return new THREE.MeshStandardMaterial({
    color: c, emissive: c.clone().multiplyScalar(0.12), roughness: 0.6, metalness: 0.15,
  });
}

function cylinder(d, l, color, y0, segments = 24) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(d / 2, d / 2, l, segments), material(color));
  m.position.y = y0 + l / 2;
  return m;
}

function cone(dBase, dTop, l, color, y0, segments = 24) {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(dTop / 2, dBase / 2, l, segments), material(color));
  m.position.y = y0 + l / 2;
  return m;
}

// Ogive nose cone (booster nose): cylinder for `straight` metres, then a tangent ogive.
function ogive(d, l, straight, color, y0) {
  const m = new THREE.Mesh(ogiveGeometry(d, l, straight, 2 * Math.PI), material(color));
  m.position.y = y0;
  return m;
}

function coreStage(v, group, y0) {
  const c = v.core;
  group.add(cylinder(c.d, c.l, c.color, y0));
  if (c.aft) {
    for (const a of c.aft) group.add(cone(a.d0, a.d1, a.y1 - a.y0, a.color, y0 + a.y0));
  } else {
    group.add(cylinder(c.d * 1.01, 1.6, ENGINE, y0));                  // engine section
  }
  if (c.band) group.add(cylinder(c.d * 1.01, c.l * 0.08, c.band, y0 + c.l * 0.92));  // interstage
  for (const surf of c.surfaces ?? []) aeroSurfaces(surf, c.d / 2, group, y0);
  return y0 + c.l;
}

// Flat plates (strakes, fins) spaced evenly around a stage, the first at `angle` degrees.
// `outline` is the planform as [outward from the skin, height above the stage base] points
// in metres, with the root along the skin.
function aeroSurfaces(surf, radius, group, y0) {
  const thickness = surf.thickness ?? 0.3;
  const shape = new THREE.Shape(surf.outline.map(([x, y]) => new THREE.Vector2(x, y)));
  const geometry = new THREE.ExtrudeGeometry(shape, { depth: thickness, bevelEnabled: false });
  geometry.translate(0, 0, -thickness / 2);
  for (let i = 0; i < surf.count; i++) {
    const a = THREE.MathUtils.degToRad(surf.angle) + (i / surf.count) * Math.PI * 2;
    const m = new THREE.Mesh(i === 0 ? geometry : geometry.clone(), material(surf.color));
    m.rotation.y = -a;  // local +X (outward) -> (cos a, 0, sin a)
    m.position.set(Math.cos(a) * (radius - 0.05), y0, Math.sin(a) * (radius - 0.05));
    group.add(m);
  }
}

function boosterRing(v, count, group, y0) {
  const b = v.boosters;
  const offset = v.core.d / 2 + b.d / 2 + 0.1;
  for (let i = 0; i < count; i++) {
    const a = (i / count) * Math.PI * 2 + Math.PI / 2;
    const g = new THREE.Group();
    g.add(cylinder(b.d, b.l - b.nose * b.d, b.color, 0, 16));
    g.add(ogive(b.d, b.nose * b.d, 0, b.color, b.l - b.nose * b.d));
    g.add(cone(b.d * 0.8, b.d * 0.5, 1.2, ENGINE, -1.2, 16));          // nozzle
    g.position.set(Math.cos(a) * offset, y0 + 1.2, Math.sin(a) * offset);
    group.add(g);
  }
}

function upperStage(v, group, y0, nozzle) {
  const u = v.upper;
  let y = y0;
  if (u.flare) {  // adapter from a wider stage below
    group.add(cone(u.flare, u.d, 2.5, u.color, y));
    y += 2.5;
  }
  group.add(cylinder(u.d, u.l - (y - y0), u.color, y));
  if (nozzle) group.add(cone(u.d * 0.55, u.d * 0.2, u.d * 0.6, ENGINE, y0 - u.d * 0.6));
  return y0 + u.l;
}

function fairing(v, group, y0, split) {
  const f = v.fairing;
  const straight = f.l * 0.55;
  const halves = new THREE.Group();
  if (split) {
    // A jettisoned fairing: two half shells drifting apart.
    for (const side of [-1, 1]) {
      const mat = material(WHITE);
      mat.side = THREE.DoubleSide;
      const m = new THREE.Mesh(ogiveGeometry(f.d, f.l, straight, Math.PI), mat);
      m.rotation.y = side > 0 ? 0 : Math.PI;
      m.position.z = side * 1.5;
      halves.add(m);
    }
  } else {
    halves.add(new THREE.Mesh(ogiveGeometry(f.d, f.l, straight, 2 * Math.PI), material(WHITE)));
  }
  halves.position.y = y0;
  group.add(halves);
  return y0 + f.l;
}

// Lathe of a cylinder (`straight` metres) topped by a tangent ogive; `sweep` < 2 pi gives a
// partial shell centred on +Z.
function ogiveGeometry(d, l, straight, sweep) {
  const r = d / 2;
  const h = l - straight;
  const rho = (r * r + h * h) / (2 * r);
  const pts = [new THREE.Vector2(0, 0), new THREE.Vector2(r, 0), new THREE.Vector2(r, straight)];
  for (let i = 1; i <= 12; i++) {
    const x = (h * i) / 12;
    pts.push(new THREE.Vector2(Math.max(0, Math.sqrt(rho * rho - x * x) + r - rho), straight + x));
  }
  return new THREE.LatheGeometry(pts, sweep < 6 ? 16 : 32, -sweep / 2, sweep);
}

function satellite(group, y0) {
  const bus = new THREE.Mesh(new THREE.BoxGeometry(2.4, 3.2, 2.4), material(FOIL));
  bus.position.y = y0 + 1.6;
  group.add(bus);
  for (const side of [-1, 1]) {
    const panel = new THREE.Mesh(new THREE.BoxGeometry(7.0, 0.08, 2.0), material(PANEL));
    panel.position.set(side * (1.2 + 0.6 + 3.5), y0 + 1.6, 0);
    group.add(panel);
    const boom = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 0.6, 6), material(WHITE));
    boom.rotation.z = Math.PI / 2;
    boom.position.set(side * 1.5, y0 + 1.6, 0);
    group.add(boom);
  }
  const dish = new THREE.Mesh(new THREE.CylinderGeometry(0.9, 0.2, 0.4, 20), material(WHITE));
  dish.position.y = y0 + 3.4;
  group.add(dish);
  return y0 + 3.6;
}

// Generic vehicles of the Moon mission page:
//   { vehicle: 'lunar', parts: ['tanks'|'lander'|'engines'|'cargo', ...], engines, engine_thrust, dry, prop }
// A tank body sized from the masses (generic bulk densities), engine bells sized from thrust,
// landing legs for a lander, and a pallet of crates for cargo left on the surface.
function bellSize(thrust) {
  const de = THREE.MathUtils.clamp(0.45 * Math.pow(Math.max(thrust, 1) / 1e4, 0.4), 0.35, 2.5);
  return { de, len: 1.2 * de };
}

function engineBells(group, count, thrust, d, y0) {
  const { de, len } = bellSize(thrust);
  const ring = count === 1 ? 0 : Math.max(de * 0.6, d / 2 - de / 2 - 0.1);
  for (let i = 0; i < count; i++) {
    const a = (i / count) * Math.PI * 2;
    const bell = cone(de, de * 0.35, len, ENGINE, y0 - len, 16);
    bell.position.x = Math.cos(a) * ring;
    bell.position.z = Math.sin(a) * ring;
    group.add(bell);
  }
  return len;
}

function lunarVehicle(tag) {
  const parts = new Set(tag.parts);
  const prop = Math.max(0, tag.prop ?? 0);
  const dry = Math.max(0, tag.dry ?? 0);
  const group = new THREE.Group();
  if (parts.has('cargo')) {  // crates on a pallet
    const n = THREE.MathUtils.clamp(Math.ceil((dry + prop) / 1500), 1, 9);
    const side = Math.ceil(Math.sqrt(n));
    group.add(cylinder(side * 1.6 + 0.4, 0.2, 0x8a8f99, 0, 4));
    for (let i = 0; i < n; i++) {
      const crate = new THREE.Mesh(new THREE.BoxGeometry(1.3, 1.1, 1.3), material(i % 3 ? FOIL : WHITE));
      crate.position.set(((i % side) - (side - 1) / 2) * 1.6, 0.75, (Math.floor(i / side) - (side - 1) / 2) * 1.6);
      group.add(crate);
    }
    return { object: group, length: 1.4 };
  }
  const volume = prop / 800 + dry / 400 + 2;  // m^3
  const d = THREE.MathUtils.clamp(Math.cbrt(volume / 1.2), 2.2, 9);
  const l = Math.max(1.5, volume / (Math.PI * d * d / 4));
  const lander = parts.has('lander');
  let base = 0;
  if (parts.has('engines') && tag.engines > 0) {
    base = bellSize(tag.engine_thrust ?? 1e5).len;
    engineBells(group, tag.engines, tag.engine_thrust ?? 1e5, d, base);
  }
  if (lander && !parts.has('tanks')) {
    // Body on legs: four struts splayed out to footpads level with the engine exits.
    const body = cylinder(d, l, FOIL, base, 8);
    group.add(body);
    for (let i = 0; i < 4; i++) {
      const a = (i / 4) * Math.PI * 2 + Math.PI / 4;
      const top = new THREE.Vector3(Math.cos(a) * d * 0.45, base + l * 0.4, Math.sin(a) * d * 0.45);
      const foot = new THREE.Vector3(Math.cos(a) * (d * 0.5 + 1.2), 0, Math.sin(a) * (d * 0.5 + 1.2));
      const len = top.distanceTo(foot);
      const strut = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, len, 6), material(WHITE));
      strut.position.copy(top).add(foot).multiplyScalar(0.5);
      strut.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), foot.clone().sub(top).normalize());
      group.add(strut);
      const pad = cylinder(0.6, 0.08, WHITE, 0, 10);
      pad.position.x = foot.x;
      pad.position.z = foot.z;
      group.add(pad);
    }
    return { object: group, length: base + l };
  }
  group.add(cylinder(d, l, parts.has('tanks') ? WHITE : FOIL, base, 24));
  group.add(cylinder(d * 1.01, 0.4, FOIL, base + l * 0.5 - 0.2, 24));
  let top = base + l;
  if (lander && parts.has('tanks')) {  // the lander rides on top of the stack
    const ld = Math.min(d, 4.0);
    group.add(cylinder(ld, ld * 0.8, FOIL, top, 8));
    top += ld * 0.8;
  }
  return { object: group, length: top };
}

// Returns {object, length} (metres), or null if the tag isn't recognised.
export function buildCraftModel(tag) {
  const v = VEHICLES[tag?.vehicle];
  const parts = new Set(Array.isArray(tag?.parts) ? tag.parts : []);
  if (!parts.size) return null;
  if (tag.vehicle === 'lunar') return lunarVehicle(tag);
  const group = new THREE.Group();

  if (parts.size === 1 && parts.has('payload')) {
    const top = satellite(group, 0);
    group.position.y = -top / 2;  // centred
    const outer = new THREE.Group();
    outer.add(group);
    return { object: outer, length: top };
  }
  if (!v) return null;

  let y = 0;
  if (parts.has('core')) {
    if (parts.has('boosters') && v.boosters) boosterRing(v, tag.boosters ?? 0, group, 0);
    y = coreStage(v, group, 0);
  } else if (parts.has('boosters') && v.boosters) {
    boosterRing(v, tag.boosters ?? 1, group, 0);
    y = v.boosters.l;
  }
  const upperBase = y;
  if (parts.has('upper')) y = upperStage(v, group, y, !parts.has('core'));
  const fairingOn = parts.has('fairing') && v.fairing;
  if (parts.has('payload') && !fairingOn) y = satellite(group, y);
  if (fairingOn) {
    const alone = parts.size === 1;
    const base = alone ? 0 : (v.fairing.coversUpper && parts.has('upper') ? upperBase : y);
    y = Math.max(y, fairing(v, group, base, alone));
  }
  return { object: group, length: y };
}

export function disposeModel(object) {
  object.traverse((o) => {
    if (o.geometry) o.geometry.dispose();
    if (o.material) o.material.dispose();
  });
}
