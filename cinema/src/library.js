export function playbackUrl(path, sign = '') {
  if (typeof path !== 'string' || !path.startsWith('/m3u8/') || !path.endsWith('.m3u8') || path.split('/').some(p => p === '..' || p === '.')) throw new Error('无效的播放路径');
  return `/d${path.split('/').map(encodeURIComponent).join('/')}${sign ? `?sign=${encodeURIComponent(sign)}` : ''}`;
}
export function formatDuration(seconds) {
  if (!seconds) return '时长待补充';
  const minutes = Math.round(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟` : `${minutes} 分钟`;
}
export function filterVideos(videos, query, category, favoritesOnly, favorites) {
  const q = query.trim().toLocaleLowerCase();
  return videos.filter(v => (!favoritesOnly || favorites.includes(v.id)) && (!category || v.tags.includes(category)) && (!q || [v.title, v.id, v.description, ...v.tags, ...v.actors].join(' ').toLocaleLowerCase().includes(q)));
}
export function safeCover(value) {
  if (!value || typeof value !== 'string') return '';
  if (/^covers\/[\w.-]+\.(jpg|jpeg|png|webp|avif)$/i.test(value)) return `${import.meta.env?.BASE_URL || '/cinema/'}${value}`;
  try { const u = new URL(value); return u.protocol === 'https:' ? u.href : ''; } catch { return ''; }
}
export function normalizeCatalog(data) {
  if (!data || !Array.isArray(data.videos)) throw new Error('视频目录格式不正确');
  return data.videos.map(v => {
    playbackUrl(v.path);
    if (!v.id || typeof v.id !== 'string') throw new Error('视频缺少编号');
    return { ...v, title: String(v.title || v.id), tags: Array.isArray(v.tags) ? v.tags.filter(t => typeof t === 'string') : [], actors: Array.isArray(v.actors) ? v.actors.filter(t => typeof t === 'string') : [], description: String(v.description || ''), cover: safeCover(v.cover), backdrop: safeCover(v.backdrop), duration: Number(v.duration) || 0 };
  });
}
export function readLocal(key, fallback) {
  try { const value = JSON.parse(localStorage.getItem(key)); return value == null ? fallback : value; } catch { return fallback; }
}
export function writeLocal(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* Playback still works when storage is unavailable. */ } }
