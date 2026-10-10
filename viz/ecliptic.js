// Ecliptic-plane orientation overlays.
//
// * Sun-centred polar grid in the ecliptic plane (z = 0 of the J2000 ecliptic
//   frame): rings at 0.1 / 1 / 10 AU that fade in and out with zoom, spokes
//   every 30 deg, and the vernal-equinox direction (+X, 0 deg) highlighted.
// * Local ecliptic ring around the focused object, parallel to the ecliptic,
//   scaled to the camera distance, marking the equinox direction and ecliptic
//   north.  It takes over from the Sun grid at close range.
//
// Units are km in the floating-origin scene used by app.js.

import * as THREE from 'three';

const AU_KM = 1.495978707e8;
const GRID_RADIUS_AU = 50;
const GRID_COLOR = '#7f9cd6';
const ACCENT_COLOR = '#4fd1c5';
const RING_LABELS_AU = [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 30, 40, 50];
const RING_LABEL_ANGLE = THREE.MathUtils.degToRad(225);
// Camera range (distance to the focused object's centre) over which the Sun
// grid fades in and the local ring fades out.
const FADE_START_AU = 0.002;
const FADE_END_AU = 0.02;

function hexToVec3(hex) {
  const n = parseInt(hex.slice(1), 16);
  return new THREE.Vector3(((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255);
}

const GRID_VERT = /* glsl */ `
#include <common>
#include <logdepthbuf_pars_vertex>
varying vec2 vP;
void main() {
  vP = position.xy;  // AU, ecliptic x/y relative to the Sun
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  #include <logdepthbuf_vertex>
}`;

const GRID_FRAG = /* glsl */ `
#include <logdepthbuf_pars_fragment>
uniform vec3 uColor;
uniform vec3 uAccent;
uniform float uOpacity;
uniform float uRadius;
varying vec2 vP;

float lineAA(float g, float w) { return 1.0 - min(abs(fract(g - 0.5) - 0.5) / max(w, 1e-7), 1.0); }
// Fade a line family out once its lines get closer than ~5-25 px apart.
float fadeDense(float w) { return 1.0 - smoothstep(0.04, 0.2, w); }
float ringLevel(float r, float s) {
  float g = r / s;
  float w = fwidth(g);
  return lineAA(g, w) * fadeDense(w);
}

void main() {
  #include <logdepthbuf_fragment>
  float r = length(vP);
  float rings = max(max(0.20 * ringLevel(r, 0.1), 0.55 * ringLevel(r, 1.0)), 0.8 * ringLevel(r, 10.0));

  float th = atan(vP.y, vP.x);
  float th2 = atan(-vP.y, -vP.x);  // avoids the derivative jump at +-pi
  float wth = min(fwidth(th), fwidth(th2));
  float sp = radians(30.0);
  float spokes = 0.35 * lineAA(th / sp, wth / sp) * fadeDense(wth / sp);
  float tau = 6.28318530718;
  float equinox = 0.9 * lineAA(th / tau, wth / tau) * step(0.0, vP.x) * fadeDense(wth / tau);

  float a = max(max(rings, spokes), equinox);
  vec3 col = mix(uColor, uAccent, clamp(equinox / max(a, 1e-4), 0.0, 1.0));
  a *= uOpacity * (1.0 - smoothstep(0.8 * uRadius, uRadius, r));
  if (a <= 0.002) discard;
  gl_FragColor = vec4(col, a);
}`;

function lineMaterial(color) {
  return new THREE.LineBasicMaterial({ color, transparent: true, depthWrite: false });
}

export class EclipticOverlay {
  constructor(scene, labelLayer) {
    this.enabled = true;

    // Sun-centred polar grid: a quad in AU units, scaled to km.
    this.grid = new THREE.Mesh(
      new THREE.PlaneGeometry(2 * GRID_RADIUS_AU, 2 * GRID_RADIUS_AU),
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
          uRadius: { value: GRID_RADIUS_AU },
        },
      }),
    );
    this.grid.scale.setScalar(AU_KM);
    this.grid.frustumCulled = false;
    this.grid.renderOrder = 1;
    scene.add(this.grid);

    // Local ring (unit radius, scaled each frame).
    this.local = new THREE.Group();
    this.local.renderOrder = 1;
    const circle = [];
    for (let i = 0; i < 256; i++) {
      const a = (i / 256) * Math.PI * 2;
      circle.push(new THREE.Vector3(Math.cos(a), Math.sin(a), 0));
    }
    const ticks = [];
    for (let deg = 30; deg < 360; deg += 30) {
      const a = THREE.MathUtils.degToRad(deg);
      ticks.push(new THREE.Vector3(0.92 * Math.cos(a), 0.92 * Math.sin(a), 0),
        new THREE.Vector3(Math.cos(a), Math.sin(a), 0));
    }
    this.localMaterials = [lineMaterial(GRID_COLOR), lineMaterial(GRID_COLOR), lineMaterial(ACCENT_COLOR),
      lineMaterial(GRID_COLOR)];
    const [mCircle, mTicks, mEquinox, mPole] = this.localMaterials;
    this.local.add(
      new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(circle), mCircle),
      new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(ticks), mTicks),
      new THREE.Line(new THREE.BufferGeometry().setFromPoints(
        [new THREE.Vector3(0, 0, 0), new THREE.Vector3(1.15, 0, 0)]), mEquinox),
      new THREE.Line(new THREE.BufferGeometry().setFromPoints(
        [new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, 0, 0.6)]), mPole),
    );
    for (const child of this.local.children) child.frustumCulled = false;
    scene.add(this.local);

    // HTML labels.
    const mk = (text, cls = '') => {
      const el = document.createElement('div');
      el.className = `ecl-label ${cls}`;
      el.textContent = text;
      labelLayer.appendChild(el);
      return el;
    };
    this.sunEquinoxLabel = mk('♈︎ 0°', 'accent');
    this.ringLabels = RING_LABELS_AU.map((r) => ({ r, el: mk(`${r} AU`) }));
    this.localEquinoxLabel = mk('♈︎', 'accent');
    this.localPoleLabel = mk('ecliptic N');
  }

  setEnabled(on) {
    this.enabled = on;
  }

  // centreRel: Sun position relative to the floating origin (km).
  update({ camera, canvas, centreRel, focusRadiusKm }) {
    const allLabels = [this.sunEquinoxLabel, this.localEquinoxLabel, this.localPoleLabel,
      ...this.ringLabels.map((l) => l.el)];
    if (!this.enabled) {
      this.grid.visible = false;
      this.local.visible = false;
      for (const el of allLabels) el.style.display = 'none';
      return;
    }

    const range = camera.position.length();  // camera distance to the focus centre, km
    const sunAlpha = THREE.MathUtils.smoothstep(range / AU_KM, FADE_START_AU, FADE_END_AU);
    const localAlpha = 1 - sunAlpha;

    // Sun grid.
    this.grid.visible = sunAlpha > 0.01;
    this.grid.position.copy(centreRel);
    this.grid.material.uniforms.uOpacity.value = sunAlpha;

    // Local ring: radius follows the camera, but stays outside the focused body.
    const ringR = Math.max(0.55 * range, 1.3 * focusRadiusKm);
    this.local.visible = localAlpha > 0.01;
    this.local.scale.setScalar(ringR);
    const [mCircle, mTicks, mEquinox, mPole] = this.localMaterials;
    mCircle.opacity = 0.55 * localAlpha;
    mTicks.opacity = 0.55 * localAlpha;
    mEquinox.opacity = 0.9 * localAlpha;
    mPole.opacity = 0.7 * localAlpha;

    // Labels.
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    const focalPx = (h / 2) / Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    const tmp = new THREE.Vector3();
    const place = (el, pos, show) => {
      if (!show) {
        el.style.display = 'none';
        return null;
      }
      tmp.copy(pos).applyMatrix4(camera.matrixWorldInverse);
      if (tmp.z >= 0) {
        el.style.display = 'none';
        return null;
      }
      const x = w / 2 + (focalPx * tmp.x) / -tmp.z;
      const y = h / 2 - (focalPx * tmp.y) / -tmp.z;
      if (x < -50 || x > w + 50 || y < -20 || y > h + 20) {
        el.style.display = 'none';
        return null;
      }
      el.style.display = '';
      el.style.opacity = String(show === true ? 1 : show);
      el.style.transform = `translate(${x + 4}px, ${y - 6}px)`;
      return [x, y];
    };

    const showSun = sunAlpha > 0.2 ? sunAlpha : false;
    const camToSunAU = camera.position.distanceTo(centreRel) / AU_KM;
    const eqR = THREE.MathUtils.clamp(0.5 * camToSunAU, 0.3, 45);
    place(this.sunEquinoxLabel, centreRel.clone().add(new THREE.Vector3(eqR * AU_KM, 0, 0)), showSun);

    const sunScreen = (() => {
      tmp.copy(centreRel).applyMatrix4(camera.matrixWorldInverse);
      return tmp.z < 0 ? [w / 2 + (focalPx * tmp.x) / -tmp.z, h / 2 - (focalPx * tmp.y) / -tmp.z] : null;
    })();
    let last = sunScreen;
    for (const { r, el } of this.ringLabels) {
      const p = centreRel.clone().add(new THREE.Vector3(
        r * AU_KM * Math.cos(RING_LABEL_ANGLE), r * AU_KM * Math.sin(RING_LABEL_ANGLE), 0));
      let s = place(el, p, showSun);
      if (s && last && Math.hypot(s[0] - last[0], s[1] - last[1]) < 50) {
        el.style.display = 'none';
        s = null;
      }
      if (s) last = s;
    }

    const showLocal = localAlpha > 0.2 ? localAlpha : false;
    place(this.localEquinoxLabel, new THREE.Vector3(1.15 * ringR, 0, 0), showLocal);
    place(this.localPoleLabel, new THREE.Vector3(0, 0, 0.6 * ringR), showLocal);
  }
}
