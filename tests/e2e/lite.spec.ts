import { test, expect } from '@playwright/test';

test('lightweight page sends text without graphical assets and fits a phone', async ({ page }) => {
  const errors: string[] = [];
  const assets: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('request', request => assets.push(request.url()));
  await page.route('**/api/say', async route => {
    expect(route.request().postDataJSON()).toEqual({ text: 'Hello Hermes' });
    await route.fulfill({ status: 200, contentType: 'audio/wav',
      headers: { 'X-User-Transcript': 'Hello%20Hermes', 'X-Hal-Transcript': 'Hello%2C%20Dave.' },
      body: Buffer.alloc(0) });
  });
  await page.setViewportSize({ width: 393, height: 851 });
  await page.goto('/lite');
  await expect(page).toHaveTitle('Hermes Hal · Voice');
  await expect(page.getByRole('heading', { name: 'Hermes Hal' })).toBeVisible();
  await page.getByLabel('Message', { exact: true }).fill('Hello Hermes');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('log')).toContainText('Hermes Hal: Hello, Dave.');
  await page.getByRole('button', { name: 'Stop playback' }).click();
  await expect(page.locator('#activity')).toHaveText('Playback stopped. Agent work continues.');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(assets.some(url => /\/assets\/|three|optic/.test(url))).toBe(false);
  expect(errors).toEqual([]);
  await page.screenshot({ path: '/tmp/hermes-hal-lite-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.screenshot({ path: '/tmp/hermes-hal-lite-desktop.png', fullPage: true });
});

test('microphone releases tracks and sends recorded audio', async ({ page }) => {
  await page.route('**/api/talk', route => route.fulfill({ status: 204 }));
  await page.goto('/lite');
  await page.getByRole('button', { name: 'Record message' }).click();
  await expect(page.getByRole('button', { name: 'Finish recording' })).toBeVisible();
  const sent = page.waitForRequest('**/api/talk');
  await page.getByRole('button', { name: 'Finish recording' }).click();
  expect((await sent).headers()['content-type']).toContain('multipart/form-data');
  await expect(page.locator('#activity')).toContainText('No speech detected');
  await expect(page.getByRole('button', { name: 'Record message' })).toBeEnabled();
});

test('permission can be denied while a turn is in progress', async ({ page }) => {
  await page.addInitScript(() => {
    class MockEvents {
      onmessage: ((event: { data: string }) => void) | null = null;
      constructor() {
        window.addEventListener('test-permission', () => this.onmessage?.({
          data: JSON.stringify({ type: 'permission_request', request_id: 'test-permission',
            title: 'Read file', timeout: 30 }),
        }));
      }
      close() {}
    }
    Object.defineProperty(window, 'EventSource', { value: MockEvents });
  });
  let finishTurn!: () => void;
  const pending = new Promise<void>(resolve => { finishTurn = resolve; });
  await page.route('**/api/say', async route => {
    await pending;
    await route.fulfill({ status: 204 });
  });
  await page.route('**/api/permission/test-permission', async route => {
    expect(route.request().postDataJSON()).toEqual({ decision: 'deny' });
    await route.fulfill({ json: { ok: true } });
  });
  await page.goto('/lite');
  await page.getByLabel('Message', { exact: true }).fill('Read my file');
  await page.getByRole('button', { name: 'Send', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  await page.evaluate(() => window.dispatchEvent(new Event('test-permission')));
  await expect(page.getByText('Allow tool: Read file?')).toBeVisible();
  await page.getByRole('button', { name: 'Deny', exact: true }).click();
  await expect(page.locator('#activity')).toHaveText('Permission denied.');
  await expect(page.locator('#permission')).toBeHidden();
  finishTurn();
});
