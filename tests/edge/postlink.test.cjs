// Public post links for our own mods and Accretion: dl.getaccretion.app/get/<mod> serves the newest build
// of one of our mods, /get/accretion Accretion's (Telegram update posts), without an account (the download
// function's public=1 mode), and nothing else.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { test } = require('node:test');
const { pathToFileURL } = require('node:url');

const KEY = Buffer.alloc(32, 7).toString('base64url'); // fictional test key
const ORIGIN = 'https://dl.fictional.invalid';
const BUILDS = [
  { path: 'mods/game-plus/1.0.ipa', version: '1.0-mod1.0.0', sha256: 'a'.repeat(64) },
  { path: 'mods/game-plus/1.1.ipa', version: '1.1-mod1.0.0', sha256: 'b'.repeat(64) },
];

function downloadFunction() {
  let handler;
  const issued = [];
  const admin = {
    auth: { getUser: async () => ({ data: { user: null } }) },
    storage: { from: () => ({ createSignedUrl: async (p) => ({ data: { signedUrl: `https://copy.fictional.invalid/${p}` }, error: null }) }) },
    from(table) {
      const query = {
        select() { return this; }, eq() { return this; }, gte() { return this; },
        async maybeSingle() { return { data: null, error: null }; },
        then(resolve, reject) {
          const result = table === 'hosted_builds' ? { data: BUILDS, error: null } : { data: [], error: null };
          return Promise.resolve(result).then(resolve, reject);
        },
        async insert(row) { issued.push(row); return { error: null }; },
      };
      return query;
    },
  };
  const env = { DOWNLOAD_PROXY_ORIGIN: ORIGIN, DOWNLOAD_PROXY_KEY: KEY };
  const source = fs.readFileSync(path.join(__dirname, '../../supabase/functions/download/index.ts'), 'utf8')
    .replace(/^import[^\n]+\r?\n/gm, '');
  vm.runInNewContext(stripTypeScriptTypes(source), {
    createClient: () => admin,
    Deno: { env: { get: (k) => env[k] ?? '' }, serve: fn => { handler = fn; } },
    Request, Response, URL, Date, AbortSignal, crypto, btoa, atob, TextEncoder, Uint8Array, String, JSON,
    console: { warn() {} },
  });
  return { issued, get: (query) => handler(new Request(`https://fictional.invalid/download?${query}`)) };
}

test('public=1 serves our newest mod build without an account and logs nothing', async () => {
  const f = downloadFunction();
  const response = await f.get('app=io.github.accretionmods.gameplus&public=1');
  assert.equal(response.status, 302);
  assert.ok(response.headers.get('Location').includes('1.1-mod1.0.0'), response.headers.get('Location'));
  assert.equal(f.issued.length, 0);
});

test('public=1 does not open other apps, and our mods and Accretion still need it', async () => {
  const f = downloadFunction();
  assert.equal((await f.get('app=com.example.other&public=1')).status, 401);
  assert.equal((await f.get('app=io.github.getaccretion.Accretion.evil&public=1')).status, 401);
  assert.equal((await f.get('app=io.github.accretionmods.gameplus')).status, 401);
  assert.equal((await f.get('app=io.github.getaccretion.Accretion')).status, 401);
});

test('public=1 serves Accretion itself without an account and logs nothing', async () => {
  const f = downloadFunction();
  const response = await f.get('app=io.github.getaccretion.Accretion&public=1');
  assert.equal(response.status, 302);
  assert.equal(f.issued.length, 0);
});

async function worker() {
  return (await import(pathToFileURL(path.join(__dirname, '../../workers/download-proxy/index.js')).href)).default;
}

function withUpstream(fn, respond) {
  const real = globalThis.fetch;
  const seen = [];
  globalThis.fetch = async (url, init) => { seen.push({ url, init }); return respond(url, init); };
  return fn(seen).finally(() => { globalThis.fetch = real; });
}

test('/get/<mod> asks for our mod in public mode and passes the redirect on', async () => {
  const w = await worker();
  const env = { DOWNLOAD_FUNCTION: 'https://fn.fictional.invalid/download' };
  await withUpstream(async (seen) => {
    const response = await w.fetch(new Request('https://dl.fictional.invalid/get/thewandererplus'), env);
    assert.equal(response.status, 302);
    assert.equal(response.headers.get('Location'), `${ORIGIN}/The-Wanderer_10.1.ipa?t=x`);
    assert.equal(seen[0].url, 'https://fn.fictional.invalid/download?app=io.github.accretionmods.thewandererplus&public=1');
  }, () => new Response(null, { status: 302, headers: { Location: `${ORIGIN}/The-Wanderer_10.1.ipa?t=x` } }));
});

test('/get/accretion asks for Accretion itself in public mode', async () => {
  const w = await worker();
  const env = { DOWNLOAD_FUNCTION: 'https://fn.fictional.invalid/download' };
  await withUpstream(async (seen) => {
    const response = await w.fetch(new Request('https://dl.fictional.invalid/get/accretion'), env);
    assert.equal(response.status, 302);
    assert.equal(seen[0].url, 'https://fn.fictional.invalid/download?app=io.github.getaccretion.Accretion&public=1');
  }, () => new Response(null, { status: 302, headers: { Location: `${ORIGIN}/Accretion_1.0.41.ipa?t=x` } }));
});

test('/get/ refuses odd names, reports a missing build, and rate-limits', async () => {
  const w = await worker();
  const env = { DOWNLOAD_FUNCTION: 'https://fn.fictional.invalid/download' };
  await withUpstream(async (seen) => {
    assert.notEqual((await w.fetch(new Request('https://dl.fictional.invalid/get/../x'), env)).status, 302); // the URL becomes /x
    assert.equal((await w.fetch(new Request('https://dl.fictional.invalid/get/a%2Fb'), env)).status, 404);
    assert.equal(seen.length, 0, 'bad names never reach the download function');
    assert.equal((await w.fetch(new Request('https://dl.fictional.invalid/get/nothing'), env)).status, 404);
    const limited = { ...env, POST_LINKS: { limit: async () => ({ success: false }) } };
    assert.equal((await w.fetch(new Request('https://dl.fictional.invalid/get/thewandererplus'), limited)).status, 429);
  }, () => new Response('{"message":"none"}', { status: 404 }));
});
