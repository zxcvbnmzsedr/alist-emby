import test from 'node:test';
import assert from 'node:assert/strict';
import {playbackUrl, subtitleUrl, subtitleLabel, normalizeCatalog, filterVideos, safeCover} from '../src/library.js';
test('sign and Unicode path are encoded separately', () => {
  assert.equal(playbackUrl('/m3u8/片 名/index.m3u8', 'x=+:0'), '/d/m3u8/%E7%89%87%20%E5%90%8D/index.m3u8?sign=x%3D%2B%3A0');
  assert.throws(() => playbackUrl('/m3u8/../key'));
  assert.throws(() => playbackUrl('https://elsewhere/video.m3u8'));
});
test('catalog defaults and search work with absent metadata', () => {
  const videos = normalizeCatalog({videos:[{id:'ABC',path:'/m3u8/ABC/index.m3u8'}]});
  assert.equal(videos[0].title, 'ABC');
  assert.equal(filterVideos(videos,'abc','',false,[]).length, 1);
  assert.equal(filterVideos(videos,'','',true,[]).length, 0);
});
test('unsafe image URLs are rejected', () => {
  assert.equal(safeCover('javascript:alert(1)'), '');
  assert.equal(safeCover('//elsewhere/image.jpg'), '');
  assert.equal(safeCover('covers/../key'), '');
  assert.equal(safeCover('covers/abc.jpg'), '/cinema/covers/abc.jpg');
});
test('subtitles use signed same-folder paths and reject unrelated files', () => {
  const video = '/m3u8/片 名/index.m3u8';
  const subtitle = '/m3u8/片 名/影片.zh-CN.vtt';
  assert.equal(subtitleUrl(subtitle, video, 'a+:0'), '/d/m3u8/%E7%89%87%20%E5%90%8D/%E5%BD%B1%E7%89%87.zh-CN.vtt?sign=a%2B%3A0');
  for (const path of ['/m3u8/other/index.vtt', '/m3u8/片 名/key', '/m3u8/片 名/../index.vtt', 'https://other/track.vtt']) assert.throws(() => subtitleUrl(path, video));
  const result = normalizeCatalog({videos:[{id:'test',path:video,subtitles:[{path:subtitle,default:true},{path:'/m3u8/other/index.vtt'}]}]});
  assert.equal(result[0].subtitles.length, 1);
  assert.equal(result[0].subtitles[0].format, 'vtt');
});
test('subtitle metadata cannot inject menu markup or unsigned URLs', () => {
  const track = {path:'/m3u8/test/index.vtt', title:'<img src=x onerror="alert(1)">', html:'<script>bad</script>', url:'https://other/track.vtt'};
  const normalized = normalizeCatalog({videos:[{id:'test',path:'/m3u8/test/index.m3u8',subtitles:[track]}]}).at(0).subtitles.at(0);
  assert.equal(normalized.html, undefined);
  assert.equal(normalized.url, undefined);
  assert.equal(subtitleLabel(normalized.title), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
});
