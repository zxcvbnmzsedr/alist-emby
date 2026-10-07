import test from 'node:test';
import assert from 'node:assert/strict';
import {playbackUrl, normalizeCatalog, filterVideos, safeCover} from '../src/library.js';
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
