<script setup>
import { ref, computed, onMounted, onBeforeUnmount, nextTick, watch } from 'vue';
import { playbackUrl, formatDuration, filterVideos, normalizeCatalog, readLocal, writeLocal } from './library.js';

const videos = ref([]), loading = ref(true), loadError = ref(''), query = ref(''), category = ref(''), tab = ref('all'), sort = ref('new');
const storedFavorites = readLocal('cinema.favorites', []), storedProgress = readLocal('cinema.progress', {});
const favorites = ref(Array.isArray(storedFavorites) ? storedFavorites : []);
const progress = ref(storedProgress && typeof storedProgress === 'object' && !Array.isArray(storedProgress) ? storedProgress : {});
const selected = ref(null), playRequested = ref(false), playbackState = ref(''), playbackError = ref(''), playerHost = ref(null);
const authenticated = ref(false), authChecking = ref(true);
const username = ref(''), password = ref(''), otp = ref(''), loginError = ref(''), loginBusy = ref(false);
const failedCovers = ref(new Set());
let player, hls, generation = 0, lastSaved = 0, focusReturn, libraryPromise;
const alistHome = import.meta.env.VITE_ALIST_HOME || '/';
const categories = computed(() => [...new Set(videos.value.flatMap(v => v.tags))]);
const visible = computed(() => {
  let list = filterVideos(videos.value, query.value, category.value, tab.value === 'favorites', favorites.value);
  if (tab.value === 'continue') list = list.filter(v => progress.value[v.id]?.time > 5 && !progress.value[v.id]?.ended);
  return [...list].sort(sort.value === 'title' ? (a,b) => a.title.localeCompare(b.title, 'zh-CN') : (a,b) => String(b.addedAt || '').localeCompare(String(a.addedAt || '')));
});
const heading = computed(() => ({all:'全部影片', favorites:'我的收藏', continue:'继续观看'}[tab.value]));
function getToken() { try { return sessionStorage.getItem('cinema.token') || localStorage.getItem('token') || ''; } catch { return ''; } }
async function api(endpoint, body, signal) {
  const prefix = endpoint === 'fs/get' && !import.meta.env.DEV ? '/cinema/api/' : '/api/';
  const res = await fetch(`${prefix}${endpoint}`, { method: 'POST', signal, headers: {'Content-Type':'application/json', 'Authorization':getToken()}, body:JSON.stringify(body) });
  if (res.status === 401) { lockLibrary(); const error = new Error('登录已失效'); error.code = 401; throw error; }
  const data = await res.json();
  if (data.code === 401) lockLibrary();
  if (data.code !== 200) { const error = new Error(data.message || '请求失败'); error.code = data.code; throw error; }
  return data.data;
}
function lockLibrary() {
  authenticated.value = false;
  stopPlayer(); videos.value = []; selected.value = null;
  try { sessionStorage.removeItem('cinema.token'); } catch { /* Keep the gate closed. */ }
}
async function verifySession() {
  const token = getToken();
  const response = await fetch(import.meta.env.DEV ? '/api/me' : '/cinema/session', {
    headers: token ? {Authorization:token} : {}, cache:'no-store',
  });
  const data = response.ok ? await response.json() : null;
  const valid = import.meta.env.DEV
    ? token && data?.code === 200 && Array.isArray(data.data?.role) && data.data.role.length > 0 && !data.data.role.includes(1) && !data.data.disabled
    : data?.authenticated === true;
  if (!valid) throw new Error('Session required');
  authenticated.value = true;
}
async function enterLibrary() {
  try { await verifySession(); await loadCatalog(); selectFromHash(); }
  catch { lockLibrary(); }
  finally { authChecking.value = false; }
}
async function recheckSession() {
  if (!authenticated.value || document.visibilityState === 'hidden') return;
  try { await verifySession(); }
  catch { lockLibrary(); loginError.value = '登录已失效，请重新登录。'; }
}
async function loadCatalog() {
  if (!authenticated.value) return;
  loading.value = true; loadError.value = '';
  try { const r = await fetch(`${import.meta.env.BASE_URL}catalog.json`, {cache:'no-cache'}); if (r.status === 401 || r.status === 403) { lockLibrary(); return; } if (!r.ok) throw new Error('暂时无法读取视频目录'); const catalog = normalizeCatalog(await r.json()); if (authenticated.value) videos.value = catalog; }
  catch { loadError.value = '视频目录加载失败，请重试。'; }
  finally { loading.value = false; }
}
function toggleFavorite(id) {
  favorites.value = favorites.value.includes(id) ? favorites.value.filter(v => v !== id) : [...favorites.value, id];
  writeLocal('cinema.favorites', favorites.value);
}
function saveProgress() {
  if (!player || !selected.value || !Number.isFinite(player.video.currentTime) || player.video.currentTime < 1) return;
  const time = player.video.currentTime, duration = player.video.duration;
  progress.value = {...progress.value, [selected.value.id]: {time, duration: Number.isFinite(duration) ? duration : 0, ended: player.video.ended || (Number.isFinite(duration) && duration - time < 15)}};
  writeLocal('cinema.progress', progress.value);
}
function stopPlayer() {
  generation++; saveProgress();
  if (hls) { hls.destroy(); hls = null; }
  if (player) { player.destroy(false); player = null; }
  playbackState.value = ''; playRequested.value = false;
}
function selectFromHash() {
  const id = new URLSearchParams(location.hash.slice(1)).get('video');
  const item = videos.value.find(v => v.id === id) || null;
  if (item?.id === selected.value?.id) return;
  stopPlayer(); selected.value = item; playbackError.value = ''; loginError.value = '';
  if (item) nextTick(() => document.querySelector('.back-button')?.focus());
  else nextTick(() => focusReturn?.focus());
}
function openVideo(v) { focusReturn = document.activeElement; location.hash = new URLSearchParams({video:v.id}).toString(); }
function closeVideo() { history.replaceState(null, '', `${location.pathname}${location.search}`); selectFromHash(); }
async function play() {
  if (!authenticated.value) return;
  stopPlayer(); const run = generation; const item = selected.value; if (!item) return;
  playRequested.value = true; playbackState.value = '正在准备播放…'; playbackError.value = '';
  try {
    libraryPromise ||= Promise.all([import('artplayer'), import('hls.js')]);
    const [file, [{default:Artplayer}, {default:Hls}]] = await Promise.all([api('fs/get', {path:item.path, password:''}), libraryPromise]);
    if (run !== generation) return;
    await nextTick();
    if (run !== generation || !playerHost.value) return;
    const url = playbackUrl(item.path, file.sign || '');
    player = new Artplayer({
      container:playerHost.value, url, type:'m3u8', lang:'zh-cn', theme:'#efbd73', volume:0.7,
      autoplay:false, playbackRate:true, setting:true, pip:true, fullscreen:true, fullscreenWeb:true,
      playsInline:true, hotkey:true, moreVideoAttr:{playsInline:true},
      customType:{ m3u8(video, source, art) {
        if (Hls.isSupported()) {
          hls = new Hls({maxBufferLength:20, backBufferLength:30, maxBufferSize:30*1024*1024});
          hls.on(Hls.Events.ERROR, (_, data) => {
            if (run !== generation || !data.fatal) return;
            playbackError.value = '播放中断，请重试。若仍失败，请检查视频源是否可用。';
            playbackState.value = '';
          });
          hls.loadSource(source); hls.attachMedia(video); art.on('destroy', () => { if (hls) { hls.destroy(); hls = null; } });
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) video.src = source;
        else throw new Error('当前浏览器不支持此视频格式');
      }},
    });
    player.on('video:loadedmetadata', () => {
      if (run !== generation) return;
      const saved = progress.value[item.id];
      if (saved && !saved.ended && saved.time > 5 && saved.time < player.video.duration - 15) player.currentTime = saved.time;
      playbackState.value = '';
      player.play().catch(() => { if (run === generation) playbackState.value = '视频已就绪，点击播放器开始播放'; });
    });
    player.on('video:playing', () => {playbackState.value = '';});
    player.on('video:timeupdate', () => { if (Date.now() - lastSaved > 5000) { saveProgress(); lastSaved = Date.now(); } });
    player.on('video:pause', saveProgress); player.on('video:ended', saveProgress);
    player.on('video:error', () => { if (run === generation) {playbackError.value = '视频加载失败，请重试。'; playbackState.value = '';} });
  } catch (e) {
    if (run !== generation) return;
    playbackState.value = ''; playRequested.value = false;
    if (e.code === 401) { lockLibrary(); loginError.value = '登录已失效，请重新登录。'; await nextTick(); document.querySelector('#username')?.focus(); }
    else if (e.code === 403) playbackError.value = '当前账号没有此影片的访问权限。';
    else playbackError.value = e.code === 500 ? '无法读取播放文件，请确认账号有此目录的访问权限。' : '暂时无法播放，请检查连接后重试。';
  }
}
async function login() {
  loginBusy.value = true; loginError.value = '';
  try {
    const result = await api('auth/login', {username:username.value, password:password.value, otp_code:otp.value});
    if (!result?.token) throw new Error('No session');
    sessionStorage.setItem('cinema.token', result.token); password.value = ''; otp.value = '';
    await verifySession(); await loadCatalog(); selectFromHash();
  } catch { lockLibrary(); loginError.value = '登录失败，请检查账号、密码和动态验证码。'; }
  finally { loginBusy.value = false; }
}
function keyboard(e) { if (e.key === 'Escape' && selected.value && !document.fullscreenElement && !player?.fullscreenWeb) closeVideo(); }
function coverFailed(id) { failedCovers.value = new Set([...failedCovers.value, id]); }
onMounted(async () => { await enterLibrary(); window.addEventListener('focus', recheckSession); document.addEventListener('visibilitychange', recheckSession); window.addEventListener('hashchange', selectFromHash); window.addEventListener('keydown', keyboard); window.addEventListener('pagehide', saveProgress); });
onBeforeUnmount(() => {stopPlayer();window.removeEventListener('focus',recheckSession);document.removeEventListener('visibilitychange',recheckSession);window.removeEventListener('hashchange',selectFromHash);window.removeEventListener('keydown',keyboard);window.removeEventListener('pagehide',saveProgress);});
watch(tab, () => { category.value = ''; });
</script>

<template>
  <div class="shell">
    <header class="topbar">
      <a class="brand" href="#" @click.prevent="closeVideo();tab='all'"><span class="brand-mark">▶</span><strong>alist-emby</strong><span class="brand-sub">MEDIA</span></a>
      <span class="header-note">媒体资料库</span>
      <a class="drive-link" :href="alistHome" target="_blank" rel="noopener">打开 AList <span aria-hidden="true">↗</span></a>
    </header>
    <main v-if="authChecking" class="empty" role="status">正在验证登录…</main>
    <main v-else-if="!authenticated" class="auth-gate">
      <form class="login-panel" @submit.prevent="login"><span class="eyebrow">WELCOME BACK</span><h2>登录后进入片库</h2><p>使用你的 AList 账号，登录后浏览和观看影片。</p><label for="username">账号</label><input id="username" v-model="username" autocomplete="username" required/><label for="password">密码</label><input id="password" v-model="password" type="password" autocomplete="current-password" required/><details><summary>使用动态验证码</summary><input v-model="otp" aria-label="动态验证码" inputmode="numeric" autocomplete="one-time-code" placeholder="六位验证码"/></details><p v-if="loginError" class="error-text" role="alert">{{ loginError }}</p><button class="primary" :disabled="loginBusy">{{ loginBusy ? '正在登录…' : '登录并进入' }}</button></form>
    </main>
    <template v-else-if="!selected">
      <section class="library-head">
        <div><p class="eyebrow">YOUR COLLECTION</p><h1>让好片，随时开场。</h1><p class="muted intro">收藏的影像，都在这里。</p></div>
        <label class="search"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></svg><input v-model="query" type="search" placeholder="搜索标题、编号、演员…" aria-label="搜索影片"/><kbd aria-hidden="true">⌕</kbd></label>
      </section>
      <nav class="tabs" aria-label="影片分类"><button :class="{active:tab==='all'}" @click="tab='all'">全部影片 <span>{{ videos.length }}</span></button><button :class="{active:tab==='continue'}" @click="tab='continue'">继续观看</button><button :class="{active:tab==='favorites'}" @click="tab='favorites'">我的收藏 <span v-if="favorites.length">{{ favorites.length }}</span></button></nav>
      <main>
        <div class="section-line"><div class="categories"><button :class="{chosen:!category}" @click="category=''">全部</button><button v-for="tag in categories" :key="tag" :class="{chosen:category===tag}" @click="category=tag">{{ tag }}</button></div><label class="sort"><span class="sr-only">排序</span><select v-model="sort"><option value="new">最近添加</option><option value="title">按标题排序</option></select></label></div>
        <div class="result-line"><h2>{{ heading }}</h2><span>{{ visible.length }} 部影片</span></div>
        <div v-if="loading" class="empty" role="status">正在读取影片…</div>
        <div v-else-if="loadError" class="empty" role="alert"><p>{{ loadError }}</p><button class="secondary" @click="loadCatalog">重新加载</button></div>
        <div v-else-if="!visible.length" class="empty"><span class="empty-icon">◫</span><h3>{{ query ? '没有找到匹配的影片' : tab==='favorites' ? '还没有收藏影片' : tab==='continue' ? '还没有观看记录' : '片库等待上新' }}</h3><p>{{ query ? '试试其他标题或编号。' : tab==='favorites' ? '在影片详情中点击收藏，就会出现在这里。' : tab==='continue' ? '开始播放后，可以在这里接着看。' : '更新视频目录后，影片会显示在这里。' }}</p></div>
        <div v-else class="poster-grid">
          <article v-for="(v,index) in visible" :key="v.id" class="movie-card">
            <button class="poster" :aria-label="`查看 ${v.title}`" @click="openVideo(v)">
              <img v-if="v.cover && !failedCovers.has(v.id)" :src="v.cover" :alt="v.title+' 封面'" loading="lazy" @error="coverFailed(v.id)"/>
              <div v-else class="poster-fallback"><span class="fallback-overline">PRIVATE COLLECTION</span><span class="fallback-title" :class="{compact:v.id.split('_').slice(0,2).join('').length>10}">{{ v.id.split('_').slice(0,2).join('\n') }}</span><span class="fallback-caption">暂无封面</span><span class="fallback-number">{{ String(index+1).padStart(2,'0') }}</span></div>
              <span class="poster-play" aria-hidden="true">▶</span><span v-if="favorites.includes(v.id)" class="poster-star" aria-label="已收藏">★</span>
              <span v-if="progress[v.id]?.time>5 && !progress[v.id]?.ended" class="progress-track"><span :style="{width:Math.min(100,progress[v.id].time/(progress[v.id].duration||v.duration||1)*100)+'%'}"></span></span>
            </button>
            <div class="card-title"><button @click="openVideo(v)">{{ v.title }}</button><span v-if="v.year">{{ v.year }}</span></div><p class="card-meta">{{ formatDuration(v.duration) }}<span v-if="v.tags.length"> · {{ v.tags[0] }}</span></p>
          </article>
        </div>
      </main>
    </template>
    <main v-else class="detail">
      <button class="back-button" @click="closeVideo">‹ 返回片库</button>
      <div class="detail-layout">
        <section class="watch-column">
          <div class="screen" :class="{'is-playing':playRequested}">
            <div v-show="playRequested" ref="playerHost" class="player-host"></div>
            <div v-if="!playRequested" class="screen-idle"><img v-if="selected.backdrop" class="backdrop-image" :src="selected.backdrop" alt="" @error="selected.backdrop=''"/><span class="eyebrow">NOW SHOWING</span><button class="big-play" @click="play" :aria-label="`播放 ${selected.title}`">▶</button><h2>{{ selected.title }}</h2><p>{{ progress[selected.id]?.time>5 && !progress[selected.id]?.ended ? '继续上次的观看' : '准备好，开始观看' }}</p></div>

          </div>
          <p v-if="playbackState" class="play-status" role="status">{{ playbackState }}</p>
          <div v-if="playbackError" class="play-error" role="alert"><span>{{ playbackError }}</span><button class="secondary" @click="play">重试播放</button></div>
          <div class="below-player"><span>正片</span><span>{{ formatDuration(selected.duration) }}</span></div>
        </section>
        <aside class="film-info"><img v-if="selected.cover && !failedCovers.has(selected.id)" class="detail-cover" :src="selected.cover" :alt="selected.title+' 封面'" @error="coverFailed(selected.id)"/><p class="eyebrow">IN YOUR COLLECTION</p><h1>{{ selected.title }}</h1><div class="film-facts"><span v-if="selected.year">{{ selected.year }}</span><span>{{ formatDuration(selected.duration) }}</span></div><div v-if="selected.tags.length" class="tags"><span v-for="t in selected.tags" :key="t">{{ t }}</span></div><button class="favorite-button" :class="{saved:favorites.includes(selected.id)}" :aria-pressed="favorites.includes(selected.id)" @click="toggleFavorite(selected.id)">{{ favorites.includes(selected.id) ? '★ 已收藏' : '☆ 收藏影片' }}</button><hr/><h3>影片简介</h3><p class="description">{{ selected.description || '这部影片还没有添加简介。' }}</p><dl><template v-if="selected.actors.length"><dt>演员</dt><dd>{{ selected.actors.join(' / ') }}</dd></template><template v-if="selected.director"><dt>导演</dt><dd>{{ selected.director }}</dd></template><template v-if="selected.studio"><dt>片商</dt><dd>{{ selected.studio }}</dd></template><template v-if="selected.premiered"><dt>发行</dt><dd>{{ selected.premiered }}</dd></template><dt>编号</dt><dd>{{ selected.id }}</dd></dl></aside>
      </div>
    </main>
    <footer><span>alist-emby <span class="footer-dot">/</span> 媒体资料库</span><span>好片值得慢慢看。</span></footer>
  </div>
</template>
