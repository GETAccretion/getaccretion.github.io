// Share links: dl.getaccretion.app/s/<id> serves one privately stored file under its name until the link's
// expiry; renewing keeps the link, revoking ends it (upload function's share modes, download's share mode).
const assert = require('node:assert/strict');
const { webcrypto } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { test } = require('node:test');
const { pathToFileURL } = require('node:url');

const SHA = 'c'.repeat(64);
const ID = 'AbCdEfGhIjKlMnOpQrSt';
const NAME = 'Wanderer-Plus_1.0.15.ipa';
const day = 86_400_000;

// one in-memory share_links table, shared by the fictional download and upload functions
function table(rows = []) {
  const data = new Map(rows.map(r => [r.id, { downloads: 0, revoked: false, ...r }]));
  const from = () => {
    const filters = {};
    let patch = null;
    const query = {
      select() { return this; },
      order() { return this; },
      eq(k, v) { filters[k] = v; if (patch) { const row = data.get(v); if (row) Object.assign(row, patch); return Promise.resolve({ error: null }); } return this; },
      update(values) { patch = values; return this; },
      async maybeSingle() { return { data: data.get(filters.id) ?? null, error: null }; },
      async insert(row) { data.set(row.id, { downloads: 0, revoked: false, ...row }); return { error: null }; },
      then(resolve, reject) { return Promise.resolve({ data: [...data.values()], error: null }).then(resolve, reject); },
    };
    return query;
  };
  return { data, from };
}

function downloadFunction(rows, cdnOk = true) {
  let handler;
  const t = table(rows);
  const env = { CDN_DOMAIN: 'cdn.fictional.invalid', CDN_KEY_PAIR_ID: 'K', CDN_PRIVATE_KEY: 'P' };
  const source = fs.readFileSync(path.join(__dirname, '../../supabase/functions/download/index.ts'), 'utf8')
    .replace(/^import[^\n]+\r?\n/gm, '');
  vm.runInNewContext(stripTypeScriptTypes(source), {
    createClient: () => ({ from: t.from, auth: { getUser: async () => ({ data: { user: null } }) } }),
    getSignedUrl: ({ url }) => url + '&signed',
    fetch: async () => new Response(null, { status: cdnOk ? 200 : 403 }),
    createHash: () => ({ update() { return this; }, digest: () => '' }),
    Deno: { env: { get: (k) => env[k] ?? '' }, serve: fn => { handler = fn; } },
    Request, Response, URL, Date, AbortSignal, crypto: webcrypto, btoa, atob, TextEncoder, Uint8Array, String, JSON,
    console: { warn() {} },
  });
  return { table: t.data, get: (id) => handler(new Request(`https://fictional.invalid/download?share=${id}`)) };
}

test('a share link serves its file under its name and counts the download', async () => {
  const f = downloadFunction([{ id: ID, sha256: SHA, name: NAME, expires_at: new Date(Date.now() + 180 * day).toISOString() }]);
  const response = await f.get(ID);
  assert.equal(response.status, 302);
  const location = response.headers.get('Location');
  assert.ok(location.startsWith(`https://cdn.fictional.invalid/bases/${SHA}?dl=${encodeURIComponent(NAME)}`), location);
  assert.equal(f.table.get(ID).downloads, 1);
});

test('expired, revoked, unknown and malformed links give no file', async () => {
  const f = downloadFunction([
    { id: ID, sha256: SHA, name: NAME, expires_at: new Date(Date.now() - day).toISOString() },
    { id: ID.replace('A', 'Z'), sha256: SHA, name: NAME, expires_at: new Date(Date.now() + day).toISOString(), revoked: true },
  ]);
  assert.equal((await f.get(ID)).status, 410);
  assert.equal((await f.get(ID.replace('A', 'Z'))).status, 404);
  assert.equal((await f.get('NoSuchLinkNoSuchLink')).status, 404);
  assert.equal((await f.get('short')).status, 404);
  assert.equal((await f.get('')).status, 404);
  assert.equal(f.table.get(ID).downloads, 0);
});

test('no answering copy means no redirect', async () => {
  const f = downloadFunction([{ id: ID, sha256: SHA, name: NAME, expires_at: new Date(Date.now() + day).toISOString() }], false);
  assert.equal((await f.get(ID)).status, 503);
});

const KEY = 'fictional-upload-key-used-only-in-tests';

function uploadFunction(rows = [], stored = [SHA]) {
  let handler;
  const t = table(rows);
  const source = fs.readFileSync(path.join(__dirname, '../../supabase/functions/upload/index.ts'), 'utf8')
    .replace(/^import[\s\S]*?from "[^"]+";\r?\n/gm, '');
  const command = kind => class { kind = kind; constructor(input) { this.input = input; } };
  vm.runInNewContext(stripTypeScriptTypes(source), {
    createClient: () => ({ from: t.from, storage: { from: () => ({}) } }),
    S3Client: class {
      async send(c) {
        if (c.kind === 'headObject' && !stored.some(s => c.input.Key === `bases/${s}`)) throw new Error('missing');
        return {};
      }
    },
    HeadBucketCommand: command('head'), HeadObjectCommand: command('headObject'), GetObjectCommand: command('get'),
    PutObjectCommand: command('put'), DeleteObjectCommand: command('delete'),
    getSignedUrl: async () => 'https://s3.invalid/x',
    Deno: { env: { get: k => ({ SUPABASE_URL: 'https://fictional.invalid', SUPABASE_SERVICE_ROLE_KEY: 'f', UPLOAD_KEY: KEY, BUILDS_BUCKET: 'b', BUILDS_AWS_KEY_ID: 'k', BUILDS_AWS_SECRET: 's' })[k] }, serve: fn => { handler = fn; } },
    Request, Response, URL, Blob, TextEncoder, Uint8Array, crypto: webcrypto, btoa, String, AbortSignal, Date, JSON,
    console: { warn() {} },
  });
  return {
    table: t.data,
    async call(mode, body, key = KEY) {
      const r = await handler(new Request(`https://fictional.invalid/upload?share=${mode}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'x-upload-key': key }, body: JSON.stringify(body),
      }));
      return { status: r.status, body: await r.json() };
    },
  };
}

test('create, renew (same id, new expiry), revoke, and renewing a revoked link is refused', async () => {
  const f = uploadFunction();
  const made = await f.call('create', { sha256: SHA, name: NAME, days: 180, note: 'my phone' });
  assert.equal(made.status, 200);
  assert.match(made.body.id, /^[A-Za-z0-9_-]{20}$/);
  const days = (iso) => Math.round((Date.parse(iso) - Date.now()) / day);
  assert.equal(days(made.body.expires_at), 180);
  const renewed = await f.call('renew', { id: made.body.id, days: 365 });
  assert.equal(renewed.status, 200);
  assert.equal(renewed.body.id, made.body.id);
  assert.equal(days(f.table.get(made.body.id).expires_at), 365);
  assert.equal((await f.call('revoke', { id: made.body.id })).status, 200);
  assert.equal(f.table.get(made.body.id).revoked, true);
  assert.equal((await f.call('renew', { id: made.body.id, days: 30 })).status, 409);
  const listed = await f.call('list', {});
  assert.equal(listed.body.links.length, 1);
});

test('update points the same link at another stored file', async () => {
  const other = 'd'.repeat(64);
  const f = uploadFunction([], [SHA, other]);
  const made = await f.call('create', { sha256: SHA, name: NAME, days: 30 });
  const updated = await f.call('update', { id: made.body.id, sha256: other, name: 'Wanderer-Plus_1.0.16.ipa' });
  assert.equal(updated.status, 200);
  assert.equal(f.table.get(made.body.id).sha256, other);
  assert.equal(f.table.get(made.body.id).name, 'Wanderer-Plus_1.0.16.ipa');
  assert.equal((await f.call('update', { id: made.body.id, sha256: 'e'.repeat(64), name: NAME })).status, 404);  // not stored
  assert.equal((await f.call('update', { id: made.body.id, sha256: other, name: 'bad name' })).status, 400);
  await f.call('revoke', { id: made.body.id });
  assert.equal((await f.call('update', { id: made.body.id, sha256: other, name: NAME })).status, 409);
});

test('share modes check the key, the file, the name and the days', async () => {
  const f = uploadFunction([], []);
  assert.equal((await f.call('create', { sha256: SHA, name: NAME, days: 30 }, 'wrong-key')).status, 401);
  assert.equal((await f.call('create', { sha256: SHA, name: NAME, days: 30 })).status, 404);  // not stored
  const g = uploadFunction();
  assert.equal((await g.call('create', { sha256: SHA, name: 'bad name.ipa', days: 30 })).status, 400);
  assert.equal((await g.call('create', { sha256: SHA, name: NAME, days: 0 })).status, 400);
  assert.equal((await g.call('create', { sha256: SHA, name: NAME, days: 401 })).status, 400);
  assert.equal((await g.call('renew', { id: 'NoSuchLinkNoSuchLink', days: 30 })).status, 404);
  assert.equal((await g.call('nope', { id: ID })).status, 404);  // unknown id before the unknown mode
});

async function worker() {
  return (await import(pathToFileURL(path.join(__dirname, '../../workers/download-proxy/index.js')).href)).default;
}

test('/s/<id> passes the share redirect on and explains expired or turned-off links', async () => {
  const w = await worker();
  const env = { DOWNLOAD_FUNCTION: 'https://fn.fictional.invalid/download' };
  const real = globalThis.fetch;
  const seen = [];
  let status = 302;
  globalThis.fetch = async (url) => { seen.push(url); return new Response(null, { status, headers: status === 302 ? { Location: 'https://dl.fictional.invalid/x.ipa?t=1' } : {} }); };
  try {
    const ok = await w.fetch(new Request(`https://dl.fictional.invalid/s/${ID}`), env);
    assert.equal(ok.status, 302);
    assert.equal(seen[0], `https://fn.fictional.invalid/download?share=${ID}`);
    status = 410;
    assert.equal((await w.fetch(new Request(`https://dl.fictional.invalid/s/${ID}`), env)).status, 410);
    status = 404;
    assert.equal((await w.fetch(new Request(`https://dl.fictional.invalid/s/${ID}`), env)).status, 404);
    const before = seen.length;
    assert.equal((await w.fetch(new Request('https://dl.fictional.invalid/s/x'), env)).status, 404);
    assert.equal(seen.length, before, 'bad ids never reach the download function');
    const limited = { ...env, POST_LINKS: { limit: async () => ({ success: false }) } };
    assert.equal((await w.fetch(new Request(`https://dl.fictional.invalid/s/${ID}`), limited)).status, 429);
  } finally {
    globalThis.fetch = real;
  }
});
