// Spacecraft trajectory trails, each drawn whole in one frame.
//
// The simulation records each craft's history relative to the body whose sphere
// of influence it was in (non-rotating axes).  Drawing every point around its own
// body's current position splits a trail wherever the craft crossed into another
// sphere of influence, because the two bodies keep moving relative to each other.
// Instead, every trail is drawn in one frame body F: each point is re-expressed
// relative to F as it was when the point was recorded, using the simulation's body
// history (positions and velocities every couple of hours, cubic-Hermite
// interpolated), and then drawn around F's current position.  An orbit of F closes
// into an ellipse; anything else is drawn as seen by an observer riding with F.
// Points and body history arrive incrementally from /api/trails.

import * as THREE from 'three';

const KM = 1000;
const MIN_ALPHA = 0.2;   // oldest point
const MAX_ALPHA = 0.95;  // newest point

class BodyHistory {
  constructor() {
    this.version = null;
    this.index = new Map();  // body id -> column
    this.samples = [];       // [seq, t, positions[[x,y,z]...], velocities[...]]
    this.head = null;        // [t, positions, velocities]: the current state, not yet a sample
    this.stableUntil = -Infinity;  // time up to which interpolation no longer changes
  }

  since() {
    if (this.version === null) return null;
    return [this.version, this.samples.length ? this.samples[this.samples.length - 1][0] : -1];
  }

  // Returns true when previously converted points are no longer valid (new history).
  merge(u) {
    if (!u) return false;
    let reset = false;
    if (u.full || u.version !== this.version) {
      this.samples = u.samples;
      reset = true;
    } else {
      this.samples.push(...u.samples);
    }
    this.version = u.version;
    this.index = new Map(u.ids.map((id, i) => [id, i]));
    this.head = u.head;
    this.stableUntil = this.samples.length ? this.samples[this.samples.length - 1][1] : -Infinity;
    return reset;
  }

  // Drop samples no longer needed to bracket times >= t (keeping at least two).
  prune(t) {
    let k = 0;
    while (k + 2 < this.samples.length && this.samples[k + 1][1] <= t) k++;
    if (k) this.samples.splice(0, k);
  }

  // Position of a body at time t (m, simulation frame), or null outside the history.
  at(id, t) {
    const i = this.index.get(id);
    if (i === undefined) return null;
    const s = this.samples;
    const n = s.length;
    const list = (k) => (k < n ? s[k] : [null, this.head[0], this.head[1], this.head[2]]);
    const last = this.head ? n : n - 1;
    if (last < 0 || n === 0) return null;
    if (t < s[0][1] - 1e-6 || t > list(last)[1] + 1e-6) return null;
    let lo = 0;
    let hi = last;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (list(mid)[1] <= t) lo = mid;
      else hi = mid;
    }
    const a = list(lo);
    const b = list(Math.min(hi, last));
    const h = b[1] - a[1];
    if (h <= 0) return a[2][i];
    const u = Math.min(Math.max((t - a[1]) / h, 0), 1);
    const u2 = u * u;
    const u3 = u2 * u;
    const h00 = 2 * u3 - 3 * u2 + 1;
    const h10 = (u3 - 2 * u2 + u) * h;
    const h01 = -2 * u3 + 3 * u2;
    const h11 = (u3 - u2) * h;
    const p0 = a[2][i];
    const p1 = b[2][i];
    const v0 = a[3][i];
    const v1 = b[3][i];
    return [0, 1, 2].map((k) => h00 * p0[k] + h10 * v0[k] + h01 * p1[k] + h11 * v1[k]);
  }
}

export class TrailRenderer {
  constructor(scene, color) {
    this.scene = scene;
    this.color = new THREE.Color(color);
    this.enabled = true;
    this.trails = new Map();  // id -> { version, resting, points, line, cache }
    this.history = new BodyHistory();
  }

  setEnabled(on) {
    this.enabled = on;
    for (const t of this.trails.values()) t.line.visible = on;
  }

  // {id: [version, lastSeq]} for an incremental fetch.
  since() {
    const out = {};
    for (const [id, t] of this.trails) {
      out[id] = [t.version, t.points.length ? t.points[t.points.length - 1][0] : -1];
    }
    return out;
  }

  bodiesSince() {
    return this.history.since();
  }

  merge(update, bodies) {
    if (this.history.merge(bodies)) {
      for (const t of this.trails.values()) t.cache = null;
    }
    for (const [id, u] of Object.entries(update)) {
      let t = this.trails.get(id);
      if (!t) {
        const line = new THREE.LineSegments(
          new THREE.BufferGeometry(),
          new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false }),
        );
        line.frustumCulled = false;
        line.renderOrder = 2;
        line.visible = this.enabled;
        this.scene.add(line);
        t = { version: null, resting: false, points: [], line, cache: null };
        this.trails.set(id, t);
      }
      if (u.full || u.version !== t.version) {
        t.points = u.points;
        t.cache = null;
      } else {
        t.points.push(...u.points);
      }
      t.version = u.version;
      t.resting = u.resting;
    }
    for (const [id, t] of this.trails) {
      if (id in update) continue;
      this.scene.remove(t.line);
      t.line.geometry.dispose();
      t.line.material.dispose();
      this.trails.delete(id);
    }
    let oldest = Infinity;
    for (const t of this.trails.values()) if (t.points.length) oldest = Math.min(oldest, t.points[0][1]);
    if (Number.isFinite(oldest)) this.history.prune(oldest);
  }

  // A point's position relative to the frame body (m), as it was when recorded.  Falls back
  // to the bodies' current offset when the history doesn't cover the point's time.
  _inFrame(p, frameId, objects) {
    if (p[2] === frameId) return [p[3], p[4], p[5]];
    const a = this.history.at(p[2], p[1]);
    const b = this.history.at(frameId, p[1]);
    if (a && b) return [a[0] - b[0] + p[3], a[1] - b[1] + p[4], a[2] - b[2] + p[5]];
    const ra = objects.get(p[2]);
    const rb = objects.get(frameId);
    if (!ra || !rb) return null;
    return [ra.abs[0] - rb.abs[0] + p[3], ra.abs[1] - rb.abs[1] + p[4], ra.abs[2] - rb.abs[2] + p[5]];
  }

  // objects: viewer registry (id -> {abs: [m,m,m]}); origin: focus position (m);
  // frameId: the body every trail is drawn relative to.
  rebuild(objects, origin, frameId) {
    const [ox, oy, oz] = origin;
    const frame = objects.get(frameId);
    if (!frame) return;
    const [fx, fy, fz] = frame.abs;
    for (const [id, t] of this.trails) {
      const pts = t.points;
      const n = pts.length;
      // Converted points are cached while they no longer depend on the newest history.
      if (!t.cache || t.cache.frame !== frameId) t.cache = { frame: frameId, coords: [] };
      const coords = t.cache.coords;
      if (coords.length > n) coords.length = 0;
      while (coords.length < n && pts[coords.length][1] <= this.history.stableUntil) {
        coords.push(this._inFrame(pts[coords.length], frameId, objects));
      }
      const pos = [];
      const col = [];
      const alpha = (i) => MIN_ALPHA + (MAX_ALPHA - MIN_ALPHA) * (n > 1 ? i / (n - 1) : 1);
      const at = (i) => {
        const c = i < coords.length ? coords[i] : this._inFrame(pts[i], frameId, objects);
        return c ? [(fx + c[0] - ox) / KM, (fy + c[1] - oy) / KM, (fz + c[2] - oz) / KM] : null;
      };
      const push = (xyz, a) => {
        pos.push(xyz[0], xyz[1], xyz[2]);
        col.push(this.color.r, this.color.g, this.color.b, a);
      };

      let prev = null;
      for (let i = 0; i < n; i++) {
        const xyz = at(i);
        // New segment on a break flag (e.g. liftoff after resting on a surface).
        if (xyz && prev && !pts[i][6]) {
          push(prev, alpha(i - 1));
          push(xyz, alpha(i));
        }
        prev = xyz;
      }
      // Connect the last recorded point to the craft's current position.
      const craft = objects.get(id);
      if (prev && craft && !t.resting) {
        push(prev, MAX_ALPHA);
        push([(craft.abs[0] - ox) / KM, (craft.abs[1] - oy) / KM, (craft.abs[2] - oz) / KM], MAX_ALPHA);
      }

      const g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
      g.setAttribute('color', new THREE.Float32BufferAttribute(col, 4));
      t.line.geometry.dispose();
      t.line.geometry = g;
    }
  }
}
