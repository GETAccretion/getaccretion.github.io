const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { test } = require('node:test');

// The admin summary answers only a linked Mod Studio whose account is an admin.
function load({ device = null, profile = null } = {}) {
  let handler;
  const table = (name) => {
    const q = {
      select() { return this; }, eq() { return this; }, update() { return this; },
      async maybeSingle() { return { data: name === 'save_devices' ? device : name === 'profiles' ? profile : null }; },
    };
    return q;
  };
  const admin = { from: table, auth: { admin: { getUserById: async () => ({ data: { user: { email: 'a@b.c' } } }) } } };
  const source = fs.readFileSync(path.join(__dirname, '../../supabase/functions/studio/index.ts'), 'utf8')
    .replace(/^import[^\n]+\r?\n/gm, '');
  vm.runInNewContext(stripTypeScriptTypes(source), {
    createClient: () => admin, Deno: { env: { get: () => '' }, serve: (fn) => { handler = fn; } },
    Request, Response, URL, Date, TextEncoder, crypto, console: { warn() {} },
  });
  return (headers = {}) => handler(new Request('https://fictional.invalid/studio?op=summary', { method: 'POST', headers, body: '{}' }));
}

test('the admin summary needs a linked Studio and an admin account', async () => {
  assert.equal((await load()()).status, 401, 'no token');
  assert.equal((await load()({ 'x-save-token': 'x'.repeat(43) })).status, 401, 'unknown token');
  const game = { id: 'd', owner: 'u', bundle: 'io.example.game' };
  assert.equal((await load({ device: game })({ 'x-save-token': 'x'.repeat(43) })).status, 401, 'a game token is not Studio');
  const studio = { id: 'd', owner: 'u', bundle: 'accretion.studio' };
  assert.equal((await load({ device: studio, profile: { role: 'user', banned: false } })({ 'x-save-token': 'x'.repeat(43) })).status, 403, 'not an admin');
  assert.equal((await load({ device: studio, profile: { role: 'admin', banned: true } })({ 'x-save-token': 'x'.repeat(43) })).status, 403, 'banned admin');
});
