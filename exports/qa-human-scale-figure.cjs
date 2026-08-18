#!/usr/bin/env node
const { chromium } = require('playwright');

async function main() {
  const url = process.argv[2] || 'http://127.0.0.1:8899/';
  const screenshotPath = process.argv[3] || '/tmp/prismatica-human-scale-figure.png';
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1100 }, deviceScaleFactor: 1 });
  const errors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));

  await page.goto(url, { waitUntil: 'networkidle', timeout: 120000 });
  await page.waitForFunction(
    () => window.__prismatica?.humanScaleFigureAsset?.loaded === true,
    null,
    { timeout: 120000 },
  );
  await page.waitForTimeout(1200);

  const report = await page.evaluate(() => {
    const p = window.__prismatica;
    const figure = p.humanScaleFigure;
    const bounds = p.humanScaleFigureBounds();
    const model = figure.getObjectByName('rocketbox-standing-gallery-visitor');
    return {
      asset: { ...p.humanScaleFigureAsset },
      visible: figure.visible,
      modelPresent: !!model,
      fallbackPresent: !!figure.getObjectByName('procedural-human-scale-fallback'),
      boundsMm: bounds.size,
      modelPositionY: model?.position.y ?? null,
    };
  });

  await page.screenshot({ path: screenshotPath, fullPage: true });
  await browser.close();

  if (errors.length) throw new Error(`Browser errors:\n${errors.join('\n')}`);
  if (!report.visible || !report.modelPresent || report.asset.fallback || report.fallbackPresent) {
    throw new Error(`Realistic visitor did not replace fallback: ${JSON.stringify(report)}`);
  }
  if (report.asset.meshCount < 1 || report.asset.vertexCount < 1000) {
    throw new Error(`Visitor geometry is unexpectedly coarse: ${JSON.stringify(report)}`);
  }
  if (report.boundsMm[1] < 1740 || report.boundsMm[1] > 1765) {
    throw new Error(`Visitor is not at the intended 1.75 m scale: ${JSON.stringify(report)}`);
  }
  process.stdout.write(`${JSON.stringify({ ok: true, screenshotPath, ...report }, null, 2)}\n`);
}

main().catch((error) => {
  console.error(error.stack || error.message || error);
  process.exit(1);
});
