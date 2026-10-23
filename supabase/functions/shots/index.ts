// shots: screenshot and short video links for posts (Mod Studio's post drafts, ShareX on the owner's PC).
//
//   GET  /functions/v1/shots?id=<id>                 → 302 to the file (a signed link, 1 hour); 410 once expired
//   POST /functions/v1/shots?op=upload&ttl=3m|6m|1y&label=...   body: the file, Content-Type image/* or video/*
//        → {id, url, expires_at}
//   POST /functions/v1/shots?op=renew  {id, ttl}     → {id, expires_at}   also brings back an expired link
//   POST /functions/v1/shots?op=list                 → {shots: [...]} newest first
//   POST /functions/v1/shots?op=delete {id}          → {ok}
//
// POSTs need "x-upload-key: <UPLOAD_KEY>" (the same key as the upload function). Files sit in the private "shots"
// bucket; a link works for anyone who has it until it expires. An expired link can be renewed for 30 days, then
// its file is deleted (each upload clears those). Deployed with --no-verify-jwt; the key is the check.
import { createClient } from "npm:@supabase/supabase-js@2.117.2";

const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
const BUCKET = "shots";
const MAX_BYTES = 50 * 1024 * 1024;  // the free plan's per-file limit
const DAY = 86400_000;
const TTL: Record<string, number> = { "3m": 92, "6m": 183, "1y": 365 };
const KEEP_AFTER_EXPIRY = 30;  // days an expired link can still be renewed
const TYPES: Record<string, string> = {
  "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
  "video/mp4": "mp4", "video/quicktime": "mov",
};

const reply = (status: number, body: Record<string, unknown>) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });

function same(a: string, b: string): boolean {
  const x = new TextEncoder().encode(a);
  const y = new TextEncoder().encode(b);
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

function newId(): string {
  const abc = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = crypto.getRandomValues(new Uint8Array(12));
  return [...bytes].map((b) => abc[b % abc.length]).join("");
}

const linkOf = (id: string) => `${Deno.env.get("SUPABASE_URL")}/functions/v1/shots?id=${id}`;
const until = (ttl: string) => new Date(Date.now() + TTL[ttl] * DAY).toISOString();

// files whose link expired more than KEEP_AFTER_EXPIRY days ago
async function clearOld() {
  const cutoff = new Date(Date.now() - KEEP_AFTER_EXPIRY * DAY).toISOString();
  const { data } = await admin.from("shots").select("id, path").lt("expires_at", cutoff).limit(100);
  if (!data?.length) return;
  await admin.storage.from(BUCKET).remove(data.map((r: { path: string }) => r.path));
  await admin.from("shots").delete().in("id", data.map((r: { id: string }) => r.id));
}

Deno.serve(async (req) => {
  const url = new URL(req.url);
  if (req.method === "GET") {
    const id = url.searchParams.get("id") ?? "";
    if (!/^[A-Za-z0-9]{12}$/.test(id)) return reply(404, { message: "No such link." });
    const { data: shot } = await admin.from("shots").select("path, expires_at").eq("id", id).maybeSingle();
    if (!shot) return reply(404, { message: "No such link." });
    if (new Date(shot.expires_at).getTime() < Date.now()) return reply(410, { message: "This link has expired." });
    const { data, error } = await admin.storage.from(BUCKET).createSignedUrl(shot.path, 3600);
    if (error || !data) return reply(502, { message: "Couldn't open the file. Try again." });
    return new Response(null, { status: 302, headers: { Location: data.signedUrl, "Cache-Control": "private, max-age=600" } });
  }
  if (req.method !== "POST") return reply(405, { message: "Use GET or POST." });
  const expected = Deno.env.get("UPLOAD_KEY") ?? "";
  if (expected.length < 32 || !same(req.headers.get("x-upload-key") ?? "", expected)) return reply(401, { message: "Not allowed." });
  const op = url.searchParams.get("op") ?? "";

  if (op === "upload") {
    const ttl = url.searchParams.get("ttl") ?? "3m";
    if (!(ttl in TTL)) return reply(400, { message: "ttl must be 3m, 6m or 1y." });
    const type = (req.headers.get("content-type") ?? "").split(";")[0].trim().toLowerCase();
    if (!(type in TYPES)) return reply(415, { message: "Send a PNG, JPEG, GIF, WebP, MP4 or MOV file." });
    const file = new Uint8Array(await req.arrayBuffer());
    if (!file.length || file.length > MAX_BYTES) return reply(413, { message: "The file must be 1 byte to 50 MB." });
    const id = newId();
    const path = `${id}.${TYPES[type]}`;
    const { error: upErr } = await admin.storage.from(BUCKET).upload(path, file, { contentType: type, upsert: false });
    if (upErr) return reply(502, { message: `Couldn't store the file: ${upErr.message}` });
    const label = (url.searchParams.get("label") ?? "").slice(0, 120);
    const row = { id, path, content_type: type, size: file.length, label, expires_at: until(ttl) };
    const { error } = await admin.from("shots").insert(row);
    if (error) {
      await admin.storage.from(BUCKET).remove([path]);
      return reply(500, { message: error.message });
    }
    await clearOld().catch(() => {});
    return reply(200, { id, url: linkOf(id), expires_at: row.expires_at });
  }

  let body: { id?: string; ttl?: string } = {};
  try { body = await req.json(); } catch { /* {} */ }
  if (op === "list") {
    const { data, error } = await admin.from("shots").select("id, label, content_type, size, created_at, expires_at")
      .order("created_at", { ascending: false }).limit(500);
    if (error) return reply(500, { message: error.message });
    return reply(200, { shots: (data ?? []).map((r: { id: string }) => ({ ...r, url: linkOf(r.id) })) });
  }
  const id = String(body.id ?? "");
  if (!/^[A-Za-z0-9]{12}$/.test(id)) return reply(400, { message: "Send the link's id." });
  if (op === "renew") {
    const ttl = String(body.ttl ?? "3m");
    if (!(ttl in TTL)) return reply(400, { message: "ttl must be 3m, 6m or 1y." });
    const { data, error } = await admin.from("shots").update({ expires_at: until(ttl) }).eq("id", id).select("id, expires_at").maybeSingle();
    if (error) return reply(500, { message: error.message });
    if (!data) return reply(404, { message: "No such link (or its file is already deleted)." });
    return reply(200, data);
  }
  if (op === "delete") {
    const { data } = await admin.from("shots").select("path").eq("id", id).maybeSingle();
    if (!data) return reply(404, { message: "No such link." });
    await admin.storage.from(BUCKET).remove([data.path]);
    await admin.from("shots").delete().eq("id", id);
    return reply(200, { ok: true });
  }
  return reply(400, { message: "op must be upload, renew, list or delete." });
});
