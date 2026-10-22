// saves: cloud saves for Accretion's mods. A mod's menu links once to the owner's account with a one-time code,
// then backs up and restores the game's save (a zip, 50 MB at most) in the private "saves" bucket, which only that
// account reaches (through this function: the bucket and tables have no public or user write access).
//
//   POST /functions/v1/saves?op=link-start {bundle, game, device} → {code, expires_at, approve_url}
//   POST ?op=link-poll {code} → {status: "waiting" | "expired"} | {status: "linked", token, account} (token once)
// with the header x-save-token (the token from link-poll):
//   POST ?op=me {} → {account, bundle, game}
//   POST ?op=backup-start {size, mod_version, game_version} → {id, upload_url} (PUT the zip there)
//   POST ?op=backup-finish {id} → {id, size, created_at} (keeps the newest 5 per account and game)
//   POST ?op=list {} → {backups: [{id, device, this_device, mod_version, game_version, size, created_at}]}
//   POST ?op=restore {id} → {download_url} (5 minutes)
//   POST ?op=unlink {} → {}
// signed in on the site, with Authorization: Bearer <access token>:
//   POST ?op=approve {code} → {bundle, game}
//   POST ?op=mine {} → {games: [{bundle, game, backups: [...], devices: [{id, device, created_at, last_seen}]}]}
//   POST ?op=delete {id} → {}       POST ?op=forget-device {device_id} → {}
//
// Deployed with --no-verify-jwt: device calls carry their own token, site calls are checked with auth.getUser.
import { createClient } from "npm:@supabase/supabase-js@2.117.2";

const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);

const BUCKET = "saves";
const MAX_BYTES = 50 * 1024 * 1024;
const KEEP = 5; // finished backups per account and game
const CODE_CHARS = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";
const APPROVE = "https://getaccretion.app/#/link?code=";

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-save-token",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const answer = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json", "Cache-Control": "no-store" } });
const fail = (status: number, error: string) => answer(status, { error });

// ---------------------------------------------------------------- small pure helpers (tests call these)

// "k7q 4xm" or "K7Q-4XMP" -> "K7Q4XMP"; null unless 7 code characters
function normalizeCode(input: unknown): string | null {
  const code = String(input ?? "").toUpperCase().replace(/[\s-]/g, "");
  return code.length === 7 && [...code].every((c) => CODE_CHARS.includes(c)) ? code : null;
}

function showCode(code: string): string {
  return `${code.slice(0, 3)}-${code.slice(3)}`;
}

function newCode(random: Uint8Array): string {
  return [...random.slice(0, 7)].map((b) => CODE_CHARS[b % CODE_CHARS.length]).join("");
}

// "benwill13@icloud.com" -> "b•••@icloud.com"
function maskEmail(email: unknown): string {
  const text = String(email ?? "");
  const at = text.indexOf("@");
  return at > 0 ? `${text[0]}•••${text.slice(at)}` : "your account";
}

function clean(value: unknown, max: number): string {
  return String(value ?? "").trim().slice(0, max);
}

function sizeOk(size: unknown): boolean {
  return Number.isInteger(size) && (size as number) >= 1 && (size as number) <= MAX_BYTES;
}

function b64url(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------- who is calling

async function deviceFor(req: Request) {
  const token = req.headers.get("x-save-token") ?? "";
  if (token.length < 20) return null;
  const { data } = await admin.from("save_devices").select("id, owner, bundle, game, device")
    .eq("token_hash", await sha256(token)).maybeSingle();
  if (data) await admin.from("save_devices").update({ last_seen: new Date().toISOString() }).eq("id", data.id);
  return data;
}

async function userFor(req: Request) {
  const token = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "");
  if (!token) return null;
  const { data } = await admin.auth.getUser(token);
  return data?.user ?? null;
}

async function accountOf(owner: string): Promise<string> {
  const { data } = await admin.auth.admin.getUserById(owner);
  return maskEmail(data?.user?.email);
}

const backupRow = (b: Record<string, unknown>, deviceId?: string) => ({
  id: b.id, device: b.device, mod_version: b.mod_version, game_version: b.game_version, size: b.size,
  created_at: b.created_at, ...(deviceId === undefined ? {} : { this_device: b.device_id === deviceId }),
});

async function removeBackups(rows: { id: string; path: string }[]) {
  if (!rows.length) return;
  await admin.storage.from(BUCKET).remove(rows.map((r) => r.path));
  await admin.from("save_backups").delete().in("id", rows.map((r) => r.id));
}

// ---------------------------------------------------------------- the calls

async function linkStart(req: Request, body: Record<string, unknown>) {
  const bundle = clean(body.bundle, 200), game = clean(body.game, 80) || bundle, device = clean(body.device, 80);
  if (!bundle) return fail(400, "Which game is this? (bundle missing)");
  const ip = (req.headers.get("x-forwarded-for") ?? "").split(",")[0].trim().slice(0, 64);
  const hourAgo = new Date(Date.now() - 3600_000).toISOString();
  const { count } = await admin.from("save_links").select("code", { count: "exact", head: true })
    .eq("ip", ip).gte("created_at", hourAgo);
  if ((count ?? 0) >= 20) return fail(429, "Too many sign-in codes from here: try again in an hour");
  for (let tries = 0; tries < 5; tries++) {
    const code = newCode(crypto.getRandomValues(new Uint8Array(7)));
    const { data, error } = await admin.from("save_links")
      .insert({ code, bundle, game, device, ip }).select("expires_at").single();
    if (!error && data) return answer(200, { code: showCode(code), expires_at: data.expires_at, approve_url: APPROVE + showCode(code) });
  }
  return fail(503, "Could not make a code: try again");
}

async function linkPoll(body: Record<string, unknown>) {
  const code = normalizeCode(body.code);
  if (!code) return answer(200, { status: "expired" });
  const { data: link } = await admin.from("save_links").select("*").eq("code", code).maybeSingle();
  if (!link || new Date(link.expires_at).getTime() < Date.now()) return answer(200, { status: "expired" });
  if (!link.owner) return answer(200, { status: "waiting" });
  const token = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const { error } = await admin.from("save_devices").insert({
    owner: link.owner, bundle: link.bundle, game: link.game, device: link.device, token_hash: await sha256(token),
  });
  if (error) return fail(503, "Could not finish linking: try again");
  await admin.from("save_links").delete().eq("code", code);
  return answer(200, { status: "linked", token, account: await accountOf(link.owner) });
}

async function backupStart(dev: Record<string, string>, body: Record<string, unknown>) {
  if (!sizeOk(body.size)) return fail(400, "That save is too big (50 MB at most)");
  const dayAgo = new Date(Date.now() - 86400_000).toISOString();
  const { count } = await admin.from("save_backups").select("id", { count: "exact", head: true })
    .eq("device_id", dev.id).gte("created_at", dayAgo);
  if ((count ?? 0) >= 30) return fail(429, "30 backups today already from this device: try tomorrow");
  const id = crypto.randomUUID();
  const path = `${dev.owner}/${dev.bundle}/${id}.zip`;
  const { error } = await admin.from("save_backups").insert({
    id, owner: dev.owner, bundle: dev.bundle, game: dev.game, device_id: dev.id, device: dev.device, path,
    size: body.size, mod_version: clean(body.mod_version, 40), game_version: clean(body.game_version, 40),
  });
  if (error) return fail(503, "Could not start the backup: try again");
  const { data, error: linkError } = await admin.storage.from(BUCKET).createSignedUploadUrl(path);
  if (linkError || !data) return fail(503, "Storage is not answering: try again");
  return answer(200, { id, upload_url: data.signedUrl });
}

async function backupFinish(dev: Record<string, string>, body: Record<string, unknown>) {
  const { data: row } = await admin.from("save_backups").select("*").eq("id", clean(body.id, 64))
    .eq("owner", dev.owner).eq("bundle", dev.bundle).maybeSingle();
  if (!row) return fail(404, "That backup was not started here");
  const folder = row.path.slice(0, row.path.lastIndexOf("/"));
  const { data: files } = await admin.storage.from(BUCKET).list(folder, { search: `${row.id}.zip` });
  const file = (files ?? []).find((f: { name: string }) => f.name === `${row.id}.zip`);
  if (!file) return fail(400, "The upload did not arrive: back up again");
  const size = Number(file.metadata?.size ?? row.size);
  if (size > MAX_BYTES) {
    await removeBackups([row]);
    return fail(400, "That save is too big (50 MB at most)");
  }
  await admin.from("save_backups").update({ finished: true, size }).eq("id", row.id);
  // keep the newest KEEP finished backups of this game, and drop unfinished ones older than an hour
  const { data: all } = await admin.from("save_backups").select("id, path, finished, created_at")
    .eq("owner", dev.owner).eq("bundle", dev.bundle).order("created_at", { ascending: false });
  const hourAgo = Date.now() - 3600_000;
  const finished = (all ?? []).filter((b: { finished: boolean; id: string }) => b.finished || b.id === row.id);
  const stale = (all ?? []).filter((b: { finished: boolean; id: string; created_at: string }) =>
    !b.finished && b.id !== row.id && new Date(b.created_at).getTime() < hourAgo);
  await removeBackups([...finished.slice(KEEP), ...stale]);
  return answer(200, { id: row.id, size, created_at: row.created_at });
}

async function list(dev: Record<string, string>) {
  const { data } = await admin.from("save_backups").select("*").eq("owner", dev.owner).eq("bundle", dev.bundle)
    .eq("finished", true).order("created_at", { ascending: false });
  return answer(200, { backups: (data ?? []).map((b: Record<string, unknown>) => backupRow(b, dev.id)) });
}

async function restore(dev: Record<string, string>, body: Record<string, unknown>) {
  const { data: row } = await admin.from("save_backups").select("path").eq("id", clean(body.id, 64))
    .eq("owner", dev.owner).eq("bundle", dev.bundle).eq("finished", true).maybeSingle();
  if (!row) return fail(404, "That backup is gone");
  const { data, error } = await admin.storage.from(BUCKET).createSignedUrl(row.path, 300);
  if (error || !data) return fail(503, "Storage is not answering: try again");
  return answer(200, { download_url: data.signedUrl });
}

async function approve(owner: string, body: Record<string, unknown>) {
  const code = normalizeCode(body.code);
  const { data: link } = code
    ? await admin.from("save_links").select("*").eq("code", code).maybeSingle()
    : { data: null };
  if (!link || link.owner || new Date(link.expires_at).getTime() < Date.now()) return fail(400, "That code is wrong or has expired");
  await admin.from("save_links").update({ owner }).eq("code", code);
  return answer(200, { bundle: link.bundle, game: link.game });
}

async function mine(owner: string) {
  const [{ data: backups }, { data: devices }] = await Promise.all([
    admin.from("save_backups").select("*").eq("owner", owner).eq("finished", true).order("created_at", { ascending: false }),
    admin.from("save_devices").select("id, bundle, game, device, created_at, last_seen").eq("owner", owner).order("created_at"),
  ]);
  const games = new Map<string, { bundle: string; game: string; backups: unknown[]; devices: unknown[] }>();
  const gameOf = (bundle: string, game: string) => {
    if (!games.has(bundle)) games.set(bundle, { bundle, game, backups: [], devices: [] });
    return games.get(bundle)!;
  };
  for (const b of backups ?? []) gameOf(b.bundle, b.game).backups.push(backupRow(b));
  for (const d of devices ?? []) gameOf(d.bundle, d.game).devices.push({ id: d.id, device: d.device, created_at: d.created_at, last_seen: d.last_seen });
  return answer(200, { games: [...games.values()] });
}

async function removeOwn(owner: string, body: Record<string, unknown>) {
  const { data: row } = await admin.from("save_backups").select("id, path").eq("id", clean(body.id, 64)).eq("owner", owner).maybeSingle();
  if (row) await removeBackups([row]);
  return answer(200, {});
}

async function forgetDevice(owner: string, body: Record<string, unknown>) {
  await admin.from("save_devices").delete().eq("id", clean(body.device_id, 64)).eq("owner", owner);
  return answer(200, {});
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  if (req.method !== "POST") return fail(405, "Use POST");
  const op = new URL(req.url).searchParams.get("op") ?? "";
  let body: Record<string, unknown> = {};
  try {
    const parsed = await req.json();
    if (parsed && typeof parsed === "object") body = parsed;
  } catch { /* an empty body is {} */ }
  try {
    if (op === "link-start") return await linkStart(req, body);
    if (op === "link-poll") return await linkPoll(body);
    if (["me", "backup-start", "backup-finish", "list", "restore", "unlink"].includes(op)) {
      const dev = await deviceFor(req);
      if (!dev) return fail(401, "This device is not linked any more");
      if (op === "me") return answer(200, { account: await accountOf(dev.owner), bundle: dev.bundle, game: dev.game });
      if (op === "backup-start") return await backupStart(dev, body);
      if (op === "backup-finish") return await backupFinish(dev, body);
      if (op === "list") return await list(dev);
      if (op === "restore") return await restore(dev, body);
      await admin.from("save_devices").delete().eq("id", dev.id);
      return answer(200, {});
    }
    if (["approve", "mine", "delete", "forget-device"].includes(op)) {
      const user = await userFor(req);
      if (!user) return fail(401, "Sign in first");
      if (op === "approve") return await approve(user.id, body);
      if (op === "mine") return await mine(user.id);
      if (op === "delete") return await removeOwn(user.id, body);
      return await forgetDevice(user.id, body);
    }
    return fail(404, "Unknown request");
  } catch (e) {
    console.warn(`saves ${op}: ${e instanceof Error ? e.message : e}`);
    return fail(503, "Accretion could not do that right now: try again");
  }
});
