#!/usr/bin/env node
const { chromium } = require('playwright');

async function main() {
  const url = process.argv[2] || 'http://127.0.0.1:8899/';
  const screenshotPath = process.argv[3] || '/tmp/prismatica-reflective-assembly.png';
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(url, { waitUntil: 'networkidle', timeout: 120000 });
  await page.waitForFunction(() => !!window.__prismatica?.reflectiveEdge, null, { timeout: 120000 });
  const report = await page.evaluate(async () => {
    const p = window.__prismatica;
    Object.assign(p.state, {
      lensShape: 'round',
      panelShape: 'round',
      panelWidth: 500,
      panelHeight: 500,
      panelDistance: 30,
      reflectiveEdgeEnabled: true,
      reflectiveEdgeReveal: 10,
      reflectiveEdgeRoughness: 0.055,
      reflectiveEdgeLights: true,
      reflectiveEdgeLightIntensity: 1.35,
      humanScaleFigure: false,
      envPreset: 'product',
      envIntensity: 1.35,
    });
    p.rebuildLenses();
    p.guiRefresh();
    p.camera.position.set(330, 170, 760);
    p.controls.target.set(0, 0, -20);
    p.controls.update();
    await new Promise((resolve) => setTimeout(resolve, 2500));
    const positionCount = p.reflectiveEdge.geometry?.attributes?.position?.count || 0;
    return {
      edgeVisible: p.reflectiveEdge.visible,
      edgePositionCount: positionCount,
      edgeMaterial: {
        metalness: p.reflectiveEdge.material.metalness,
        roughness: p.reflectiveEdge.material.roughness,
      },
      selectedParts: p.selectedFabricationParts(),
      scope: p.state.assemblyExportInfo,
      controlsPresent: document.body.textContent.includes('Reflective edge') &&
        document.body.textContent.includes('Fabricator 3D model'),
    };
  });
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await browser.close();
  if (errors.length) throw new Error(`Browser errors:\n${errors.join('\n')}`);
  if (!report.edgeVisible || report.edgePositionCount < 12 || !report.controlsPresent) {
    throw new Error(`Reflective assembly QA failed: ${JSON.stringify(report)}`);
  }
  process.stdout.write(`${JSON.stringify({ ok: true, screenshotPath, ...report }, null, 2)}\n`);
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exit(1);
});
