// Mirrors buildSolidForExport() from index.html for the Ripple Field lens.
// Writes a sealed binary STL: top sculpted face, flat bottom, perimeter walls.
//
// Run with defaults:
//   node exports/build-stl.mjs
//
// Override any param via flags:
//   node exports/build-stl.mjs --wavelength=42 --amplitude=100 --step=10 --thickness=15 --resolution=1.0
//
// Output: exports/prismatica_ripple_<W>x<H>_amp<amp>_step<step>_wl<wl>_<thickness>mmbase.stl
//
// Geometry: linearly STEPPED concentric ripples. Centre peak = `amplitude` mm,
// each successive ring is `step` mm shorter than the one inside it. Outer 15%
// of the inscribed circle is feathered so the rim fades flat (no sharp lop-off).
//
// IMPORTANT: this script only runs the geometry. If you've tweaked the lens
// in the live visualiser, either:
//   1. Pass the same params as flags above, OR
//   2. Use the in-app "export STL (A)" button in the Actions folder — that
//      always exports exactly what's on screen. (Recommended.)

import { writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

// --- parse CLI flags ---
const args = Object.fromEntries(
  process.argv.slice(2).map(a => {
    const m = a.match(/^--([a-zA-Z0-9_]+)=(.+)$/);
    return m ? [m[1], m[2]] : [a, true];
  })
);
const num = (k, dflt) => args[k] !== undefined ? Number(args[k]) : dflt;

// --- lens parameters (defaults match index.html stepped_ripple defaults) ---
const PANEL_W   = num('width',      600);   // mm
const PANEL_H   = num('height',     600);   // mm
const THICKNESS = num('thickness',  15);    // mm — base flat thickness below the ripples
const RESOLUTION= num('resolution', 1.0);   // mm grid pitch
const RIPPLE = {
  wavelength: num('wavelength', 42),    // mm — distance between ripple peaks
  amplitude:  num('amplitude',  100),   // mm — height of the central peak
  step:       num('step',       10),    // mm — drop in peak height per successive ripple
  edgeFade:   num('edgefade',   0.18),  // 0..0.5 — ratio of short-side that fades to flat
  bumpSharp:  num('bumpsharp',  0.5),   // 0..1 — 0=flat plateaus, 0.5=cosine, 1=sharp spikes
};

console.log('Building with:');
console.log(`  panel:       ${PANEL_W} × ${PANEL_H} mm`);
console.log(`  base:        ${THICKNESS} mm thick`);
console.log(`  ripple:      wavelength ${RIPPLE.wavelength} mm`);
console.log(`               central peak ${RIPPLE.amplitude} mm`);
console.log(`               steps down ${RIPPLE.step} mm per ring`);
console.log(`               edge fade ratio ${RIPPLE.edgeFade}`);
console.log(`               bump shape exponent ${RIPPLE.bumpSharp}`);
console.log(`  grid pitch:  ${RESOLUTION} mm`);

// --- height field: stepped ripples covering the full panel rectangle, fading
// smoothly to flat in a margin near each panel edge. No flat corners.
const TAU = Math.PI * 2;
const halfW = PANEL_W * 0.5, halfH = PANEL_H * 0.5;
const margin = Math.max(0.0001, Math.min(PANEL_W, PANEL_H) * RIPPLE.edgeFade);
// bumpSharp slider maps to symmetric pow-based wave reshape:
//   0.0 = flat plateaus (1 - (1-v)^5)
//   0.5 = pass-through cosine
//   1.0 = sharp spikes (v^5)
function reshapeWave(v) {
  if (RIPPLE.bumpSharp === 0.5) return v;
  if (RIPPLE.bumpSharp > 0.5) {
    const n = 1 + (RIPPLE.bumpSharp - 0.5) * 8;
    return Math.pow(v, n);
  }
  const n = 1 + (0.5 - RIPPLE.bumpSharp) * 8;
  return 1 - Math.pow(1 - v, n);
}
function heightAt(x, y) {
  const dx = halfW - Math.abs(x);
  const dy = halfH - Math.abs(y);
  const d = Math.min(dx, dy);
  if (d <= 0) return 0;
  const env = d >= margin ? 1 : 0.5 * (1 - Math.cos(Math.PI * (d / margin)));
  const r = Math.hypot(x, y);
  const ampLocal = Math.max(0, RIPPLE.amplitude - RIPPLE.step * (r / RIPPLE.wavelength));
  const waveRaw = 0.5 * (1 + Math.cos(TAU * r / RIPPLE.wavelength));
  const wave = reshapeWave(waveRaw);
  return ampLocal * wave * env;
}

// --- sample the grid ---
const nx = Math.max(4, Math.floor(PANEL_W / RESOLUTION) + 1);
const ny = Math.max(4, Math.floor(PANEL_H / RESOLUTION) + 1);
console.log(`\nSampling ${nx}×${ny} = ${(nx*ny).toLocaleString()} vertices…`);

const verts = [];
const topIds = [], botIds = [];
const vert = (x, y, z) => { const id = verts.length / 3; verts.push(x, y, z); return id; };

for (let i = 0; i < ny; i++) {
  const y = -PANEL_H/2 + (i / (ny - 1)) * PANEL_H;
  const rowT = [], rowB = [];
  for (let j = 0; j < nx; j++) {
    const x = -PANEL_W/2 + (j / (nx - 1)) * PANEL_W;
    rowT.push(vert(x, y, heightAt(x, y)));
  }
  for (let j = 0; j < nx; j++) {
    const x = -PANEL_W/2 + (j / (nx - 1)) * PANEL_W;
    rowB.push(vert(x, y, -THICKNESS));
  }
  topIds.push(rowT); botIds.push(rowB);
}

// --- triangulate into a sealed solid ---
const triangles = [];
const tri = (a, b, c) => triangles.push([a, b, c]);
for (let i = 0; i < ny - 1; i++) for (let j = 0; j < nx - 1; j++) {
  const a = topIds[i][j], b = topIds[i][j+1], c = topIds[i+1][j+1], d = topIds[i+1][j];
  tri(a, b, c); tri(a, c, d);
}
for (let i = 0; i < ny - 1; i++) for (let j = 0; j < nx - 1; j++) {
  const a = botIds[i][j], b = botIds[i][j+1], c = botIds[i+1][j+1], d = botIds[i+1][j];
  tri(a, c, b); tri(a, d, c);
}
for (let j = 0; j < nx - 1; j++) {
  const t1 = topIds[0][j], t2 = topIds[0][j+1], b1 = botIds[0][j], b2 = botIds[0][j+1];
  tri(t1, b1, b2); tri(t1, b2, t2);
  const ut1 = topIds[ny-1][j], ut2 = topIds[ny-1][j+1], ub1 = botIds[ny-1][j], ub2 = botIds[ny-1][j+1];
  tri(ut1, ub2, ub1); tri(ut1, ut2, ub2);
}
for (let i = 0; i < ny - 1; i++) {
  const t1 = topIds[i][0], t2 = topIds[i+1][0], b1 = botIds[i][0], b2 = botIds[i+1][0];
  tri(t1, b2, b1); tri(t1, t2, b2);
  const rt1 = topIds[i][nx-1], rt2 = topIds[i+1][nx-1], rb1 = botIds[i][nx-1], rb2 = botIds[i+1][nx-1];
  tri(rt1, rb1, rb2); tri(rt1, rb2, rt2);
}

// --- write binary STL ---
const buf = Buffer.alloc(80 + 4 + triangles.length * 50);
buf.write(`Prismatica Ripple wl${RIPPLE.wavelength} amp${RIPPLE.amplitude} step${RIPPLE.step} ${new Date().toISOString()}`, 0, 'utf8');
buf.writeUInt32LE(triangles.length, 80);

let off = 84;
for (const [ai, bi, ci] of triangles) {
  const ax = verts[ai*3], ay = verts[ai*3+1], az = verts[ai*3+2];
  const bx = verts[bi*3], by = verts[bi*3+1], bz = verts[bi*3+2];
  const cx = verts[ci*3], cy = verts[ci*3+1], cz = verts[ci*3+2];
  const ux = bx-ax, uy = by-ay, uz = bz-az;
  const vx = cx-ax, vy = cy-ay, vz = cz-az;
  let nx_ = uy*vz - uz*vy;
  let ny_ = uz*vx - ux*vz;
  let nz_ = ux*vy - uy*vx;
  const len = Math.hypot(nx_, ny_, nz_) || 1;
  nx_ /= len; ny_ /= len; nz_ /= len;
  buf.writeFloatLE(nx_, off); off += 4;
  buf.writeFloatLE(ny_, off); off += 4;
  buf.writeFloatLE(nz_, off); off += 4;
  buf.writeFloatLE(ax, off); off += 4;
  buf.writeFloatLE(ay, off); off += 4;
  buf.writeFloatLE(az, off); off += 4;
  buf.writeFloatLE(bx, off); off += 4;
  buf.writeFloatLE(by, off); off += 4;
  buf.writeFloatLE(bz, off); off += 4;
  buf.writeFloatLE(cx, off); off += 4;
  buf.writeFloatLE(cy, off); off += 4;
  buf.writeFloatLE(cz, off); off += 4;
  buf.writeUInt16LE(0, off); off += 2;
}

const tag = `wl${RIPPLE.wavelength}_amp${RIPPLE.amplitude}_step${RIPPLE.step}`;
const outPath = join(__dirname, `prismatica_ripple_${PANEL_W}x${PANEL_H}_${tag}_${THICKNESS}mmbase.stl`);
writeFileSync(outPath, buf);

const sizeMB = (buf.length / 1048576).toFixed(2);
const totalH = THICKNESS + RIPPLE.amplitude;

// Compute the visible peak heights for a quick sanity-check report. Uses the
// same heightAt() that built the geometry so the numbers always match the file.
const peakHeights = [];
const peakLimit = Math.min(halfW, halfH);  // furthest peak that still fits
for (let n = 0; n * RIPPLE.wavelength < peakLimit; n++) {
  const r = n * RIPPLE.wavelength;
  const h = heightAt(r, 0);
  if (h < 0.5) break;
  peakHeights.push(`${h.toFixed(1)}mm`);
}

console.log(`\n✓ wrote ${outPath}`);
console.log(`  ${sizeMB} MB · ${triangles.length.toLocaleString()} triangles`);
console.log(`  bounding box: ${PANEL_W}×${PANEL_H}×${totalH}mm`);
console.log(`  base ${THICKNESS}mm + ${RIPPLE.amplitude}mm peak ripple = ${totalH}mm overall`);
console.log(`  visible peaks (centre outwards): ${peakHeights.join(' → ')}`);
console.log(`  narrowest acrylic (at troughs): ${THICKNESS}mm`);
