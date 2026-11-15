/* Run against tests.e2e.acquisition_preview only; see docs/ACQUISITION-UI-AUDIT.md. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const base = process.env.AUDIT_BASE_URL || 'http://127.0.0.1:8018';
const output = process.env.AUDIT_OUTPUT_DIR || '/tmp/audioreap-acquisition-audit/verified';
const widths = (process.env.AUDIT_WIDTHS || '320,360,390,640,768,1024,1440').split(',').map(Number);

async function checkLayout(page, label) {
  const issues = await page.evaluate(() => {
    const problems = [];
    if (document.documentElement.scrollWidth > innerWidth + 1) problems.push('horizontal page overflow');
    const selectors = '.card-title, .card-sub, .album-batch-title, .job-verdict > span, .job-source-title, .acquire-artist-info, .jump-main, .jump-sub, .rv-comparison-side > strong';
    document.querySelectorAll(selectors).forEach(el => {
      if (!el.getClientRects().length) return;
      if (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1)
        problems.push(`${el.className || el.tagName}: ${el.textContent.trim().slice(0, 70)}`);
    });
    return problems;
  });
  assert.deepEqual(issues, [], label);
}

async function snapshot(page, name, fullPage = false) {
  if (fullPage) await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(output, `${name}.png`), fullPage, animations: 'disabled' });
}

async function scenario(browser, width, theme) {
  const label = `${width}-${theme}`;
  const context = await browser.newContext({ viewport: { width, height: 900 },
    colorScheme: theme, isMobile: width < 640, hasTouch: width < 640, serviceWorkers: 'block' });
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    return url.origin === new URL(base).origin ? route.continue() : route.fulfill({ status: 404, body: '' });
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response => { if (response.status() >= 500) errors.push(`${response.status()} ${response.url()}`); });
  await page.goto(`${base}/acquire?q=Daft%20Punk`);
  await page.locator('#search-artists .acquire-artist').first().waitFor();
  await page.locator('#cloud-results .search-track-card').first().waitFor();
  await page.locator('#local-results .search-library-card').first().waitFor();
  assert.equal(await page.locator('#search-artists .acquire-artist').count(), 8);
  assert.equal(await page.locator('#search-artists .acquire-artist:visible').count(), 3);
  await checkLayout(page, `${label} unified search`);
  await snapshot(page, `search-${label}`, width === 390 || width === 1440);
  await page.locator('#search-artists summary').click();
  assert.equal(await page.locator('#search-artists .acquire-artist:visible').count(), 8);
  await checkLayout(page, `${label} all artist matches`);
  await page.locator('#search-artists .acquire-artist').first().click();
  await page.locator('#search-discography').waitFor();
  await page.getByRole('button', { name: 'Show tracks', exact: true }).click();
  await page.locator('.release-tracklist').waitFor();
  await page.getByRole('button', { name: 'Check source', exact: true }).first().click();
  await page.locator('#ytsrc-recording-2').waitFor();
  await checkLayout(page, `${label} discography and tracklist`);
  await snapshot(page, `discography-${label}`, width === 390 || width === 1440);
  await page.getByRole('link', { name: '← Search results' }).click();
  await page.locator('#search-artists .acquire-artist').first().waitFor();
  assert.equal(await page.locator('#q').inputValue(), 'Daft Punk');
  await page.getByRole('tab', { name: 'Discover', exact: true }).click();
  await page.locator('#disco-q').fill('Daft Punk');
  await page.locator('#artist-candidates .acquire-artist').first().waitFor();
  assert.equal(await page.locator('#artist-candidates .acquire-artist').count(), 8);
  assert.match(await page.locator('#artist-candidates .acquire-artist').first().innerText(), /100% match/);
  await checkLayout(page, `${label} Discover artist matches`);

  // The real command palette, including focus trapping and collapsed results.
  await page.locator('#jump-btn').click();
  await page.locator('#jump-input').fill('Daft Punk');
  await page.locator('#jump-results .acquire-artist').first().waitFor();
  await page.locator('#jump-results .jump-item-action').first().waitFor();
  await checkLayout(page, `${label} global search`);
  await snapshot(page, `palette-${label}`);
  await page.locator('#jump-input').focus();
  await page.keyboard.press('Shift+Tab');
  assert.equal(await page.locator('#jump-palette').evaluate(el => el.contains(document.activeElement)), true);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#jump-palette').isVisible(), false);
  assert.equal(await page.locator('#jump-btn').evaluate(el => el === document.activeElement), true);

  await page.goto(`${base}/jobs?view=review`);
  await page.locator('#job-demo-flagged [data-review-open]').waitFor();
  assert.match(await page.locator('#job-demo-flagged .card-title').innerText(), /Extended Album Version/);
  assert.match(await page.locator('#job-demo-flagged .job-verdict').innerText(), /expected 3:43, downloaded 5:27/);
  assert.match(await page.locator('#job-demo-verified .job-verdict').innerText(), /Strong match/);
  await checkLayout(page, `${label} review queue`);
  assert.equal(await page.locator('#album-group-demo-alb .job-card').count(), 2);
  await snapshot(page, `jobs-${label}`, width === 390 || width === 1440);
  await page.locator('#job-demo-flagged .job-check').check();
  await page.locator('#batch-toolbar').getByRole('button', { name: 'Approve', exact: true }).click();
  const dialog = page.locator('#confirm-dialog');
  await dialog.waitFor();
  const box = await dialog.boundingBox();
  assert.ok(Math.abs(box.x + box.width / 2 - width / 2) < 2, `${label} horizontal dialog centering`);
  assert.ok(Math.abs(box.y + box.height / 2 - 450) < 2, `${label} vertical dialog centering`);
  await snapshot(page, `confirm-${label}`);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('#job-demo-flagged .job-check').isChecked(), true);
  await page.locator('#job-demo-flagged').evaluate(el => { el.dataset.auditOld = 'true'; });
  await page.getByRole('button', { name: 'Refresh', exact: true }).click();
  await page.waitForFunction(() => !document.querySelector('#job-demo-flagged[data-audit-old]') &&
    document.querySelector('#job-demo-flagged.job-card--selected .job-check:checked'));

  await page.locator('#job-demo-flagged [data-review-open]').click();
  await page.locator('#job-demo-flagged.card-review').waitFor();
  await page.locator('#job-demo-flagged [name=title]').fill('Reviewed title — edited before saving');
  assert.equal(await page.evaluate(() => {
    const list = document.getElementById('job-list');
    const event = new CustomEvent('htmx:beforeSwap', { bubbles: true, cancelable: true,
      detail: { target: list, requestConfig: { elt: document.querySelector('[data-jobs-refresh]') } } });
    return !list.dispatchEvent(event);
  }), true, `${label} late refresh cannot replace an open review`);
  // Trigger the periodic poll explicitly; it must leave this edit untouched.
  assert.equal(await page.evaluate(() => {
    const list = document.getElementById('job-list');
    const event = new CustomEvent('htmx:beforeRequest', { bubbles: true, cancelable: true, detail: { elt: list } });
    return !list.dispatchEvent(event);
  }), true, `${label} refresh is prevented while editing`);
  assert.equal(await page.locator('#job-demo-flagged [name=title]').inputValue(), 'Reviewed title — edited before saving');
  await checkLayout(page, `${label} expanded review`);
  await snapshot(page, `review-${label}`, width === 390 || width === 1440);
  await page.locator('#job-demo-flagged .rv-actions').scrollIntoViewIfNeeded();
  await snapshot(page, `review-actions-${label}`);
  await page.locator('#job-demo-flagged .rv-reject').click();
  await dialog.waitFor();
  assert.match(await page.locator('#confirm-dialog-accept').innerText(), /Trash/);
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  // Observe the real form submission without placing/tagging audio in this UI audit.
  let submitted;
  await page.route('**/jobs/demo-flagged/approve', async route => {
    submitted = new URLSearchParams(route.request().postData());
    await route.fulfill({ status: 200, contentType: 'text/html', body: '<div id="audit-saved">Saved</div>' });
  });
  await page.locator('#job-demo-flagged .rv-approve-next').click();
  await page.locator('#audit-saved').waitFor();
  assert.equal(submitted.get('title'), 'Reviewed title — edited before saving');
  assert.equal(submitted.get('advance'), 'next');
  await page.goto(`${base}/jobs?view=failed`);
  await checkLayout(page, `${label} failed jobs`);
  await page.goto(`${base}/acquire?q=Daft%20Punk`);
  await page.locator('#cloud-results .search-track-card').first().waitFor();
  await page.locator('#cloud-results .search-track-card').first().getByRole('button', { name: /^Get / }).click();
  await page.locator('#receipt-demo-acquired').waitFor();
  await checkLayout(page, `${label} acquisition receipt`);
  await page.locator('#receipt-demo-acquired [data-acquire-jobs-link]').click();
  await page.locator('#job-demo-acquired').waitFor();
  await page.locator('#jobs-back-to-acquire').waitFor();
  assert.match(await page.locator('#jobs-back-to-acquire').getAttribute('href'), /Daft/);
  assert.deepEqual(errors, [], `${label} browser errors`);
  await context.close();
  console.log(`PASS ${label}: search, artists, releases, global palette, queue, selection, dialog, review and receipt`);
}

(async () => {
  let marker;
  for (let attempt = 0; attempt < 30; attempt++) {
    try { marker = await fetch(`${base}/__audit__`).then(r => r.json()); break; }
    catch (error) { if (attempt === 29) throw error; await new Promise(resolve => setTimeout(resolve, 200)); }
  }
  assert.equal(marker.isolated_acquisition_fixture, true, 'Only run against the isolated fixture server');
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ headless: true,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) });
  try {
    for (const width of widths) for (const theme of ['light', 'dark']) await scenario(browser, width, theme);
  } finally { await browser.close(); }
  console.log(`Screenshots: ${output}`);
})().catch(error => { console.error(error); process.exitCode = 1; });
