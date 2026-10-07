import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
// Development only. Production is static and shares the existing AList origin.
const target = process.env.ALIST_ORIGIN || 'http://127.0.0.1:5244';
export default defineConfig({
  base: '/cinema/', plugins: [vue()],
  publicDir: process.env.CINEMA_PUBLIC_DIR || 'public',
  server: { port: 5178, strictPort: true, proxy: Object.fromEntries(['/api/', '/d/', '/p/'].map(path => [path, { target, changeOrigin: true, timeout: 120000, proxyTimeout: 120000 }])) },
});
