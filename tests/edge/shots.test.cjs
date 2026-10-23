const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { test } = require('node:test');

const KEY = 'k'.repeat(40);

// The shots function with a stand-in database and storage: rows by id, uploaded paths.
function load(rows = {}) {
  let handler;
  const stored = [];
  const table = () => {
    let filter = {};
    const q = {
      select() { return q; }, order() { return q; }, limit() { return q; }, lt() { return Promise.resolve({ data: [] }); },
      eq(k, v) { filter[k] = v; return q; }, in() { return Promise.resolve({}); },
      async maybeSingle() { return { data: rows[filter.id] ?? null }; },
      async insert(row) { rows[row.id] = row; return {}; },
      update(patch) { q.patch = patch; return q; },
      delete() { return q; },
    };
    return q;
  };
  const storage = {
    from: () => ({
      async upload(p) { stored.push(p); return {}; },
      async createSignedUrl(p) { return { data: { signedUrl: `https://files.invalid/${p}?sig=1` } }; },
      async remove() { return {}; },
    }),
  };
  const admin = { from: table, storage };
  const env = { SUPABASE_URL: 'https://project.invalid', UPLOAD_KEY: KEY };
  const source = fs.readFileSync(path.join(__dirname, '../../supabase/functions/shots/index.ts'), 'utf8')
    .replace(/^import[^\n]+\r?\n/gm, '');
  vm.runInNewContext(stripTypeScriptTypes(source), {
    createClient: () => admin, Deno: { env: { get: (k) => env[k] }, serve: (fn) => { handler = fn; } },
    Request, Response, URL, Date, TextEncoder, Uint8Array, crypto, console: { warn() {} },
  });
  return { call: (url, init) => handler(new Request(`https://project.invalid/functions/v1/shots${url}`, init)), stored, rows };
}

test('posting needs the upload key', async () => {
  const { call } = load();
  assert.equal((await call('?op=list', { method: 'POST' })).status, 401);
  assert.equal((await call('?op=list', { method: 'POST', headers: { 'x-upload-key': 'x'.repeat(40) } })).status, 401);
});

test('upload checks the life and the file type, then returns a link', async () => {
  const { call, stored } = load();
  const h = (type) => ({ 'x-upload-key': KEY, 'content-type': type });
  assert.equal((await call('?op=upload&ttl=5y', { method: 'POST', headers: h('image/png'), body: 'x' })).status, 400);
  assert.equal((await call('?op=upload&ttl=3m', { method: 'POST', headers: h('application/zip'), body: 'x' })).status, 415);
  const ok = await call('?op=upload&ttl=1y&label=menu', { method: 'POST', headers: h('image/png'), body: 'png-bytes' });
  assert.equal(ok.status, 200);
  const body = await ok.json();
  assert.match(body.id, /^[A-Za-z0-9]{12}$/);
  assert.equal(body.url, `https://project.invalid/functions/v1/shots?id=${body.id}`);
  assert.deepEqual(stored, [`${body.id}.png`]);
  const days = (new Date(body.expires_at) - Date.now()) / 86400000;
  assert.ok(days > 364 && days < 366, 'a year');
});

test('a link opens the file until it expires', async () => {
  const live = { path: 'aaaaaaaaaaaa.png', expires_at: new Date(Date.now() + 86400000).toISOString() };
  const old = { path: 'bbbbbbbbbbbb.png', expires_at: new Date(Date.now() - 86400000).toISOString() };
  const { call } = load({ aaaaaaaaaaaa: live, bbbbbbbbbbbb: old });
  const open = await call('?id=aaaaaaaaaaaa', { redirect: 'manual' });
  assert.equal(open.status, 302);
  assert.match(open.headers.get('location'), /aaaaaaaaaaaa\.png\?sig=1$/);
  assert.equal((await call('?id=bbbbbbbbbbbb')).status, 410, 'expired');
  assert.equal((await call('?id=cccccccccccc')).status, 404, 'unknown');
  assert.equal((await call('?id=../etc')).status, 404, 'not an id');
});

test('renew needs a known id and a valid life', async () => {
  const { call } = load();
  const h = { 'x-upload-key': KEY, 'content-type': 'application/json' };
  assert.equal((await call('?op=renew', { method: 'POST', headers: h, body: JSON.stringify({ id: 'x' }) })).status, 400);
  assert.equal((await call('?op=renew', { method: 'POST', headers: h, body: JSON.stringify({ id: 'aaaaaaaaaaaa', ttl: '9m' }) })).status, 400);
  assert.equal((await call('?op=renew', { method: 'POST', headers: h, body: JSON.stringify({ id: 'aaaaaaaaaaaa', ttl: '6m' }) })).status, 404);
});
