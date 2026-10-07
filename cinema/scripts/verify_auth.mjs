import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';

const base = process.env.CINEMA_URL || 'http://127.0.0.1:4178/cinema/';
const output = new URL('../../../outputs/cinema/', import.meta.url);
await mkdir(output, {recursive:true});
const browser = await chromium.launch({...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {}), headless:true});
const results = {};
try {
  const context = await browser.newContext();
  const page = await context.newPage();
  const errors = [], privateRequests = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { if (/\/cinema\/(catalog.json|covers\/)/.test(r.url())) privateRequests.push(r.url()); });
  // No interception in this first check: production must reject anonymous data access.
  await page.goto(base + '#video=TEST_001');
  await page.getByRole('heading', {name:'登录后进入片库'}).waitFor();
  assert.equal(await page.locator('.movie-card,.film-info').count(), 0);
  assert.deepEqual(privateRequests, []);
  results.anonymousEntryLoadsNoPrivateData = true;
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  await page.screenshot({path:new URL('auth-mobile.png', output).pathname, fullPage:true});
  await page.setViewportSize({width:1440,height:1024});
  await page.screenshot({path:new URL('auth-desktop.png', output).pathname, fullPage:true});

  await page.evaluate(() => localStorage.setItem('token', 'invalid-test-token'));
  await page.reload();
  await page.getByRole('heading', {name:'登录后进入片库'}).waitFor();
  assert.deepEqual(privateRequests, []);
  results.invalidStoredTokenBlocked = true;

  // The following checks use synthetic identity/catalog responses to test UI transitions.
  let valid = false;
  await page.route('**/cinema/session', route => route.fulfill({status:valid ? 200 : 401, contentType:'application/json', body:JSON.stringify({authenticated:valid})}));
  await page.route('**/api/auth/login', async route => {
    valid = route.request().postDataJSON().password === 'correct-test-password';
    await route.fulfill({contentType:'application/json', body:JSON.stringify(valid ? {code:200,data:{token:'synthetic-test-token'}} : {code:400,message:'Invalid credentials'})});
  });
  await page.route('**/cinema/catalog.json', route => route.fulfill({contentType:'application/json', body:JSON.stringify({videos:[{id:'TEST_001',path:'/m3u8/TEST_001/index.m3u8'}]})}));
  await page.locator('#username').fill('synthetic-test-user');
  await page.locator('#password').fill('wrong');
  await page.getByRole('button', {name:'登录并进入'}).click();
  await page.getByRole('alert').waitFor();
  assert.deepEqual(privateRequests, []);
  results.mockWrongPasswordKeepsGateClosed = true;
  await page.locator('#password').fill('correct-test-password');
  await page.getByRole('button', {name:'登录并进入'}).click();
  await page.locator('.film-info').waitFor();
  assert.equal(privateRequests.length, 1);
  results.mockLoginRestoresDeepLink = true;
  await page.reload();
  await page.locator('.film-info').waitFor();
  results.mockValidSessionSurvivesReload = true;
  await page.getByRole('button', {name:'☆ 收藏影片', exact:true}).click();
  await page.getByRole('button', {name:'‹ 返回片库', exact:true}).click();
  await page.getByRole('searchbox').fill('no-such-video-xyz');
  await page.getByText('没有找到匹配的影片').waitFor();
  await page.getByRole('searchbox').fill('test');
  assert.equal(await page.locator('.movie-card').count(), 1);
  await page.getByRole('button', {name:/我的收藏/}).click();
  assert.equal(await page.locator('.movie-card').count(), 1);
  await page.reload();
  await page.getByRole('button', {name:/我的收藏/}).click();
  assert.equal(await page.locator('.movie-card').count(), 1);
  results.mockSearchAndFavoritesSurviveReload = true;
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
  results.mobileLibraryNoOverflow = true;
  valid = false;
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await page.getByRole('heading', {name:'登录后进入片库'}).waitFor();
  assert.equal(await page.locator('.movie-card,.film-info').count(), 0);
  results.mockExpiredSessionClearsLibrary = true;
  assert.deepEqual(errors, []);
  await context.close();
  await writeFile(new URL('auth-browser-verification.json', output), JSON.stringify(results,null,2));
  console.log(JSON.stringify(results,null,2));
} finally { await browser.close(); }
