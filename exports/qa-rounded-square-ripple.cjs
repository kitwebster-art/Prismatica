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
    p.setAssemblyView('compare');
    const comparePreflight = p.fabricationExportPreflight(true);
    p.setAssemblyView('square');
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

    const samplePoints = [[0, 0], [45, 0], [90, 45], [120, 120], [200, 150], [240, 240]];
    const heightSamples = samplePoints.map(([x, y]) => [x, y, height(x, y)]);
    const balancedVertexCount = p.lensVertexCount();
    p.state.renderQuality = 'high';
    p.rebuildLenses();
    const highVertexCount = p.lensVertexCount();
    p.state.renderQuality = 'balanced';
    p.rebuildLenses();

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
      balancedVertexCount,
      highVertexCount,
      vertexReduction: 1 - (balancedVertexCount / highVertexCount),
      heightSamples,
      lensTypeOptionVisible: document.body.textContent.includes('Rounded Square Ripple'),
      presetOptionVisible: document.body.textContent.includes(presetName),
      renderMetrics: {
        renderer: p.renderer.info.render,
        memory: p.renderer.info.memory,
      },
      assemblyView: p.state.assemblyView,
      lensShape: p.state.lensShape,
      panelShape: p.state.panelShape,
      shapeCompare: p.state.shapeCompare,
      activeLensResolution: p.activeLensResolution(),
      livePostProcessingRequired: p.livePostProcessingRequired(),
      fabricationPreflight: p.fabricationExportPreflight(true),
      comparePreflight,
      squareOnlyControlVisible: document.body.textContent.includes('assembly view') &&
        document.body.textContent.includes('square only'),
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

  const migrationContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const migrationPage = await migrationContext.newPage();
  const migrationErrors = [];
  migrationPage.on('console', (message) => {
    if (message.type() === 'error') migrationErrors.push(message.text());
  });
  migrationPage.on('pageerror', (error) => migrationErrors.push(error.message));
  await migrationPage.addInitScript((legacyDefaults) => {
    const key = 'prismatica.defaults.v1';
    if (!localStorage.getItem(key)) localStorage.setItem(key, JSON.stringify(legacyDefaults));
  }, {
    lensType: 'sine_wave_lens',
    lensShape: 'round',
    panelShape: 'round',
    shapeCompare: true,
    shapeCompareLayout: 'top_bottom',
  });
  await migrationPage.goto(url, { waitUntil: 'networkidle', timeout: 120000 });
  await migrationPage.waitForFunction(() => !!window.__prismatica, null, { timeout: 120000 });
  const legacyMigration = await migrationPage.evaluate(() => {
    const p = window.__prismatica;
    const stored = JSON.parse(localStorage.getItem('prismatica.defaults.v1') || '{}');
    return {
      assemblyView: p.state.assemblyView,
      shapeCompare: p.state.shapeCompare,
      lensShape: p.state.lensShape,
      panelShape: p.state.panelShape,
      storedAssemblyView: stored.assemblyView,
      storedSchemaVersion: stored._defaultsSchemaVersion,
    };
  });
  await migrationPage.evaluate(() => {
    const p = window.__prismatica;
    p.setAssemblyView('compare');
    p.state.saveAsDefaults();
  });
  await migrationPage.reload({ waitUntil: 'networkidle', timeout: 120000 });
  await migrationPage.waitForFunction(() => !!window.__prismatica, null, { timeout: 120000 });
  const modernComparison = await migrationPage.evaluate(() => ({
    assemblyView: window.__prismatica.state.assemblyView,
    shapeCompare: window.__prismatica.state.shapeCompare,
  }));
  await migrationContext.close();

  await browser.close();
  if (errors.length) throw new Error(`Browser errors:\n${errors.join('\n')}`);
  if (migrationErrors.length) throw new Error(`Migration browser errors:\n${migrationErrors.join('\n')}`);
  if (report.typeLabel !== 'Rounded Square Ripple') throw new Error(`Unexpected label: ${report.typeLabel}`);
  if (!report.lensTypeOptionVisible || !report.presetOptionVisible) throw new Error(`Preset UI not visible: ${JSON.stringify(report)}`);
  if (!report.squareOnlyControlVisible || report.assemblyView !== 'square' || report.lensShape !== 'standard' || report.panelShape !== 'rectangular' || report.shapeCompare) {
    throw new Error(`Square-only assembly mode failed: ${JSON.stringify(report)}`);
  }
  if (report.fabricationPreflight) throw new Error(`Square fabrication preflight failed: ${JSON.stringify(report)}`);
  if (!report.comparePreflight.includes('Choose square only or round only')) throw new Error(`Comparison export guard failed: ${JSON.stringify(report)}`);
  if (report.activeLensResolution !== 2) throw new Error(`Balanced live mesh optimisation failed: ${JSON.stringify(report)}`);
  if (report.livePostProcessingRequired) throw new Error(`Neutral live view should bypass post-processing: ${JSON.stringify(report)}`);
  if (!(report.vertexReduction > 0.4)) throw new Error(`Live mesh vertex reduction is too small: ${JSON.stringify(report)}`);
  if (report.panelDistance !== 40 || report.panelMode !== 4) throw new Error(`Preset state mismatch: ${JSON.stringify(report)}`);
  if (report.symmetryError > 1e-6) throw new Error(`Square field symmetry failed: ${JSON.stringify(report)}`);
  if (!(report.heightRange[0] >= 0 && report.heightRange[1] > 14)) throw new Error(`Height range failed: ${JSON.stringify(report)}`);
  if (report.vertexCount < 100000) throw new Error(`Lens geometry too coarse: ${JSON.stringify(report)}`);
  if (legacyMigration.assemblyView !== 'square' || legacyMigration.shapeCompare || legacyMigration.lensShape !== 'standard' || legacyMigration.panelShape !== 'rectangular' || legacyMigration.storedAssemblyView !== 'square' || legacyMigration.storedSchemaVersion !== 2) {
    throw new Error(`Legacy defaults migration failed: ${JSON.stringify(legacyMigration)}`);
  }
  if (modernComparison.assemblyView !== 'compare' || !modernComparison.shapeCompare) {
    throw new Error(`Explicit modern comparison default was not preserved: ${JSON.stringify(modernComparison)}`);
  }
  process.stdout.write(`${JSON.stringify({ ok: true, outputDir, ...report, legacyMigration, modernComparison }, null, 2)}\n`);
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exit(1);
});
