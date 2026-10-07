import { spawn } from 'node:child_process';
import { setTimeout } from 'node:timers/promises';

const port = process.env.CINEMA_TEST_PORT || '4180';
const server = spawn(process.execPath, ['node_modules/vite/bin/vite.js', 'preview', '--host', '127.0.0.1', '--port', port, '--strictPort'], {stdio:'ignore'});
const base = `http://127.0.0.1:${port}/cinema/`;
try {
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (server.exitCode !== null) throw new Error('Preview server exited');
    try { ready = (await fetch(base)).ok; } catch {}
    if (ready) break;
    await setTimeout(100);
  }
  if (!ready) throw new Error('Preview server did not start');
  const check = spawn(process.execPath, ['scripts/verify_auth.mjs'], {stdio:'inherit', env:{...process.env, CINEMA_URL:base}});
  const code = await new Promise(resolve => check.once('exit', resolve));
  if (code !== 0) throw new Error('Browser checks failed');
} finally {
  server.kill('SIGTERM');
}
