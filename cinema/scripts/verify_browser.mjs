// Explicit live playback verification using the operator's own video and account.
import { chromium } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import assert from 'node:assert/strict';

const base = process.env.CINEMA_URL;
const id = process.env.CINEMA_TEST_VIDEO_ID;
const token = process.env.CINEMA_TOKEN;
if (!base || !id || !token) throw new Error('Set CINEMA_URL, CINEMA_TEST_VIDEO_ID and CINEMA_TOKEN for live playback checks');
const output = new URL('../../../outputs/cinema/', import.meta.url);
await mkdir(output, {recursive:true});
const browser = await chromium.launch({headless:true, ...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {}), args:['--autoplay-policy=no-user-gesture-required']});
try {
  const context = await browser.newContext({viewport:{width:1440,height:1024}});
  await context.addInitScript(value => sessionStorage.setItem('cinema.token', value), token);
  const page = await context.newPage();
  const errors = [], ranges = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('response', response => {
    const range = response.request().headers()['range'];
    if (range) ranges.push({status:response.status(), range, contentRange:response.headers()['content-range']});
  });
  await page.goto(base + '#video=' + encodeURIComponent(id));
  await page.locator('.film-info').waitFor();
  await page.locator('.big-play').click();
  await page.waitForFunction(() => document.querySelector('video')?.readyState >= 2, null, {timeout:90000});
  await page.evaluate(() => {const video=document.querySelector('video');video.muted=true;return video.play();});
  await page.waitForFunction(() => document.querySelector('video')?.currentTime > 1, null, {timeout:60000});
  const duration = await page.locator('video').evaluate(video => video.duration);
  const boundary = process.env.CINEMA_TEST_BOUNDARY_SECONDS ? Number(process.env.CINEMA_TEST_BOUNDARY_SECONDS) : null;
  const seek = boundary !== null ? boundary - 1 : Math.max(1, duration * .6);
  assert.ok(Number.isFinite(seek) && seek > 0 && seek + 2 < duration, 'Select a seek point within this video');
  await page.locator('video').evaluate((video,time) => {video.currentTime=time;}, seek);
  await page.waitForFunction(time => {const video=document.querySelector('video');return video?.currentTime>time+2 && video.readyState>=2;}, seek, {timeout:90000});
  await page.locator('video').evaluate(video => video.pause());
  await page.getByRole('button', {name:'‹ 返回片库', exact:true}).click();
  assert.equal(await page.locator('video').count(), 0);
  await page.goto(base + '#video=' + encodeURIComponent(id));
  await page.locator('.big-play').click();
  await page.waitForFunction(time => document.querySelector('video')?.currentTime > time, seek, {timeout:90000});
  assert.deepEqual(errors, []);
  assert.ok(ranges.some(row => row.status === 206));
  console.log(JSON.stringify({playback:true, seeking:true, crossPackBoundary:boundary !== null, resume:true, rangeReads:ranges.length, pageErrors:0}, null, 2));
} finally {
  await browser.close();
}
