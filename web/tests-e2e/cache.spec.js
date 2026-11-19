import { test, expect } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const FIXTURES = fileURLToPath(new URL('../.fixtures', import.meta.url));
const LONG = 180_000;
const REMOTE = /^(cdn\.jsdelivr\.net|pypi\.org|files\.pythonhosted\.org)$/;

async function analyzeOne(page) {
  await page.locator('#folder-input').setInputFiles(FIXTURES);
  await expect(page.locator('#state')).toHaveText('Idle', { timeout: LONG });
  await expect(page.locator('#rows tr', { hasText: 'genuine.flac' }).locator('.chip'))
    .toHaveText('GENUINE LOSSLESS');
}

test('second load makes no requests to the CDN or PyPI', async ({ page, context }) => {
  await page.goto('/');
  await analyzeOne(page);

  // Page and worker requests answered by the service worker are served locally;
  // anything else to these hosts went to the network.
  const remote = [];
  let local = 0;
  context.on('response', (res) => {
    if (!REMOTE.test(new URL(res.url()).hostname)) return;
    if (res.fromServiceWorker()) local++; else remote.push(res.url());
  });

  await page.reload();
  expect(await page.evaluate(() => !!navigator.serviceWorker.controller)).toBe(true);
  await analyzeOne(page);
  expect(remote).toEqual([]);
  expect(local).toBeGreaterThan(10); // workers' fetches are intercepted
});
