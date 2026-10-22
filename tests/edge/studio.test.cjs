const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { test } = require('node:test');

// The admin summary answers only a linked Mod Studio whose account is an admin.
function load({ device = null, profile = null, rpc = async () => ({ data: null }) } = {}) {
  let handler;
  const table = (name) => {
    const q = {
      select() { return this; }, eq() { return this; }, update() { return this; },
      async maybeSingle() { return { data: name === 'save_devices' ? device : name === 'profiles' ? profile : null }; },
    };
    return q;
  };
  const admin = { from: table, rpc, auth: { admin: { getUserById: async () => ({ data: { user: { email: 'a@b.c' } } }) } } };
  const source = fs.readFileSync(path.join(__dirname, '../../supabase/functions/studio/index.ts'), 'utf8')
    .replace(/^import[^\n]+\r?\n/gm, '');
  vm.runInNewContext(stripTypeScriptTypes(source), {
    createClient: () => admin, Deno: { env: { get: () => '' }, serve: (fn) => { handler = fn; } },
    Request, Response, URL, Date, TextEncoder, crypto, console: { warn() {} },
  });
  return (headers = {}, op = 'summary', body = {}) =>
    handler(new Request(`https://fictional.invalid/studio?op=${op}`, { method: 'POST', headers, body: JSON.stringify(body) }));
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

test('moderation actions go through studio_moderate with the linked admin as the actor', async () => {
  const studio = { id: 'd', owner: 'admin-uuid', bundle: 'accretion.studio' };
  const calls = [];
  const rpc = async (name, args) => { calls.push([name, args]); return { data: { id: 7, hidden: true } }; };
  const call = load({ device: studio, profile: { role: 'admin', banned: false }, rpc });
  const token = { 'x-save-token': 'x'.repeat(43) };
  assert.equal((await call(token, 'act', { action: 'delete-everything', target: '1' })).status, 400, 'unknown action');
  assert.equal((await call(token, 'act', { action: 'hide' })).status, 400, 'no target');
  const ok = await call(token, 'act', { action: 'hide', target: '7', flag: true });
  assert.equal(ok.status, 200);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [['studio_moderate', { p_actor: 'admin-uuid', p_action: 'hide', p_target: '7', p_flag: true, p_value: null }]]);
  const none = load({ device: studio, profile: { role: 'admin', banned: false } });
  assert.equal((await none(token, 'act', { action: 'ban', target: 'u' })).status, 404, 'nothing matched');
  const user = load({ device: studio, profile: { role: 'mod', banned: false }, rpc });
  assert.equal((await user(token, 'act', { action: 'hide', target: '7' })).status, 403, 'a moderator account is not enough for Studio');
});
