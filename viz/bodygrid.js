// Local polar grid around a body, in that body's equatorial plane.
//
// Shown for the focused body, or for a focused spacecraft's reference body
// (the smallest sphere of influence containing it).  Rings are distances from
// the body's centre at powers of ten that follow the zoom, out to a few camera
// ranges (at most the sphere of influence); spokes every 30 deg
// with the equator's ascending node on the ecliptic highlighted (the vernal
// equinox for Earth); a short axis marks the body's north pole.  The grid
// fades out as the camera leaves the sphere of influence.
//
// Units are km in the floating-origin scene used by app.js.

import * as THREE from 'three';

const GRID_COLOR = '#d6b87f';
const ACCENT_COLOR = '#4fd1c5';
// The grid reaches out to this many camera ranges (and at most the SOI).
const FADE_RANGES = 3;
const LABEL_ANGLE = THREE.MathUtils.degToRad(225);

function hexToVec3(hex) {
  const n = parseInt(hex.slice(1), 16);
  return new THREE.Vector3(((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255);
}

const GRID_VERT = /* glsl */ `
#include <common>
#include <logdepthbuf_pars_vertex>
uniform float uExtent;
varying vec2 vP;
varying vec3 vView;
void main() {
  vP = position.xy * uExtent;  // km in the equatorial plane, from the body centre
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  vView = mv.xyz;              // km from the camera
  gl_Position = projectionMatrix * mv;
  #include <logdepthbuf_vertex>
}`;

const GRID_FRAG = /* glsl */ `
#include <logdepthbuf_pars_fragment>
uniform vec3 uColor;
uniform vec3 uAccent;
uniform float uOpacity;
uniform float uExtent;
uniform float uFadeR;    // grid fades out beyond this radius (follows the zoom)
uniform float uSpacing;  // fine ring spacing, km (a power of ten)
uniform float uFine;     // fine-ring weight, fades out as the camera zooms out
uniform float uRange;    // camera distance from the body centre, km
varying vec2 vP;
varying vec3 vView;

float lineAA(float g, float w) { return 1.0 - min(abs(fract(g - 0.5) - 0.5) / max(w, 1e-7), 1.0); }
float fadeDense(float w) { return 1.0 - smoothstep(0.04, 0.2, w); }
float ringLevel(float r, float s) {
  float g = r / s;
  float w = fwidth(g);
  return lineAA(g, w) * fadeDense(w);
}

void main() {
  #include <logdepthbuf_fragment>
  float r = length(vP);
  float rings = max(0.55 * uFine * ringLevel(r, uSpacing), 0.55 * ringLevel(r, 10.0 * uSpacing));

  float th = atan(vP.y, vP.x);
  float th2 = atan(-vP.y, -vP.x);
  float wth = min(fwidth(th), fwidth(th2));
  float sp = radians(30.0);
  float spokes = 0.35 * lineAA(th / sp, wth / sp) * fadeDense(wth / sp);
  float tau = 6.28318530718;
  float node = 0.9 * lineAA(th / tau, wth / tau) * step(0.0, vP.x) * fadeDense(wth / tau);

  float a = max(max(rings, spokes), node);
  vec3 col = mix(uColor, uAccent, clamp(node / max(a, 1e-4), 0.0, 1.0));
  a *= uOpacity * (1.0 - smoothstep(0.6 * uFadeR, uFadeR, r));
  // Also fade with distance from the camera, so grazing views don't fill with distant rings.
  a *= 1.0 - smoothstep(1.2 * uRange, 2.5 * uRange, length(vView));
  if (a <= 0.002) discard;
  gl_FragColor = vec4(col, a);
}`;

function fmtKm(km) {
  return `${Math.round(km).toLocaleString('en-US')} km`;
}

export class BodyGrid {
  constructor(scene, labelLayer) {
    this.enabled = true;

    this.grid = new THREE.Mesh(
      new THREE.PlaneGeometry(2, 2),
      new THREE.ShaderMaterial({
        vertexShader: GRID_VERT,
        fragmentShader: GRID_FRAG,
        transparent: true,
        depthWrite: false,
        side: THREE.DoubleSide,
        uniforms: {
          uColor: { value: hexToVec3(GRID_COLOR) },
          uAccent: { value: hexToVec3(ACCENT_COLOR) },
          uOpacity: { value: 1 },
          uExtent: { value: 1 },
          uFadeR: { value: 1 },
          uRange: { value: 1 },
          uSpacing: { value: 1 },
          uFine: { value: 1 },
        },
      }),
    );
    this.grid.frustumCulled = false;
    this.grid.renderOrder = 1;
    scene.add(this.grid);

    this.poleMaterial = new THREE.LineBasicMaterial({ color: GRID_COLOR, transparent: true, depthWrite: false });
    this.pole = new THREE.Line(new THREE.BufferGeometry().setFromPoints(
      [new THREE.Vector3(), new THREE.Vector3()]), this.poleMaterial);
    this.pole.frustumCulled = false;
    this.pole.renderOrder = 1;
    scene.add(this.pole);

    const mk = (cls = '') => {
      const el = document.createElement('div');
      el.className = `ecl-label body-grid ${cls}`;
      labelLayer.appendChild(el);
      return el;
    };
    this.nodeLabel = mk('accent');
    this.poleLabel = mk();
    this.ringLabels = Array.from({ length: 18 }, () => mk());
    this.allLabels = [this.nodeLabel, this.poleLabel, ...this.ringLabels];

    this.basis = new THREE.Matrix4();
    this.x = new THREE.Vector3();
    this.y = new THREE.Vector3();
    this.z = new THREE.Vector3();
  }

  setEnabled(on) {
    this.enabled = on;
  }

  hide() {
    this.grid.visible = false;
    this.pole.visible = false;
    for (const el of this.allLabels) el.style.display = 'none';
  }

  // body: {id, rel (km, floating origin), radiusKm, pole (unit, ecliptic frame), soiKm}
  update({ camera, canvas, body }) {
    if (!this.enabled || !body || !Number.isFinite(body.soiKm)) {
      this.hide();
      return;
    }
    const range = camera.position.distanceTo(body.rel);
    const alpha = 1 - THREE.MathUtils.smoothstep(range, 0.6 * body.soiKm, 1.2 * body.soiKm);
    if (alpha < 0.01) {
      this.hide();
      return;
    }

    // Equatorial basis: x = ascending node of the equator on the ecliptic, z = north pole.
    const { x, y, z } = this;
    z.copy(body.pole).normalize();
    x.set(-z.y, z.x, 0);  // ecliptic north (0,0,1) x pole
    if (x.lengthSq() < 1e-12) x.set(1, 0, 0);
    x.normalize();
    y.crossVectors(z, x);
    this.basis.makeBasis(x, y, z);

    // Ring spacing follows the camera distance from the body centre.
    const f = Math.log10(Math.max(range / 2, 1e-6));
    const k = Math.floor(f);
    const t = f - k;
    const spacing = 10 ** k;

    const g = this.grid;
    g.visible = true;
    g.position.copy(body.rel);
    g.quaternion.setFromRotationMatrix(this.basis);
    g.scale.setScalar(body.soiKm);
    const u = g.material.uniforms;
    u.uOpacity.value = alpha;
    u.uExtent.value = body.soiKm;
    const fadeR = Math.min(body.soiKm, Math.max(FADE_RANGES * range, 2 * body.radiusKm));
    u.uFadeR.value = fadeR;
    u.uRange.value = range;
    u.uSpacing.value = spacing;
    u.uFine.value = 1 - t;

    // Pole axis from the north pole surface outward.
    const poleLen = Math.max(0.5 * body.radiusKm, 0.25 * range);
    const pos = this.pole.geometry.attributes.position;
    const p0 = body.rel.clone().addScaledVector(z, body.radiusKm);
    const p1 = body.rel.clone().addScaledVector(z, body.radiusKm + poleLen);
    pos.setXYZ(0, p0.x, p0.y, p0.z);
    pos.setXYZ(1, p1.x, p1.y, p1.z);
    pos.needsUpdate = true;
    this.pole.visible = true;
    this.poleMaterial.opacity = 0.7 * alpha;

    // Labels.
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const focalPx = (h / 2) / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    const tmp = new THREE.Vector3();
    const project = (p) => {
      tmp.copy(p).applyMatrix4(camera.matrixWorldInverse);
      if (tmp.z >= 0) return null;
      const sx = w / 2 + (focalPx * tmp.x) / -tmp.z;
      const sy = h / 2 - (focalPx * tmp.y) / -tmp.z;
      if (sx < -50 || sx > w + 50 || sy < -20 || sy > h + 20) return null;
      return [sx, sy];
    };
    const place = (el, p, opacity) => {
      const s = opacity > 0.2 ? project(p) : null;
      if (!s) {
        el.style.display = 'none';
        return null;
      }
      el.style.display = '';
      el.style.opacity = String(opacity);
      el.style.transform = `translate(${s[0] + 4}px, ${s[1] - 6}px)`;
      return s;
    };
    const inPlane = (r, angle) => body.rel.clone()
      .addScaledVector(x, r * Math.cos(angle)).addScaledVector(y, r * Math.sin(angle));

    this.nodeLabel.textContent = body.id === 'earth' ? '♈︎ 0°' : '☊ 0°';
    this.nodeLabel.title = 'Ascending node of the equator on the ecliptic';
    const nodeR = THREE.MathUtils.clamp(0.5 * range, 1.3 * body.radiusKm, 0.6 * fadeR);
    place(this.nodeLabel, inPlane(nodeR, 0), alpha);
    this.poleLabel.textContent = `${body.name ?? body.id} N`;
    place(this.poleLabel, p1, alpha);

    // Ring labels: fine and coarse multiples, nearest first, decluttered.
    const radii = [];
    for (let n = 1; n <= 9; n++) radii.push([n * spacing, 1 - t]);
    for (let n = 2; n <= 9; n++) radii.push([n * 10 * spacing, 1]);
    radii.unshift([10 * spacing, 1]);
    radii.sort((a, b) => a[0] - b[0]);
    let last = project(body.rel);
    let i = 0;
    for (const [r, weight] of radii) {
      if (i >= this.ringLabels.length) break;
      if (r < 1.05 * body.radiusKm || r > 0.7 * fadeR) continue;
      const el = this.ringLabels[i];
      el.textContent = fmtKm(r);
      let s = place(el, inPlane(r, LABEL_ANGLE), alpha * weight);
      if (s && last && Math.hypot(s[0] - last[0], s[1] - last[1]) < 60) {
        el.style.display = 'none';
        s = null;
      }
      if (s) {
        last = s;
        i++;
      }
    }
    for (; i < this.ringLabels.length; i++) this.ringLabels[i].style.display = 'none';
  }
}
