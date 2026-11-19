import { test, expect } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const FIXTURES = fileURLToPath(new URL('../.fixtures', import.meta.url));
// Pyodide and its packages come from the CDN on the first run.
const LONG = 180_000;

const rowFor = (page, file) => page.locator('#rows tr', { hasText: file });
const verdictOf = (page, file) => rowFor(page, file).locator('.chip');

test('analyzes a folder and matches parity verdicts', async ({ page }) => {
  await page.goto('/');

  await page.locator('#folder-input').setInputFiles(FIXTURES);
  await expect(page.locator('#results')).toBeVisible();

  const state = page.locator('#state');
  await expect(state).toHaveText('Idle', { timeout: LONG });

  await expect(verdictOf(page, 'fake_mp3.flac')).toHaveText('PROBABLE UPSCALE');
  await expect(verdictOf(page, 'genuine.flac')).toHaveText('GENUINE LOSSLESS');

  await rowFor(page, 'genuine.flac').click();
  const img = page.locator('#inspector img');
  await expect(img).toBeVisible();
  await expect
    .poll(() => img.evaluate((el) => el.naturalWidth), { timeout: 30_000 })
    .toBeGreaterThan(0);
});
