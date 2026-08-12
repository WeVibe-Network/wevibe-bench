// ─────────────────────────────────────────────────────────────────────────────
// SOURCE: control-plane  [OPT-IN — network]
//
// Consumes the host-side control plane (default http://127.0.0.1:7718), which
// owns the four surfaces the read-only board cannot own itself: the model
// roster, run control, the live event feed, and extraction.
//
// ── WHY THIS IS A SOURCE MODULE AND NOT A SERVER CHANGE ──────────────────────
//
// The dashboard is read-only by construction: GET only, bench repo mounted
// `:ro`, no docker socket, uid 1000. Those are kernel-enforced properties, and
// they are what make "the dashboard corrupted a run" impossible rather than
// merely unlikely. Adding write routes here would trade that for convenience.
//
// So the board READS the control plane exactly like any other source, and the
// browser posts to the control plane DIRECTLY for the two write actions. The
// dashboard server never proxies a write, never spawns a process, and keeps
// every safety property it had before this feature existed.
//
// If the control plane is not running, this module reports `unwired` with a
// reason and the board loses its control affordances while every measurement
// panel renders exactly as before. That degradation is the designed behaviour,
// not a failure path.
//
// READ-ONLY: GET only, against three endpoints. This module never posts.
// ─────────────────────────────────────────────────────────────────────────────

export const id = "control-plane";
export const fields = ["control", "events", "extraction"];
export function describe() {
  return "host-side control plane — roster, run control, event feed, extraction (opt-in)";
}

/** One bounded GET. Never throws; a failure becomes a null section. */
async function get(url, timeoutMs = 1500) {
  try {
    const res = await fetch(url, {
      signal: AbortSignal.timeout(timeoutMs),
      headers: { accept: "application/json" },
    });
    if (!res.ok) return { ok: false, reason: `HTTP ${res.status}` };
    return { ok: true, data: await res.json() };
  } catch (err) {
    return { ok: false, reason: String(err?.message ?? err) };
  }
}

export async function read(ctx) {
  const base = ctx.config?.controlUrl ?? "http://127.0.0.1:7718";
  // What the BROWSER is told to POST to. Inside a container `base` is a
  // container-side address the browser cannot reach, so the public URL is
  // configured separately and only falls back to `base` when they are the same
  // host (the bare `node server.mjs` case).
  const publicBase = ctx.config?.controlPublicUrl ?? base;

  // Capabilities first: it is the cheapest call and its failure is the whole
  // answer — if the control plane is down, nothing else is worth asking.
  const caps = await get(`${base}/api/capabilities`);
  if (!caps.ok) {
    return {
      ok: false,
      reason:
        `control plane unreachable at ${base} (${caps.reason}) — ` +
        "start it with `node control/server.mjs`. The board stays fully " +
        "functional; only the control surfaces are unavailable.",
    };
  }

  // The remaining three are independent: any one may be unwired without
  // invalidating the others, so each failure is recorded rather than aborting.
  const [roster, run, events, extraction] = await Promise.all([
    get(`${base}/api/roster`, 2500),
    get(`${base}/api/run`),
    get(`${base}/api/events?limit=400`, 2500),
    get(`${base}/api/extraction`),
  ]);

  const notes = [];
  if (!roster.ok) notes.push(`roster unwired — ${roster.reason}`);
  if (!run.ok) notes.push(`run state unwired — ${run.reason}`);
  if (!events.ok) notes.push(`event feed unwired — ${events.reason}`);
  if (!extraction.ok) notes.push(`extraction unwired — ${extraction.reason}`);

  return {
    ok: true,
    provenance: { path: base, mtime: Date.now(), bytes: null },
    patch: {
      control: {
        // The base url is published so the browser knows where to POST. The
        // dashboard server itself never posts anywhere.
        base_url: publicBase,
        contract_version: caps.data?.contract_version ?? null,
        capabilities: caps.data ?? null,
        roster: roster.ok ? roster.data : null,
        run: run.ok ? run.data : null,
        notes,
      },
      events: events.ok ? events.data : null,
      extraction: extraction.ok ? extraction.data : null,
    },
  };
}
