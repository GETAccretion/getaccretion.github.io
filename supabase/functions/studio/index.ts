// studio: the admin numbers and moderation for Mod Studio's Store tab. Mod Studio links once like a game's cloud saves
// (saves?op=link-start with bundle "accretion.studio", approved at #/link while signed in); its device token is
// accepted here only when the linked account is an admin.
//
//   POST /functions/v1/studio?op=summary {apps?: ["io.github.accretionmods.x", ...]} with x-save-token
//     → {account, generated_at, sections: {accounts, downloads, apps, community, searches, share_links, cloud_saves,
//        builds}}; a section that fails carries {error} instead, so one slow table never hides the rest.
//   POST ?op=moderation → {reports (open, oldest first), recent posts, hidden posts, banned, team}
//   POST ?op=act {action: hide|resolve|ban|role|supporter, target, flag?, value?} → one action through
//     public.studio_moderate (server key only; it checks the admin again and records them on resolved reports).
//
// Deployed with --no-verify-jwt like the others; the token and the admin role are the check.
import { createClient } from "npm:@supabase/supabase-js@2.117.2";

const admin = createClient(Deno.env.get("SUPABASE_URL")!, Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!);
const STUDIO_BUNDLE = "accretion.studio";
const DAY = 86400_000;

const cors = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type, x-save-token",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};
const answer = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { ...cors, "Content-Type": "application/json", "Cache-Control": "no-store" } });

async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const since = (days: number) => new Date(Date.now() - days * DAY).toISOString();
const dayOf = (iso: string) => String(iso).slice(0, 10);

// rows of a table, every page (the summary reads at most `cap` rows per table)
async function rows(table: string, columns: string, filter: (q: any) => any = (q) => q, cap = 20000) {
  const out: any[] = [];
  for (let from = 0; from < cap; from += 1000) {
    const { data, error } = await filter(admin.from(table).select(columns)).range(from, from + 999);
    if (error) throw new Error(`${table}: ${error.message}`);
    out.push(...(data ?? []));
    if (!data || data.length < 1000) break;
  }
  return out;
}

async function count(table: string, filter: (q: any) => any = (q) => q): Promise<number> {
  const { count, error } = await filter(admin.from(table).select("*", { count: "exact", head: true }));
  if (error) throw new Error(`${table}: ${error.message}`);
  return count ?? 0;
}

async function section(fn: () => Promise<unknown>) {
  try { return await fn(); } catch (e) { return { error: e instanceof Error ? e.message : String(e) }; }
}

const accounts = () => section(async () => ({
  total: await count("profiles"),
  new_7d: await count("profiles", (q) => q.gte("created_at", since(7))),
  new_30d: await count("profiles", (q) => q.gte("created_at", since(30))),
  banned: await count("profiles", (q) => q.eq("banned", true)),
  moderators: await count("profiles", (q) => q.in("role", ["mod", "admin"])),
}));

const downloads = () => section(async () => {
  const log = await rows("download_log", "bundle_id, via, at", (q) => q.gte("at", since(30)));
  const perDay: Record<string, number> = {};
  for (let d = 13; d >= 0; d--) perDay[dayOf(new Date(Date.now() - d * DAY).toISOString())] = 0;
  const perApp: Record<string, { d7: number; d30: number }> = {};
  const via: Record<string, number> = {};
  const weekAgo = Date.now() - 7 * DAY;
  for (const r of log) {
    const day = dayOf(r.at);
    if (day in perDay) perDay[day]++;
    const a = (perApp[r.bundle_id] ??= { d7: 0, d30: 0 });
    a.d30++;
    if (new Date(r.at).getTime() >= weekAgo) a.d7++;
    via[r.via] = (via[r.via] ?? 0) + 1;
  }
  const top = Object.entries(perApp).sort((x, y) => y[1].d7 - x[1].d7 || y[1].d30 - x[1].d30).slice(0, 25)
    .map(([bundle, n]) => ({ bundle, ...n }));
  return { total_7d: log.filter((r) => new Date(r.at).getTime() >= weekAgo).length, total_30d: log.length,
    per_day: Object.entries(perDay).map(([day, n]) => ({ day, n })), via, top, per_app: perApp };
});

// views, searches and installs (app_hits), likes and ratings, per app; ours by the ids Studio sends
const apps = (ids: string[]) => section(async () => {
  const hits = await rows("app_hits", "app_id, day, searches, views, installs", (q) => q.gte("day", since(30).slice(0, 10)));
  const weekAgo = since(7).slice(0, 10);
  const per: Record<string, any> = {};
  for (const h of hits) {
    const a = (per[h.app_id] ??= { views_7d: 0, views_30d: 0, searches_7d: 0, installs_7d: 0, installs_30d: 0 });
    a.views_30d += h.views; a.installs_30d += h.installs;
    if (h.day >= weekAgo) { a.views_7d += h.views; a.searches_7d += h.searches; a.installs_7d += h.installs; }
  }
  const ours: Record<string, any> = {};
  for (const id of ids) {
    const likes = await count("app_likes", (q) => q.eq("app_id", id));
    const ratings = await rows("app_ratings", "stars", (q) => q.eq("app_id", id), 5000);
    const comments = await count("posts", (q) => q.eq("app_id", id).eq("hidden", false));
    ours[id] = { ...(per[id] ?? {}), likes, ratings: ratings.length, comments,
      rating: ratings.length ? Math.round(ratings.reduce((s: number, r: any) => s + r.stars, 0) / ratings.length * 10) / 10 : null };
  }
  const trending = Object.entries(per).map(([id, a]) => ({ id, ...a, score: 3 * a.searches_7d + 2 * a.installs_7d + a.views_7d }))
    .sort((x, y) => y.score - x.score).slice(0, 15);
  return { ours, trending };
});

const community = () => section(async () => ({
  posts_7d: await count("posts", (q) => q.gte("created_at", since(7))),
  posts_hidden: await count("posts", (q) => q.eq("hidden", true)),
  reports_open: await count("reports", (q) => q.eq("status", "open")),
  install_reports_7d: await count("install_reports", (q) => q.gte("created_at", since(7))),
  install_reports_recent: (await rows("install_reports", "app_version, ios, stage, bundle_id, created_at",
    (q) => q.order("created_at", { ascending: false }), 10)).slice(0, 10),
}));

const searches = () => section(async () => {
  const misses = await rows("search_misses", "query, day, hits", (q) => q.gte("day", since(7).slice(0, 10)));
  const per: Record<string, number> = {};
  for (const m of misses) per[m.query] = (per[m.query] ?? 0) + m.hits;
  return { missing_top: Object.entries(per).sort((a, b) => b[1] - a[1]).slice(0, 20).map(([query, hits]) => ({ query, hits })) };
});

const shareLinks = () => section(async () => {
  const links = await rows("share_links", "id, name, note, expires_at, revoked, downloads, last_download_at, created_at");
  const now = Date.now();
  const active = links.filter((l) => !l.revoked && new Date(l.expires_at).getTime() > now);
  return { active: active.length, downloads: links.reduce((s, l) => s + (l.downloads ?? 0), 0),
    links: active.map((l) => ({ id: l.id, name: l.name, note: l.note, expires_at: l.expires_at, downloads: l.downloads, last_download_at: l.last_download_at })) };
});

const cloudSaves = () => section(async () => {
  const devices = await rows("save_devices", "bundle, game, last_seen, created_at");
  const backups = await rows("save_backups", "bundle, game, size, created_at", (q) => q.eq("finished", true));
  const per: Record<string, any> = {};
  for (const d of devices) {
    if (d.bundle === STUDIO_BUNDLE) continue;
    (per[d.bundle] ??= { game: d.game, devices: 0, backups: 0, bytes: 0 }).devices++;
  }
  for (const b of backups) {
    const g = (per[b.bundle] ??= { game: b.game, devices: 0, backups: 0, bytes: 0 });
    g.backups++; g.bytes += b.size;
  }
  return { devices: devices.filter((d) => d.bundle !== STUDIO_BUNDLE).length, backups: backups.length,
    bytes: backups.reduce((s, b) => s + b.size, 0), backups_7d: backups.filter((b) => b.created_at >= since(7)).length, per_game: per };
});

const builds = () => section(async () => {
  const hosted = await rows("hosted_builds", "bundle_id, version, size, created_at", (q) => q.order("created_at", { ascending: false }), 2000);
  const latest: Record<string, any> = {};
  for (const h of hosted) latest[h.bundle_id] ??= { version: h.version, size: h.size, created_at: h.created_at, builds: 0 };
  for (const h of hosted) latest[h.bundle_id].builds++;
  return { latest };
});

// the moderation queue and lists, like the site's #/mod page
async function moderation() {
  const { data: reports, error } = await admin.from("reports")
    .select("id,reason,status,created_at,post:posts(id,body,app_id,hidden,created_at,author_p:profiles!posts_author_fkey(id,username,banned,role)),profile:profiles!reports_profile_id_fkey(id,username,banned,role),reporter_p:profiles!reports_reporter_fkey(username)")
    .eq("status", "open").order("created_at", { ascending: true }).limit(100);
  if (error) throw new Error(`reports: ${error.message}`);
  const recent = await section(async () => (await rows("posts", "id,body,app_id,hidden,created_at,parent_id,author_p:profiles!posts_author_fkey(id,username,banned,role)",
    (q) => q.order("created_at", { ascending: false }), 40)).slice(0, 40));
  const hidden = await section(async () => (await rows("posts", "id,body,app_id,created_at,author_p:profiles!posts_author_fkey(id,username,banned,role)",
    (q) => q.eq("hidden", true).order("created_at", { ascending: false }), 30)).slice(0, 30));
  const banned = await section(() => rows("profiles", "id,username,created_at", (q) => q.eq("banned", true).order("created_at", { ascending: false }), 200));
  const team = await section(() => rows("profiles", "id,username,role,supporter", (q) => q.or("role.in.(mod,admin),supporter.eq.true").order("username"), 200));
  return { reports: reports ?? [], recent, hidden, banned, team };
}

const ACTIONS = new Set(["hide", "resolve", "ban", "role", "supporter"]);

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
  if (req.method !== "POST") return answer(405, { error: "Use POST" });
  const op = new URL(req.url).searchParams.get("op") ?? "";
  const token = req.headers.get("x-save-token") ?? "";
  if (token.length < 20) return answer(401, { error: "Link Mod Studio first" });
  const { data: device } = await admin.from("save_devices").select("id, owner, bundle").eq("token_hash", await sha256(token)).maybeSingle();
  if (!device || device.bundle !== STUDIO_BUNDLE) return answer(401, { error: "Mod Studio is not linked any more" });
  const { data: profile } = await admin.from("profiles").select("role, banned").eq("id", device.owner).maybeSingle();
  if (!profile || profile.role !== "admin" || profile.banned) return answer(403, { error: "This account is not an admin" });
  await admin.from("save_devices").update({ last_seen: new Date().toISOString() }).eq("id", device.id);
  let body: any = {};
  try { body = await req.json(); } catch { /* {} */ }
  if (op === "moderation") {
    try { return answer(200, await moderation()); } catch (e) { return answer(500, { error: e instanceof Error ? e.message : String(e) }); }
  }
  if (op === "act") {
    // one moderation action, through public.studio_moderate (it checks the admin again and names them)
    const action = String(body.action ?? "");
    const target = String(body.target ?? "").slice(0, 100);
    if (!ACTIONS.has(action) || !target) return answer(400, { error: "Unknown action" });
    const { data, error } = await admin.rpc("studio_moderate", { p_actor: device.owner, p_action: action, p_target: target,
      p_flag: typeof body.flag === "boolean" ? body.flag : null, p_value: typeof body.value === "string" ? body.value : null });
    if (error) return answer(400, { error: error.message });
    if (!data) return answer(404, { error: "Nothing matched (already gone, or an admin)" });
    return answer(200, { ok: true, result: data });
  }
  if (op !== "summary") return answer(404, { error: "Unknown request" });
  const ids = Array.isArray(body.apps) ? body.apps.filter((x: unknown) => typeof x === "string").slice(0, 30) : [];
  const { data: user } = await admin.auth.admin.getUserById(device.owner);
  const email = String(user?.user?.email ?? "");
  const [a, d, p, c, s, l, cs, b] = await Promise.all([accounts(), downloads(), apps(ids), community(), searches(), shareLinks(), cloudSaves(), builds()]);
  return answer(200, {
    account: email.includes("@") ? `${email[0]}•••${email.slice(email.indexOf("@"))}` : "admin",
    generated_at: new Date().toISOString(),
    sections: { accounts: a, downloads: d, apps: p, community: c, searches: s, share_links: l, cloud_saves: cs, builds: b },
  });
});
