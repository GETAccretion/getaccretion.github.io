// upload: puts a new build of Accretion's own apps (the Accretion app, Accretion Mods) straight
// into the private "builds" bucket, so release files never sit in a public GitHub release.
//
//   POST /functions/v1/upload            {bundle_id, version, sha256} → {signedUrl, path, upload_id}
//        then PUT the .ipa to signedUrl (Content-Type: application/octet-stream)
//   POST /functions/v1/upload?finish=1   {bundle_id, version, sha256, upload_id, name?, notes?} → {path, sha256}
//        also posts "<name> <version>" with the notes to the What's new feed (announce: false skips it)
//   POST /functions/v1/upload?base=put   {sha256}  → {signedUrl}  PUT a mod's private base .ipa
//   POST /functions/v1/upload?base=get   {sha256}  → {signedUrl}  GET it back (15 minutes)
//   POST /functions/v1/upload?share=create  {sha256, name, days, note?} → {id, expires_at}  a share link for a
//        stored base-style file (dl.getaccretion.app/s/<id>, served by the download function)
//   POST /functions/v1/upload?share=renew   {id, days} → {id, expires_at}   same link, new expiry (from now)
//   POST /functions/v1/upload?share=revoke  {id}       → {id}               the link stops working for good
//   POST /functions/v1/upload?share=list    {}         → {links: [...]}
//
// Every call needs "x-upload-key: <UPLOAD_KEY>", a secret only the release workflows and the
// mods publish script hold. Deployed with --no-verify-jwt; the key is the check.
//
// Builds go to the S3 bucket in aws/builds-cdn.yaml when BUILDS_BUCKET, BUILDS_REGION,
// BUILDS_AWS_KEY_ID and BUILDS_AWS_SECRET are set and S3 answers, and to the Supabase "builds"
// bucket otherwise. Every finished build is kept in both, so the download function can fall back
// to Supabase whenever AWS is unavailable.
import { createClient } from "npm:@supabase/supabase-js@2.117.2";
import {
  DeleteObjectCommand,
  GetObjectCommand,
  HeadBucketCommand,
  HeadObjectCommand,
  PutObjectCommand,
  S3Client,
} from "npm:@aws-sdk/client-s3@3.1149.0";
import { getSignedUrl } from "npm:@aws-sdk/s3-request-presigner@3.1149.0";

const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);

const BUCKET = Deno.env.get("BUILDS_BUCKET") ?? "";
const s3 = BUCKET && Deno.env.get("BUILDS_AWS_KEY_ID") && Deno.env.get("BUILDS_AWS_SECRET")
  ? new S3Client({
    region: Deno.env.get("BUILDS_REGION") ?? "us-east-2",
    credentials: {
      accessKeyId: Deno.env.get("BUILDS_AWS_KEY_ID")!,
      secretAccessKey: Deno.env.get("BUILDS_AWS_SECRET")!,
    },
  })
  : null;

const IPA = "application/octet-stream";

// S3 is set up and answering right now
async function s3Healthy(): Promise<boolean> {
  if (!s3) return false;
  try {
    await s3.send(new HeadBucketCommand({ Bucket: BUCKET }), { abortSignal: AbortSignal.timeout(4000) });
    return true;
  } catch (e) {
    console.warn(`S3 unavailable (${e instanceof Error ? e.message : e}); using Supabase storage`);
    return false;
  }
}

// where the .ipa is PUT (valid 1 hour)
async function uploadLink(path: string): Promise<string | null> {
  if (s3 && await s3Healthy()) {
    return await getSignedUrl(s3, new PutObjectCommand({ Bucket: BUCKET, Key: path }), { expiresIn: 3600 });
  }
  const { data, error } = await admin.storage.from("builds").createSignedUploadUrl(path, { upsert: false });
  return error || !data ? null : data.signedUrl;
}

async function readFromS3(path: string): Promise<Uint8Array | null> {
  if (!s3) return null;
  try {
    const object = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: path }));
    return object.Body ? await object.Body.transformToByteArray() : null;
  } catch {
    return null;
  }
}

async function readFromSupabase(path: string): Promise<Uint8Array | null> {
  const slash = path.lastIndexOf("/");
  const folder = path.slice(0, slash);
  const file = path.slice(slash + 1);
  const { data: files } = await admin.storage.from("builds").list(folder, { search: file });
  if (!files?.some((f: { name: string }) => f.name === file)) return null;
  const { data: blob } = await admin.storage.from("builds").download(path);
  return blob ? new Uint8Array(await blob.arrayBuffer()) : null;
}

// the stored file and where it was found, or null if it isn't anywhere
async function readBuild(path: string): Promise<{ bytes: Uint8Array; where: "s3" | "supabase" } | null> {
  const fromS3 = await readFromS3(path);
  if (fromS3) return { bytes: fromS3, where: "s3" };
  const fromSupabase = await readFromSupabase(path);
  return fromSupabase ? { bytes: fromSupabase, where: "supabase" } : null;
}

// keep a copy in the other store, so downloads still work if either one is down (best effort)
async function mirrorBuild(path: string, bytes: Uint8Array, where: "s3" | "supabase") {
  try {
    if (where === "s3") {
      const { error } = await admin.storage.from("builds").upload(path, bytes, { upsert: false, contentType: IPA });
      if (error) throw error;
    } else if (s3) {
      await s3.send(new PutObjectCommand({ Bucket: BUCKET, Key: path, Body: bytes, ContentType: IPA, IfNoneMatch: "*" }));
    }
  } catch (e) {
    console.warn(`Couldn't copy ${path} from ${where} to the other store`);
  }
}

async function removeBuild(path: string) {
  if (s3) await s3.send(new DeleteObjectCommand({ Bucket: BUCKET, Key: path })).catch(() => {});
  await admin.storage.from("builds").remove([path]);
}

const reply = (status: number, body: Record<string, unknown>) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });

// Final paths contain the content hash and are written without replacement. Upload links only
// address separate staging objects, so even a reusable signed PUT cannot change a publication.
async function storeBuild(path: string, bytes: Uint8Array): Promise<"s3" | "supabase" | null> {
  if (s3) {
    try {
      await s3.send(new PutObjectCommand({ Bucket: BUCKET, Key: path, Body: bytes, ContentType: IPA, IfNoneMatch: "*" }));
      return "s3";
    } catch (e) {
      if ((e as { $metadata?: { httpStatusCode?: number } }).$metadata?.httpStatusCode === 412) return "s3";
    }
  }
  const { error } = await admin.storage.from("builds").upload(path, bytes, { upsert: false, contentType: IPA });
  if (!error || String(error.statusCode) === "409") return "supabase";
  return null;
}

async function existingBuild(bundle: string, version: string) {
  return await admin.from("hosted_builds").select("path,sha256").eq("bundle_id", bundle).eq("version", version).maybeSingle();
}

// constant-time comparison, so the key can't be guessed one character at a time
function same(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

// Share links (public.share_links): one id per file, its expiry 1 to 400 days from now, renewable.
const SHARE_ID = /^[A-Za-z0-9_-]{16,32}$/;
const SHARE_NAME = /^[A-Za-z0-9-]{1,64}_[A-Za-z0-9.-]{1,32}\.ipa$/;  // the download proxy's file-name rule

function shareDays(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 && value <= 400 ? value : null;
}

function newShareId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(15));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_");  // 20 characters
}

async function shareLinks(mode: string, body: Record<string, unknown>): Promise<Response> {
  const until = (days: number) => new Date(Date.now() + days * 86_400_000).toISOString();
  if (mode === "list") {
    const { data, error } = await admin.from("share_links")
      .select("id,name,note,expires_at,revoked,downloads,last_download_at,created_at").order("created_at", { ascending: false });
    return error ? reply(503, { message: "Couldn't read share links." }) : reply(200, { links: data ?? [] });
  }
  if (mode === "create") {
    const sha = String(body.sha256 ?? "");
    const name = String(body.name ?? "");
    const days = shareDays(body.days);
    const note = body.note === undefined ? null : String(body.note).slice(0, 200);
    if (!/^[0-9a-f]{64}$/.test(sha)) return reply(400, { message: "Send sha256: 64 lowercase hex characters." });
    if (!SHARE_NAME.test(name)) return reply(400, { message: "name must look like App-Name_1.0.ipa." });
    if (!days) return reply(400, { message: "days must be a whole number from 1 to 400." });
    if (!s3 || !(await s3Healthy())) return reply(503, { message: "S3 isn't available right now." });
    try {
      await s3.send(new HeadObjectCommand({ Bucket: BUCKET, Key: `bases/${sha}` }));
    } catch {
      return reply(404, { message: "No stored file with that sha256: upload it first (base=put)." });
    }
    const row = { id: newShareId(), sha256: sha, name, note, expires_at: until(days) };
    const { error } = await admin.from("share_links").insert(row);
    return error ? reply(503, { message: "Couldn't save the share link." }) : reply(200, { id: row.id, expires_at: row.expires_at });
  }
  const id = String(body.id ?? "");
  if (!SHARE_ID.test(id)) return reply(400, { message: "Send the link's id." });
  const { data: found, error: findError } = await admin.from("share_links").select("id,revoked").eq("id", id).maybeSingle();
  if (findError) return reply(503, { message: "Couldn't read the share link." });
  if (!found) return reply(404, { message: "No share link with that id." });
  if (mode === "renew") {
    const days = shareDays(body.days);
    if (!days) return reply(400, { message: "days must be a whole number from 1 to 400." });
    if (found.revoked) return reply(409, { message: "This link was revoked; make a new one." });
    const expires_at = until(days);
    const { error } = await admin.from("share_links").update({ expires_at }).eq("id", id);
    return error ? reply(503, { message: "Couldn't renew the link." }) : reply(200, { id, expires_at });
  }
  if (mode === "revoke") {
    const { error } = await admin.from("share_links").update({ revoked: true }).eq("id", id);
    return error ? reply(503, { message: "Couldn't revoke the link." }) : reply(200, { id });
  }
  return reply(400, { message: "share must be create, renew, revoke or list." });
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return reply(405, { message: "Use POST." });
  const expected = Deno.env.get("UPLOAD_KEY") ?? "";
  if (expected.length < 32 || !same(req.headers.get("x-upload-key") ?? "", expected)) {
    return reply(401, { message: "Not allowed." });
  }

  let body: { bundle_id?: string; version?: string; size?: number; sha256?: string; upload_id?: string; name?: string; notes?: string; announce?: boolean };
  try {
    body = await req.json();
    if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("Invalid body");
  } catch {
    return reply(400, { message: "Send a JSON object with bundle_id, version and sha256." });
  }

  // private base apps for mods (mods/scripts/base.py). Kept in S3 only, under bases/<sha256> (no
  // .ipa ending, so the Oracle mirror and the store copy script skip them), never listed anywhere
  // public. The mod build checks the file's sha256 itself.
  const shareMode = new URL(req.url).searchParams.get("share");
  if (shareMode) return await shareLinks(shareMode, body as Record<string, unknown>);

  const baseMode = new URL(req.url).searchParams.get("base");
  if (baseMode === "put" || baseMode === "get") {
    const sha = String((body as { sha256?: string }).sha256 ?? "");
    if (!/^[0-9a-f]{64}$/.test(sha)) return reply(400, { message: "Send {sha256}: 64 lowercase hex characters." });
    if (!s3 || !(await s3Healthy())) return reply(503, { message: "S3 isn't available for base apps right now." });
    const Key = `bases/${sha}`;
    const signedUrl = baseMode === "put"
      ? await getSignedUrl(s3, new PutObjectCommand({ Bucket: BUCKET, Key }), { expiresIn: 3600 })
      : await getSignedUrl(s3, new GetObjectCommand({ Bucket: BUCKET, Key }), { expiresIn: 900 });
    return reply(200, { signedUrl, path: Key });
  }
  const bundle = body.bundle_id ?? "";
  const version = body.version ?? "";
  const claimed = body.sha256 ?? "";
  if (typeof bundle !== "string" || typeof version !== "string" || typeof claimed !== "string" ||
      !/^[A-Za-z0-9][A-Za-z0-9.-]{0,199}$/.test(bundle) || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/.test(version)) {
    return reply(400, { message: "bundle_id or version looks wrong." });
  }
  if (!/^[0-9a-f]{64}$/.test(claimed)) return reply(400, { message: "Send sha256: 64 lowercase hex characters." });
  if ((body.name !== undefined && typeof body.name !== "string") ||
      (body.notes !== undefined && typeof body.notes !== "string") ||
      (body.announce !== undefined && typeof body.announce !== "boolean")) {
    return reply(400, { message: "name and notes must be text; announce must be true or false." });
  }

  const existing = await existingBuild(bundle, version);
  if (existing.error) return reply(503, { message: "Couldn't check publication state. Try again." });
  if (existing.data) {
    if (existing.data.sha256 !== claimed) return reply(409, { message: "This version is already published. Bump the version." });
    return reply(200, { published: true, path: existing.data.path, sha256: claimed });
  }

  if (new URL(req.url).searchParams.get("finish") !== "1") {
    const upload_id = crypto.randomUUID();
    const path = `staging/${upload_id}/${bundle}/${version}.upload`;
    let signedUrl: string | null = null;
    try {
      signedUrl = await uploadLink(path);
    } catch {
      console.warn("Couldn't prepare a staging upload");
    }
    if (!signedUrl) return reply(500, { message: "Couldn't prepare the upload. Try again." });
    return reply(200, { signedUrl, path, upload_id });
  }

  const upload_id = body.upload_id ?? "";
  if (typeof upload_id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(upload_id)) {
    return reply(400, { message: "Finish needs the upload_id returned when preparing the upload." });
  }
  const staging = `staging/${upload_id}/${bundle}/${version}.upload`;
  const stored = await readBuild(staging);
  if (!stored) return reply(409, { message: "The file isn't in storage yet. Upload it first." });
  const bytes = stored.bytes;
  // fingerprint the stored file, so the app can check what it downloads is what was published
  const digest = await crypto.subtle.digest("SHA-256", bytes as Uint8Array<ArrayBuffer>);
  const sha256 = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  if (claimed !== sha256) {
    return reply(422, { message: "The uploaded file doesn't match the sha256 that was sent. Upload it again." });
  }
  const path = `${bundle}/${version}-${sha256}.ipa`;
  const where = await storeBuild(path, bytes);
  if (!where) return reply(503, { message: "Couldn't keep the finished build. Try again." });
  const { error } = await admin.from("hosted_builds").insert({
    bundle_id: bundle,
    version,
    path,
    size: bytes.byteLength,
    sha256,
  });
  if (error) {
    if (error.code === "23505") {
      const winner = await existingBuild(bundle, version);
      if (winner.data?.sha256 === sha256) return reply(200, { published: true, path: winner.data.path, sha256 });
      return reply(409, { message: "This version is already published. Bump the version." });
    }
    return reply(503, { message: "Couldn't record the build. Try again." });
  }
  await mirrorBuild(path, bytes, where);
  await removeBuild(staging).catch(() => console.warn("Couldn't remove a completed staging upload"));

  // the What's new feed: one post per published build (best effort; the build is already live)
  const extra = body as { name?: string; notes?: string; announce?: boolean };
  if (extra.announce !== false) {
    const isApp = bundle === "io.github.getaccretion.Accretion";
    const name = (extra.name ?? (isApp ? "Accretion" : bundle)).slice(0, 80);
    await admin.from("announcements").insert({
      kind: isApp ? "release" : "mod",
      title: `${name} ${version}`,
      body: (extra.notes ?? "").slice(0, 4000),
      app_id: bundle,
      version,
      link: `https://getaccretion.app/#/app/${encodeURIComponent(bundle)}`,
    });
  }
  return reply(200, { path, sha256 });
});
