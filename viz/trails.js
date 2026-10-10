// Spacecraft trajectory trails.
//
// The simulation records each craft's history relative to the body whose sphere
// of influence it was in (non-rotating axes).  Here every point is re-anchored
// to that body's *current* position, so an Earth orbit draws as a closed
// ellipse around Earth rather than a smear along Earth's path around the Sun.
// Points arrive incrementally from /api/trails (only new points each poll).

import * as THREE from 'three';

const KM = 1000;
const MIN_ALPHA = 0.2;   // oldest point
const MAX_ALPHA = 0.95;  // newest point

export class TrailRenderer {
  constructor(scene, color) {
    this.scene = scene;
    this.color = new THREE.Color(color);
    this.enabled = true;
    this.trails = new Map();  // id -> { version, resting, points, line }
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

  merge(update) {
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
        t = { version: null, resting: false, points: [], line };
        this.trails.set(id, t);
      }
      if (u.full || u.version !== t.version) t.points = u.points;
      else t.points.push(...u.points);
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
  }

  // objects: viewer registry (id -> {abs: [m,m,m]}); origin: focus position (m).
  rebuild(objects, origin) {
    const [ox, oy, oz] = origin;
    for (const [id, t] of this.trails) {
      const pts = t.points;
      const pos = [];
      const col = [];
      const n = pts.length;
      const alpha = (i) => MIN_ALPHA + (MAX_ALPHA - MIN_ALPHA) * (n > 1 ? i / (n - 1) : 1);
      const at = (p) => {
        const b = objects.get(p[2]);
        if (!b) return null;
        return [(b.abs[0] + p[3] - ox) / KM, (b.abs[1] + p[4] - oy) / KM, (b.abs[2] + p[5] - oz) / KM];
      };
      const push = (xyz, a) => {
        pos.push(xyz[0], xyz[1], xyz[2]);
        col.push(this.color.r, this.color.g, this.color.b, a);
      };

      let prev = null;
      for (let i = 0; i < n; i++) {
        const p = pts[i];
        const xyz = at(p);
        // New segment on a break flag (e.g. liftoff) or a change of reference body.
        if (xyz && prev && !p[6] && prev.p[2] === p[2]) {
          push(prev.xyz, alpha(i - 1));
          push(xyz, alpha(i));
        }
        prev = xyz ? { p, xyz } : null;
      }
      // Connect the last recorded point to the craft's current position.
      const craft = objects.get(id);
      if (prev && craft && !t.resting) {
        push(prev.xyz, MAX_ALPHA);
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
