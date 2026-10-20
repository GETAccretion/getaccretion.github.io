const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const { test } = require('node:test');

// Loads the download function with the Alibaba Cloud copy configured and fetch stubbed.
function load(env = {}) {
  const fetched = [];
  const context = {
    createClient: () => ({}),
    Deno: { env: { get: (name) => env[name] ?? '' }, serve() {} },
    Request, Response, URL, Date, AbortSignal, TextEncoder, crypto, btoa, console: { warn() {} },
    fetch: async (url, init) => { fetched.push({ url, init }); return new Response('x', { status: 206 }); },
  };
  const source = fs.readFileSync(path.join(__dirname, '../../supabase/functions/download/index.ts'), 'utf8')
    .replace(/^import[^\n]+\r?\n/gm, '');
  vm.runInNewContext(stripTypeScriptTypes(source), context);
  return { context, fetched };
}

test('V2 query signing matches the published S3 example', async () => {
  const { context } = load();
  // docs.aws.amazon.com, "Query string request authentication" (signature version 2)
  const link = await context.v2Link('johnsmith.s3.amazonaws.com', 'johnsmith', 'photos/puppy.jpg',
    'AKIAIOSFODNN7EXAMPLE', 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', 1175139620);
  assert.equal(link, 'https://johnsmith.s3.amazonaws.com/photos/puppy.jpg?AWSAccessKeyId=AKIAIOSFODNN7EXAMPLE&Expires=1175139620&Signature=NpgCjnDzrM%2BWFzoENXmpNDUsSn8%3D');
});

test('the Alibaba Cloud copy is off until configured', async () => {
  const { context, fetched } = load();
  assert.equal(await context.ossLink('io.github.getaccretion.Accretion/1.0.39.ipa', 'Accretion_1.0.39.ipa'), null);
  assert.equal(fetched.length, 0);
});

test('the Alibaba Cloud link uses the S3-compatible host and names the file', async () => {
  const { context, fetched } = load({ OSS_BUCKET: 'example-builds', OSS_REGION: 'ap-southeast-1', OSS_KEY_ID: 'FICTIONAL', OSS_SECRET: 'fictional' });
  const link = await context.ossLink('io.github.getaccretion.Accretion/1.0.39.ipa', 'Accretion_1.0.39.ipa');
  assert.match(link, /^https:\/\/example-builds\.s3\.oss-ap-southeast-1\.aliyuncs\.com\/io\.github\.getaccretion\.Accretion\/1\.0\.39\.ipa\?AWSAccessKeyId=FICTIONAL&Expires=\d+&Signature=[^&]+&response-content-disposition=attachment%3B%20filename%3D%22Accretion_1\.0\.39\.ipa%22$/);
  assert.equal(fetched[0].init.headers.Range, 'bytes=0-0');
});
