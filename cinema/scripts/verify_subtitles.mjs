// 在本地测试视频上验证字幕显示、拖动、关闭和重新开启。
import { chromium } from '@playwright/test';
import assert from 'node:assert/strict';
import { readFile, mkdir, mkdtemp, rm } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const base = process.env.CINEMA_URL || 'http://127.0.0.1:4180/cinema/';
const fixtures = await mkdtemp(join(tmpdir(), 'alist-subtitle-test-'));
execFileSync('ffmpeg', ['-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=0x182630:s=320x180:r=25',
  '-t','12','-c:v','libx264','-pix_fmt','yuv420p','-g','75','-f','hls','-hls_time','3','-hls_list_size','0',
  '-hls_segment_filename',join(fixtures,'part_%03d.ts'),join(fixtures,'index.m3u8')]);
const output = new URL('../../work/subtitle-web-test/', import.meta.url);
await mkdir(output, {recursive:true});
const browser = await chromium.launch({headless:true,
  ...(process.env.BROWSER_EXECUTABLE ? {executablePath:process.env.BROWSER_EXECUTABLE} : {}),
  args:['--autoplay-policy=no-user-gesture-required']});
try {
  const context = await browser.newContext();
  await context.addInitScript(() => sessionStorage.setItem('cinema.token', 'synthetic-test-token'));
  const page = await context.newPage();
  const errors = [], requested = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/cinema/session', route => route.fulfill({json:{authenticated:true}}));
  const subtitles = [{path:'/m3u8/SUBTITLE_TEST/index.zh-CN.vtt', format:'vtt', title:'简体中文', language:'zho', default:true},
    {path:'/m3u8/SUBTITLE_TEST/index.en.vtt', format:'vtt', title:'<b data-subtitle-test>测试英语</b>', language:'eng', default:false}];
  await page.route('**/cinema/catalog.json', route => route.fulfill({json:{videos:[{
    id:'SUBTITLE_TEST',title:'字幕兼容测试',path:'/m3u8/SUBTITLE_TEST/index.m3u8',duration:12,subtitles}]}}));
  await page.route('**/cinema/api/fs/get', route => {
    requested.push(route.request().postDataJSON().path);
    return route.fulfill({json:{code:200,data:{sign:'synthetic-sign'}}});
  });
  await page.route('**/d/m3u8/SUBTITLE_TEST/**', async route => {
    const name = new URL(route.request().url()).pathname.split('/').pop();
    if (name.endsWith('.vtt')) return route.fulfill({contentType:'text/vtt',body:'WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n中文字幕测试\n\n00:00:06.000 --> 00:00:10.000\n拖动后字幕\n'});
    return route.fulfill({contentType:name.endsWith('.m3u8') ? 'application/vnd.apple.mpegurl' : 'video/mp2t',body:await readFile(join(fixtures,name))});
  });
  await page.goto(base + '#video=SUBTITLE_TEST');
  await page.locator('.big-play').click();
  await page.waitForFunction(() => document.querySelector('.art-subtitle')?.textContent.includes('中文字幕测试'), null, {timeout:15000});
  await page.locator('video').evaluate(video => {video.pause(); video.currentTime=7;});
  await page.waitForFunction(() => document.querySelector('.art-subtitle')?.textContent.includes('拖动后字幕'));
  await page.screenshot({path:new URL('subtitles-visible.png',output).pathname});
  await page.locator('.art-control-setting').click();
  await page.getByText('字幕', {exact:true}).filter({visible:true}).click();
  assert.equal(await page.locator('[data-subtitle-test]').count(), 0);
  assert.equal(await page.getByText(subtitles[1].title, {exact:true}).isVisible(), true);
  await page.getByText('关闭字幕', {exact:true}).click();
  assert.equal(await page.locator('.art-subtitle').isVisible(), false);
  if (!await page.locator('.art-settings').isVisible()) await page.locator('.art-control-setting').click();
  await page.getByText('字幕', {exact:true}).filter({visible:true}).click();
  await page.getByText('简体中文', {exact:true}).click();
  await page.waitForFunction(() => document.querySelector('.art-subtitle')?.textContent.includes('拖动后字幕'));
  assert.equal(await page.locator('.art-subtitle').isVisible(), true);
  assert.ok(requested.includes(subtitles[0].path));
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({subtitleVisible:true,seeking:true,toggle:true,signedSubtitleLookup:true,safeMenuLabels:true,pageErrors:0}));
} finally {await browser.close(); await rm(fixtures,{recursive:true,force:true});}
