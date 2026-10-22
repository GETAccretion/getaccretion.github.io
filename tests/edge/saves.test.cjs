const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { test } = require('node:test');

// The function's pure helpers, loaded the way download.test.cjs loads its function (imports stripped).
function load(admin = {}) {
  let handler;
  const context = {
    createClient: () => admin,
    Deno: { env: { get: () => '' }, serve: (fn) => { handler = fn; } },
    Request, Response, URL, Date, TextEncoder, crypto, btoa, console: { warn() {} },
  };
  const source = fs.readFileSync(path.join(__dirname, '../../supabase/functions/saves/index.ts'), 'utf8')
    .replace(/^import[^\n]+\r?\n/gm, '');
  vm.runInNewContext(stripTypeScriptTypes(source), context);
  return { context, call: (op, body = {}, headers = {}) => handler(new Request(`https://fictional.invalid/saves?op=${op}`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', ...headers }, body: JSON.stringify(body),
  })) };
}

test('codes: 7 unambiguous characters, typed any way, shown with a dash', () => {
  const { context: c } = load();
  assert.equal(c.normalizeCode('k7q 4xm'), null, 'six characters are not a code');
  assert.equal(c.normalizeCode('k7q 4xm8'), 'K7Q4XM8', 'spaces are ignored');
  assert.equal(c.normalizeCode('k7q-4xmp'), 'K7Q4XMP');
  assert.equal(c.normalizeCode('K7Q4XMP'), 'K7Q4XMP');
  assert.equal(c.normalizeCode('K7Q4XM0'), null, 'zero is not a code character');
  assert.equal(c.normalizeCode(undefined), null);
  assert.equal(c.showCode('K7Q4XMP'), 'K7Q-4XMP');
  const code = c.newCode(new Uint8Array([0, 1, 2, 3, 250, 255, 30]));
  assert.equal(code.length, 7);
  assert.ok(c.normalizeCode(code));
});

test('accounts are shown masked, sizes are checked', () => {
  const { context: c } = load();
  assert.equal(c.maskEmail('benwill13@icloud.com'), 'b•••@icloud.com');
  assert.equal(c.maskEmail(''), 'your account');
  assert.equal(c.sizeOk(1), true);
  assert.equal(c.sizeOk(50 * 1024 * 1024), true);
  assert.equal(c.sizeOk(50 * 1024 * 1024 + 1), false);
  assert.equal(c.sizeOk(0), false);
  assert.equal(c.sizeOk('12'), false);
});

test('device and site calls need their credentials', async () => {
  const query = { select() { return this; }, eq() { return this; }, async maybeSingle() { return { data: null }; } };
  const { call } = load({ from: () => query, auth: { getUser: async () => ({ data: { user: null } }) } });
  assert.equal((await call('list')).status, 401);
  assert.equal((await call('list', {}, { 'x-save-token': 'x'.repeat(43) })).status, 401);
  assert.equal((await call('mine')).status, 401);
  assert.equal((await call('mine', {}, { Authorization: 'Bearer fictional' })).status, 401);
  assert.equal((await call('nonsense')).status, 404);
  const expired = await (await call('link-poll', { code: 'bad' })).json();
  assert.equal(expired.status, 'expired');
});
