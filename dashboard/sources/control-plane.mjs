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
export const fields = ["control", "events", "extraction", "hold", "tui", "profile", "profiles"];
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

/**
 * Is this URL a loopback address — i.e. one that only resolves to whichever
 * machine dereferences it?
 *
 * Parsed rather than string-matched: `127.0.0.1`, `localhost` and `::1` are all
 * loopback, and the whole 127/8 block counts.
 *
 * The 127/8 test requires FOUR NUMERIC OCTETS and is anchored at both ends. A
 * looser `/^127\./` also matches the hostname `127.0.0.1.evil.example`, which
 * is an ordinary DNS name someone else controls — it would be classified as
 * loopback and the board would suppress its own warning. Caught by test.
 */
export function isLoopback(url) {
  let host;
  try {
    host = new URL(url).hostname;
  } catch {
    return false;
  }
  const h = host.replace(/^\[|\]$/g, "").toLowerCase();
  if (h === "localhost" || h === "::1") return true;
  const v4 = h.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!v4) return false;
  const octets = v4.slice(1).map(Number);
  if (octets.some((o) => o > 255)) return false;
  return octets[0] === 127;
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

  // The remaining calls are independent: any one may be unwired without
  // invalidating the others, so each failure is recorded rather than aborting.
  const [roster, run, events, extraction, hold, tui, profiles] = await Promise.all([
    get(`${base}/api/roster`, 2500),
    get(`${base}/api/run`),
    get(`${base}/api/events?limit=400`, 2500),
    get(`${base}/api/extraction`),
    get(`${base}/api/hold`),
    get(`${base}/api/tui`, 2500),
    get(`${base}/api/profiles`),
  ]);

  const notes = [];
  if (!roster.ok) notes.push(`roster unwired — ${roster.reason}`);
  if (!run.ok) notes.push(`run state unwired — ${run.reason}`);
  if (!events.ok) notes.push(`event feed unwired — ${events.reason}`);
  if (!extraction.ok) notes.push(`extraction unwired — ${extraction.reason}`);
  if (!hold.ok) notes.push(`hold unwired — ${hold.reason}`);
  if (!tui.ok) notes.push(`tui unwired — ${tui.reason}`);
  if (!profiles.ok) notes.push(`profiles unwired — ${profiles.reason}`);

  // THE FROZEN PROFILE, PROJECTED ONTO THE CONTRACT'S `profile` GROUP.
  //
  // This projection is what makes a created profile survive a refresh. It was
  // previously written only into browser memory by the modal's create handler
  // and was overwritten by the very next poll, because nothing on the read path
  // produced it — which is why creating a profile appeared to do nothing.
  //
  // `enforced` is copied from the service rather than defaulted here: one
  // source of truth for the debt badge, so it cannot be silently dropped by a
  // renderer that forgets to pass it along.
  //
  // `transfer` is passed through from the service, NOT recomputed here. It is
  // derived in exactly one place (control/profiles.mjs `transferOf`) so a
  // renderer can never disagree with the service about which experiment this is.
  const active = profiles.ok ? (profiles.data?.active ?? null) : null;
  const profile = active
    ? {
        exists: true,
        id: active.id,
        subject_model: typeof active.subject_model === "string" ? active.subject_model : null,
        memory_models: Array.isArray(active.memory_models) ? active.memory_models : [],
        transfer: active.transfer ?? null,
        created_at: active.created_at ?? null,
        enforced: active.enforced === true,
        stack_id: active.stack_id ?? null,
        runs: Array.isArray(active.runs) ? active.runs : [],
      }
    : {
        exists: false,
        id: null,
        subject_model: null,
        memory_models: [],
        transfer: null,
        created_at: null,
        enforced: false,
        stack_id: null,
        runs: [],
      };

  return {
    ok: true,
    provenance: { path: base, mtime: Date.now(), bytes: null },
    patch: {
      control: {
        // The base url is published so the browser knows where to POST. The
        // dashboard server itself never posts anywhere.
        base_url: publicBase,
        // ── IS THAT ADDRESS REACHABLE FROM THE BROWSER? ───────────────────
        // `base_url` is a LOOPBACK address, and loopback means "the machine
        // running the browser". That is correct only when the operator is
        // browsing from the host. Open the board from another device — the
        // documented LAN case — and every control POST resolves to that
        // DEVICE's own loopback, so it fails before leaving it.
        //
        // The control plane cannot simply be published on the LAN instead: it
        // binds 127.0.0.1 with no --host flag as a deliberate safety property
        // (control/server.mjs:23-25). It spawns processes; the read-only board
        // may be exposed, the control plane may not.
        //
        // So the board must be able to SAY this rather than render controls
        // that are guaranteed to fail. The browser resolves the verdict — only
        // it knows its own origin — and this flag tells it what to compare
        // against, so the rule lives in one place.
        base_url_is_loopback: isLoopback(publicBase),
        contract_version: caps.data?.contract_version ?? null,
        capabilities: caps.data ?? null,
        roster: roster.ok ? roster.data : null,
        run: run.ok ? run.data : null,
        notes,
      },
      events: events.ok ? events.data : null,
      extraction: extraction.ok ? extraction.data : null,
      hold: hold.ok ? hold.data : null,
      tui: tui.ok ? tui.data : null,
      profile,
      // The full set, including PRIOR profiles — the inspector's curve overlay
      // draws them hollow and never joins them to the active series.
      profiles: profiles.ok ? profiles.data : null,
    },
  };
}
