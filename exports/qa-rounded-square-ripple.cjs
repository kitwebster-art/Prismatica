#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright');

async function main() {
  const url = process.argv[2] || 'http://127.0.0.1:8899/';
  const outputDir = path.resolve(process.argv[3] || 'exports/rounded-square-ripple');
  fs.mkdirSync(outputDir, { recursive: true });

  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));

  await page.goto(url, { waitUntil: 'networkidle', timeout: 120000 });
  await page.waitForFunction(() => !!window.__prismatica?.LENS_TYPES?.rounded_square_ripple, null, { timeout: 120000 });

  const report = await page.evaluate(async () => {
    const p = window.__prismatica;
    const presets = JSON.parse(localStorage.getItem('prismatica.presets.v2') || '{}');
    const presetName = 'square lens · rounded concentric';
    const preset = presets[presetName];
    if (!preset) throw new Error(`Bundled preset missing: ${presetName}`);

    p.applyPreset({
      ...preset,
      humanScaleFigure: false,
      showDimensions: false,
      showSectionProfile: false,
      presentationMode: false,
      lensHidden: false,
      shapeView: false,
    });
    p.state.currentPreset = presetName;
    p.camera.position.set(155, 70, 880);
    p.controls.target.set(0, 0, -12);
    p.controls.update();
    await new Promise((resolve) => setTimeout(resolve, 2800));

    const type = p.LENS_TYPES.rounded_square_ripple;
    const height = type.fn(p.state.params, p.state.panelWidth, p.state.panelHeight);
    const pairs = [
      [height(90, 0), height(0, 90)],
      [height(120, 45), height(45, 120)],
      [height(170, 90), height(90, 170)],
    ];
    let min = Infinity;
    let max = -Infinity;
    for (let y = -240; y <= 240; y += 12) {
      for (let x = -240; x <= 240; x += 12) {
        const value = height(x, y);
        min = Math.min(min, value);
        max = Math.max(max, value);
      }
    }

    return {
      presetName,
      typeLabel: type.label,
      panel: [p.state.panelWidth, p.state.panelHeight],
      panelDistance: p.state.panelDistance,
      panelMode: p.state.panelMode,
      superellipsePower: p.state.params.superellipse_power,
      heightRange: [min, max],
      symmetryError: Math.max(...pairs.map(([a, b]) => Math.abs(a - b))),
      vertexCount: p.lensVertexCount(),
      lensTypeOptionVisible: document.body.textContent.includes('Rounded Square Ripple'),
      presetOptionVisible: document.body.textContent.includes(presetName),
      renderMetrics: {
        renderer: p.renderer.info.render,
        memory: p.renderer.info.memory,
      },
    };
  });

  await page.screenshot({ path: path.join(outputDir, 'final-glass.png'), fullPage: true });

  await page.evaluate(async () => {
    const p = window.__prismatica;
    p.state.shapeView = true;
    p.state.lensHidden = false;
    p.rebuildLenses();
    p.camera.position.set(0, 0, 820);
    p.controls.target.set(0, 0, 0);
    p.controls.update();
    await new Promise((resolve) => setTimeout(resolve, 1800));
  });
  await page.screenshot({ path: path.join(outputDir, 'shape-field.png'), fullPage: true });

  await page.evaluate(async () => {
    const p = window.__prismatica;
    const presets = JSON.parse(localStorage.getItem('prismatica.presets.v2') || '{}');
    p.applyPreset({
      ...presets['square lens · rounded concentric'],
      humanScaleFigure: false,
      lensHidden: true,
      shapeView: false,
    });
    p.scene.traverse((object) => {
      if (object.material === p.ledMat) {
        object.visible = true;
        object.position.z = 2;
      }
    });
    p.camera.position.set(155, 70, 880);
    p.controls.target.set(0, 0, -12);
    p.controls.update();
    await new Promise((resolve) => setTimeout(resolve, 1400));
  });
  await page.screenshot({ path: path.join(outputDir, 'raw-panel-baseline.png'), fullPage: true });

  await browser.close();
  if (errors.length) throw new Error(`Browser errors:\n${errors.join('\n')}`);
  if (report.typeLabel !== 'Rounded Square Ripple') throw new Error(`Unexpected label: ${report.typeLabel}`);
  if (!report.lensTypeOptionVisible || !report.presetOptionVisible) throw new Error(`Preset UI not visible: ${JSON.stringify(report)}`);
  if (report.panelDistance !== 40 || report.panelMode !== 4) throw new Error(`Preset state mismatch: ${JSON.stringify(report)}`);
  if (report.symmetryError > 1e-6) throw new Error(`Square field symmetry failed: ${JSON.stringify(report)}`);
  if (!(report.heightRange[0] >= 0 && report.heightRange[1] > 14)) throw new Error(`Height range failed: ${JSON.stringify(report)}`);
  if (report.vertexCount < 100000) throw new Error(`Lens geometry too coarse: ${JSON.stringify(report)}`);
  process.stdout.write(`${JSON.stringify({ ok: true, outputDir, ...report }, null, 2)}\n`);
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exit(1);
});
