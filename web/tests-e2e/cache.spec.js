import { test, expect } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const FIXTURES = fileURLToPath(new URL('../.fixtures', import.meta.url));
const LONG = 180_000;
const REMOTE = /^(cdn\.jsdelivr\.net|pypi\.org|files\.pythonhosted\.org)$/;

test.skip(({ browserName }) => browserName !== 'chromium', 'service worker network events are Chromium-only');

async function analyzeOne(page) {
  await page.locator('#folder-input').setInputFiles(FIXTURES);
  await expect(page.locator('#state')).toHaveText('Idle', { timeout: LONG });
  await expect(page.locator('#rows tr', { hasText: 'genuine.flac' }).locator('.chip'))
    .toHaveText('GENUINE LOSSLESS');
}

test('second load makes no requests to the CDN or PyPI', async ({ page, context }) => {
  // Requests the service worker itself sent to the network, and responses it served locally.
  let network = [];
  let local = 0;
  context.on('request', (req) => {
    if (REMOTE.test(new URL(req.url()).hostname) && req.serviceWorker()) network.push(req.url());
  });
  context.on('response', (res) => {
    if (REMOTE.test(new URL(res.url()).hostname) && res.fromServiceWorker()) local++;
  });

  await page.goto('/');
  await analyzeOne(page);
  // The channel works: a cold load reaches the network through the service worker.
  expect(network.length).toBeGreaterThan(0);

  network = [];
  local = 0;
  await page.reload();
  expect(await page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
  await analyzeOne(page);
  expect(network).toEqual([]);
  expect(local).toBeGreaterThan(10); // page and worker fetches are served by the service worker
});
