#!/usr/bin/env node
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright');

async function main() {
  const payloadPath = process.argv[2];
  if (!payloadPath) throw new Error('missing payload JSON path');

  const payload = JSON.parse(fs.readFileSync(payloadPath, 'utf8'));
  const appUrl = payload.appUrl || 'http://127.0.0.1:8899/';
  const outputDir = payload.outputDir || path.join(os.homedir(), 'Downloads');
  const timeoutMs = payload.timeoutMs || 60 * 60 * 1000;
  const progressPath = payload.progressPath || null;
  let lastProgress = 0;
  fs.mkdirSync(outputDir, { recursive: true });

  function writeProgress(update) {
    if (!progressPath) return;
    const progress = Number.isFinite(update.progress) ? update.progress : lastProgress;
    lastProgress = progress;
    const data = {
      ok: true,
      state: 'running',
      progress,
      status: 'starting render…',
      updatedAt: new Date().toISOString(),
      ...update,
      progress,
    };
    try {
      fs.writeFileSync(progressPath, JSON.stringify(data, null, 2));
    } catch (_) {}
  }

  function progressFromStatus(status) {
    const text = status || '';
    let m = text.match(/rendering\s+(\d+)\s*\/\s*(\d+)/i);
    if (m) {
      const frame = Number(m[1]);
      const total = Math.max(1, Number(m[2]));
      return Math.max(0, Math.min(0.995, frame / total));
    }
    m = text.match(/(\d+(?:\.\d+)?)%/);
    if (m) return Math.max(0, Math.min(0.995, Number(m[1]) / 100));
    if (/saved/i.test(text)) return 1;
    if (/prepar|start/i.test(text)) return 0.02;
    return null;
  }

  writeProgress({ state: 'starting', progress: 0, status: 'launching dedicated Chrome…' });

  const browser = await chromium.launch({
    channel: 'chrome',
    headless: true,
    args: [
      '--disable-background-timer-throttling',
      '--disable-backgrounding-occluded-windows',
      '--disable-renderer-backgrounding',
      '--disable-features=CalculateNativeWinOcclusion',
      '--autoplay-policy=no-user-gesture-required',
    ],
  });

  const context = await browser.newContext({
    acceptDownloads: true,
    viewport: { width: 1440, height: 1000 },
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  page.setDefaultTimeout(timeoutMs);
  let streamFile = null;
  let streamPath = '';
  let streamBytes = 0;

  function bufferFromBrowserBytes(data) {
    if (Buffer.isBuffer(data)) return data;
    if (typeof data === 'string') return Buffer.from(data, 'base64');
    if (data instanceof ArrayBuffer) return Buffer.from(data);
    if (ArrayBuffer.isView(data)) return Buffer.from(data.buffer, data.byteOffset, data.byteLength);
    if (data && data.type === 'Buffer' && Array.isArray(data.data)) return Buffer.from(data.data);
    if (Array.isArray(data)) return Buffer.from(data);
    throw new Error(`unsupported streamed MP4 chunk type: ${Object.prototype.toString.call(data)}`);
  }

  async function closeStreamFile() {
    if (!streamFile) return;
    const file = streamFile;
    streamFile = null;
    await file.close();
  }

  await page.exposeFunction('__prismaticaMp4Start', async (filename) => {
    await closeStreamFile().catch(() => {});
    const safeName = path.basename(filename || `prismatica_render_${Date.now()}.mp4`);
    streamPath = path.join(outputDir, safeName);
    streamBytes = 0;
    streamFile = await fs.promises.open(streamPath, 'w');
    return { path: streamPath };
  });
  await page.exposeFunction('__prismaticaMp4Write', async (data, position) => {
    if (!streamFile) throw new Error('streamed MP4 write before start');
    const buf = bufferFromBrowserBytes(data);
    const pos = Number(position);
    await streamFile.write(buf, 0, buf.length, Number.isFinite(pos) ? pos : null);
    streamBytes = Math.max(streamBytes, (Number.isFinite(pos) ? pos : 0) + buf.length);
    return { bytes: streamBytes };
  });
  await page.exposeFunction('__prismaticaMp4Finish', async () => {
    await closeStreamFile();
    let bytes = streamBytes;
    try { bytes = fs.statSync(streamPath).size; } catch (_) {}
    return { path: streamPath, bytes };
  });
  await page.exposeFunction('__prismaticaMp4Abort', async () => {
    await closeStreamFile().catch(() => {});
    if (streamPath) await fs.promises.rm(streamPath, { force: true }).catch(() => {});
    return { ok: true };
  });

  const consoleLines = [];
  page.on('console', msg => {
    const line = `[${msg.type()}] ${msg.text()}`;
    consoleLines.push(line);
    if (msg.type() === 'error' || msg.type() === 'warning') {
      process.stderr.write(line + '\n');
    }
  });
  page.on('pageerror', err => {
    const line = `[pageerror] ${err.message}`;
    consoleLines.push(line);
    process.stderr.write(line + '\n');
  });

  let progressTimer = null;
  try {
    await page.goto(`${appUrl}${appUrl.includes('?') ? '&' : '?'}backgroundRender=${Date.now()}`, {
      waitUntil: 'domcontentloaded',
      timeout: 60_000,
    });
    await page.waitForFunction(() => !!window.__prismatica && !!window.__prismatica.renderOffline, null, {
      timeout: 60_000,
    });
    writeProgress({ state: 'starting', progress: 0.01, status: 'Prismatica loaded…' });

    await page.evaluate(({ state, sequence }) => {
      const api = window.__prismatica;
      if (state) api.applyPreset(state);
      if (Array.isArray(sequence)) api.setShotSequence(sequence);
      window.__prismaticaBackgroundMp4 = {
        start: window.__prismaticaMp4Start,
        write: window.__prismaticaMp4Write,
        finish: window.__prismaticaMp4Finish,
        abort: window.__prismaticaMp4Abort,
      };
      api.guiRefresh();
    }, {
      state: payload.state || {},
      sequence: payload.sequence || [],
    });
    writeProgress({ state: 'running', progress: 0.02, status: 'render starting…' });

    const opts = payload.opts || { sequence: true };
    progressTimer = setInterval(async () => {
      try {
        const status = await page.locator('#status').textContent({ timeout: 250 }).catch(() => '');
        const pct = progressFromStatus(status);
        const update = {
          state: 'running',
          status: status || 'rendering…',
        };
        if (pct != null) update.progress = pct;
        writeProgress(update);
      } catch (_) {}
    }, 1000);
    let downloadError = null;
    const downloadPromise = page.waitForEvent('download', { timeout: timeoutMs })
      .then(download => ({ ok: true, download }))
      .catch(error => {
        downloadError = error;
        return { ok: false, error };
    });
    const renderPromise = page.evaluate(renderOpts => window.__prismatica.renderOffline(renderOpts), opts);
    const renderResult = await renderPromise;
    let suggested = renderResult && renderResult.filename;
    let savePath = renderResult && renderResult.path;
    if (renderResult && renderResult.direct && renderResult.path) {
      // Dedicated/background renders stream MP4 chunks straight to disk through
      // the exposed Node writer, so there is intentionally no browser download
      // event to wait for here.
    } else {
    const downloadResult = await Promise.race([
      downloadPromise,
      new Promise(resolve => setTimeout(() => resolve({ ok: false, error: null }), 15_000)),
    ]);
    if (!downloadResult.ok || !downloadResult.download) {
      const status = await page.locator('#status').textContent({ timeout: 1000 }).catch(() => '');
      const returnedName = renderResult && renderResult.filename ? ` App reported: ${renderResult.filename}.` : '';
      const downloadMsg = downloadError ? ` Download watcher: ${downloadError.message || downloadError}.` : '';
      throw new Error(`render completed but the browser download could not be captured. Status: ${status || 'unknown'}.${returnedName}${downloadMsg}`);
    }

    const download = downloadResult.download;
    suggested = download.suggestedFilename() || suggested || `prismatica_render_${Date.now()}`;
    savePath = path.join(outputDir, suggested);
    await download.saveAs(savePath);
    }
    suggested = suggested || path.basename(savePath || `prismatica_render_${Date.now()}.mp4`);

    const status = await page.locator('#status').textContent({ timeout: 1000 }).catch(() => '');
    if (progressTimer) clearInterval(progressTimer);
    writeProgress({
      state: 'done',
      progress: 1,
      status: status || `saved: ${suggested}`,
      path: savePath,
      filename: suggested,
      renderResult,
      console: consoleLines.slice(-40),
    });
    await browser.close();
    process.stdout.write(JSON.stringify({
      ok: true,
      path: savePath,
      filename: suggested,
      status,
      renderResult,
      console: consoleLines.slice(-40),
    }) + '\n');
  } catch (err) {
    if (progressTimer) clearInterval(progressTimer);
    await closeStreamFile().catch(() => {});
    writeProgress({
      ok: false,
      state: 'error',
      progress: 0,
      status: 'render failed',
      error: err.message || String(err),
      console: consoleLines.slice(-40),
    });
    await browser.close().catch(() => {});
    err.message = `${err.message}\n${consoleLines.slice(-20).join('\n')}`;
    throw err;
  }
}

main().catch(err => {
  process.stderr.write((err && err.stack) ? err.stack : String(err));
  process.stderr.write('\n');
  process.exit(1);
});
