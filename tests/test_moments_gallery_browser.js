// Real Chromium regression for the gallery's click, overlay and facts behavior.
// Provision: npm install --prefix <tmp> playwright@1.63.0; NODE_PATH=<tmp>/node_modules
//            npx playwright install chromium
const assert = require('node:assert/strict');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const { chromium } = require('playwright');

(async () => {
  const renderer = path.resolve(__dirname, '../.github/actions/moments-gallery/moments_gallery.py');
  const setup = `import importlib.util, os, pathlib
p=pathlib.Path(${JSON.stringify(renderer)})
s=importlib.util.spec_from_file_location('gallery',p)
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
r=m._make_self_test_root()
os.environ.update(GALLERY_MANIFEST='manifest.json',GALLERY_ROOT=str(r),GALLERY_NAME='browser',GALLERY_SUMMARY_THUMBNAILS='0')
assert m._main_impl()==0
print(r/'browser-gallery'/'index.html')`;
  const generated = spawnSync('python3', ['-c', setup], { encoding: 'utf8' });
  assert.equal(generated.status, 0, generated.stderr);
  const htmlPath = generated.stdout.trim().split('\n').at(-1);
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(String(error)));
    await page.goto('file://' + htmlPath);
    const warm = page.locator('.scenario[data-scenario="warmup"]');
    const normal = page.locator('.scenario[data-scenario="normal"]');
    assert.equal(await warm.locator('.facts-pane:not([hidden])').count(), 1);
    assert.equal(await normal.locator('.facts-pane:not([hidden])').count(), 1);
    assert.match(await warm.locator('.facts-pane:not([hidden])').innerText(), /Recording/);
    assert.match(await normal.locator('.facts-pane:not([hidden])').innerText(), /fox/);
    assert.equal(await normal.locator('.chip').count(), 2);
    assert.equal(await normal.locator('audio').count(), 1);

    await warm.locator('.frame').nth(0).click(); // one click, not double-click
    assert.equal(await page.locator('body.enlarged').count(), 1);
    assert.match(await page.locator('#enlarged-img').getAttribute('src'), /warmup\/0000.png$/);
    assert.equal(await page.locator('#enlarged-close').evaluate(el => el === document.activeElement), true);
    await page.keyboard.press('ArrowRight');
    assert.match(await page.locator('#enlarged-img').getAttribute('src'), /warmup\/0001.png$/);
    assert.equal(await warm.locator('.frame[aria-current="true"]').count(), 1);
    assert.equal(await warm.locator('.facts-pane:not([hidden])').count(), 1);
    await page.keyboard.press('ArrowLeft');
    assert.match(await page.locator('#enlarged-img').getAttribute('src'), /warmup\/0000.png$/);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('body.enlarged').count(), 0);

    await normal.locator('.frame').nth(1).click();
    assert.match(await page.locator('#enlarged-img').getAttribute('src'), /normal\/0001.png$/);
    await page.keyboard.press('ArrowRight');
    assert.match(await page.locator('#enlarged-img').getAttribute('src'), /normal\/0002.png$/);
    await page.keyboard.press('ArrowRight'); // clamp to this scenario, not next/previous
    assert.match(await page.locator('#enlarged-img').getAttribute('src'), /normal\/0002.png$/);
    await page.keyboard.press('ArrowLeft');
    assert.match(await page.locator('#enlarged-img').getAttribute('src'), /normal\/0001.png$/);
    await page.keyboard.press('Escape');
    assert.equal(await normal.locator('.facts-pane:not([hidden])').count(), 1);
    assert.match(await normal.locator('.facts-pane:not([hidden])').innerText(), /Recording/);
    assert.deepEqual(errors, []);
    console.log('BROWSER_OK click, right/left, escape, multi-scenario, facts/chips/audio');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
