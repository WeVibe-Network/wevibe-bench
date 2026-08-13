// ─────────────────────────────────────────────────────────────────────────────
// EVENT PROXY — the live agent activity stream
//
// Subscribes to the worker's `opencode serve` GET /event (text/event-stream)
// and re-publishes a MAPPED, BOUNDED view to the board.
//
// ── WHY PROXY RATHER THAN LET THE BROWSER CONNECT DIRECTLY ───────────────────
//
// Three reasons, all structural:
//
//  1. BOUNDING. Reasoning deltas arrive token-by-token and are unbounded. A
//     browser tab left open on a 12-hour run would accumulate every token the
//     model ever emitted. The ring buffer caps retention HERE, and the cap is
//     reported (`total` vs `returned`) rather than applied silently.
//
//  2. RECONNECTION. The worker's serve dies and restarts across a cell's life
//     (teardown, the per-attempt process kills). A browser EventSource would
//     surface each of those as a page-level error. This module reconnects with
//     backoff and reports connection state as DATA, so the board renders an
//     honest "feed down" instead of an empty panel that looks like silence.
//
//  3. ONE CONSUMER. The serve is the harness's own observation channel, and
//     WO-OBS-1 established that a failure of that channel is what voids a cell.
//     A single proxy connection is one predictable reader; N browser tabs are
//     N. The control plane must never be the reason the harness goes blind.
//
// ── WHAT THIS MODULE MUST NEVER DO ───────────────────────────────────────────
//
// It is READ-ONLY against the serve: GET /event and nothing else. It never
// posts a prompt, never aborts, never summarises. Driving the session is the
// harness's job exclusively — a control plane that can inject a turn can
// corrupt the measurement it is displaying.
// ─────────────────────────────────────────────────────────────────────────────

import { EVENT_MAP, EVENT_IGNORED, PART_KIND, EVENT_TEXT_MAX, EVENT_RING_MAX } from "./contract.mjs";

/** Truncate payload text and report whether it was cut. Never silently. */
function clipText(s) {
  const t = String(s ?? "");
  if (!t) return { text: null, truncated: false };
  if (t.length <= EVENT_TEXT_MAX) return { text: t, truncated: false };
  return { text: t.slice(0, EVENT_TEXT_MAX), truncated: true };
}

/**
 * Map one upstream event to the board shape. Returns null for an unmapped
 * type — the caller counts those rather than dropping them invisibly.
 */
export function mapEvent(raw) {
  const type = typeof raw?.type === "string" ? raw.type : null;
  if (!type) return null;
  if (EVENT_IGNORED.has(type)) return null;
  const envelopeKind = EVENT_MAP[type];
  if (!envelopeKind) return null;

  const p = raw.properties ?? {};
  const base = {
    id: typeof raw.id === "string" ? raw.id : null,
    kind: envelopeKind,
    type,
    at: null,
    session_id: typeof p.sessionID === "string" ? p.sessionID : null,
    tool: null,
    file: null,
    name: null,
    detail: null,
    text: null,
    truncated: false,
  };

  switch (type) {
    // The substantive channel. The ENVELOPE says only "a part changed" — the
    // Part's own `type` is what makes it a tool call, a thought, or a step, so
    // the kind is refined here rather than read off the envelope.
    case "message.part.updated":
      return fromPart(base, p.part, p.time);

    case "file.edited":
      base.name = "edit";
      base.file = typeof p.file === "string" ? p.file : null;
      base.detail = base.file;
      return base;

    case "session.error":
      base.kind = "error";
      base.name = "error";
      Object.assign(base, clipText(errorText(p.error)));
      base.detail = base.text;
      return base;

    case "session.idle":
      base.name = "idle";
      base.detail = "session went idle";
      return base;

    case "session.compacted":
      base.name = "compacted";
      base.detail = "context compacted";
      return base;

    // Carries the run's own status line. Useful, but only when it actually
    // says something — a status event with no status is not a row.
    case "session.status": {
      const s = typeof p.status === "string" ? p.status : null;
      if (!s) return null;
      base.name = "status";
      Object.assign(base, clipText(s));
      base.detail = base.text;
      return base;
    }

    default:
      return null;
  }
}

/**
 * Refine a `message.part.updated` into a feed row using the Part's own type.
 *
 * Returns null for parts that are real but must not become rows — `text` is
 * assistant prose that belongs in the TRANSCRIPT tab, and a part type we do not
 * recognise is counted as unmapped rather than rendered as a mystery row.
 */
function fromPart(base, part, time) {
  if (!part || typeof part !== "object") return null;
  const pt = typeof part.type === "string" ? part.type : null;
  const kind = pt ? PART_KIND[pt] : null;
  if (!kind) return null;

  base.kind = kind;
  base.type = `${base.type}:${pt}`;
  base.id = typeof part.id === "string" ? part.id : base.id;
  base.session_id = typeof part.sessionID === "string" ? part.sessionID : base.session_id;
  base.at = partTime(part, time);

  switch (pt) {
    case "tool": {
      base.tool = typeof part.tool === "string" ? part.tool : null;
      base.name = base.tool ?? "tool";
      const st = part.state ?? {};
      const status = typeof st.status === "string" ? st.status : null;

      // A failed tool call is an error the operator must see, even though it
      // arrives on the tool channel.
      if (status === "error") {
        base.kind = "error";
        Object.assign(base, clipText(errorText(st.error) ?? "tool failed"));
        base.detail = base.text;
        return base;
      }

      // Tool INPUT is deliberately summarised, not dumped: it can carry an
      // entire file body. The board shows WHAT ran, not its payload.
      const summary = summariseInput(st.input);
      Object.assign(base, clipText(summary));
      base.detail = [status, base.text].filter(Boolean).join(" · ") || status;
      return base;
    }

    case "reasoning": {
      // Reasoning text streams in via deltas we drop; the completed part is
      // often empty at the moment it updates. Duration is the honest signal.
      const ms = spanMs(part.time);
      base.name = "thinking";
      base.detail = ms == null ? "reasoning" : `reasoning ${Math.round(ms / 1000)}s`;
      return base;
    }

    case "patch": {
      const files = Array.isArray(part.files) ? part.files : [];
      base.name = "patch";
      base.file = files[0] ?? null;
      base.detail = files.length
        ? (files.length === 1 ? files[0] : `${files.length} files · ${files[0]}`)
        : "patch";
      return base;
    }

    case "step-start":
      base.name = "step";
      base.detail = "step started";
      return base;

    case "step-finish": {
      const t = part.tokens ?? {};
      const bits = [];
      if (typeof part.reason === "string") bits.push(part.reason);
      if (Number.isFinite(t.input)) bits.push(`in ${t.input}`);
      if (Number.isFinite(t.output)) bits.push(`out ${t.output}`);
      if (Number.isFinite(t.reasoning)) bits.push(`think ${t.reasoning}`);
      base.name = "step";
      base.detail = bits.join(" · ") || "step finished";
      return base;
    }

    default:
      return null;
  }
}

/**
 * Best-effort event time. Parts carry `time.start`/`time.end`; the envelope
 * carries its own `time`. Null is returned rather than Date.now() — stamping a
 * received-at time onto an event that never had one would fabricate ordering
 * evidence the feed then displays as fact.
 */
function partTime(part, envelopeTime) {
  const t = part?.time;
  if (t && typeof t === "object") {
    if (Number.isFinite(t.end)) return t.end;
    if (Number.isFinite(t.start)) return t.start;
  }
  if (Number.isFinite(envelopeTime)) return envelopeTime;
  return null;
}

function spanMs(t) {
  if (!t || typeof t !== "object") return null;
  if (!Number.isFinite(t.start)) return null;
  if (!Number.isFinite(t.end)) return null;
  return t.end - t.start;
}

/** Pull a human string out of the several error shapes upstream emits. */
function errorText(err) {
  if (!err) return null;
  if (typeof err === "string") return err;
  return (
    err?.data?.message ??
    err?.message ??
    err?.name ??
    null
  );
}

/**
 * Summarise a tool input without dumping it. A `write` call's input contains
 * the whole file; rendering that on a public stream is both noise and a
 * disclosure risk.
 */
function summariseInput(input) {
  if (!input || typeof input !== "object") return null;
  for (const key of ["filePath", "file_path", "path", "pattern", "command", "query"]) {
    const v = input[key];
    if (typeof v === "string" && v) return v;
  }
  const keys = Object.keys(input);
  return keys.length ? `${keys.length} arg${keys.length === 1 ? "" : "s"}` : null;
}

/**
 * A bounded ring of mapped events plus connection state.
 *
 * `total` counts everything ever seen, `unmapped` counts what was recognised
 * but not rendered. Both are exposed so the board can state "showing last N of
 * M" honestly instead of implying the feed is complete.
 */
export class EventRing {
  constructor(max = EVENT_RING_MAX) {
    this.max = max;
    this.items = [];
    this.total = 0;
    this.unmapped = 0;
    this.seq = 0;
    this.connected = false;
    this.reason = null;
    this.connected_at = null;
    this.last_event_at = null;
  }

  push(raw) {
    this.total += 1;
    const ev = mapEvent(raw);
    if (!ev) {
      this.unmapped += 1;
      return null;
    }
    this.seq += 1;
    // `seq` counts MAPPED events only, so it doubles as the mapped total and
    // is what `capped` is computed against. Using the raw total would report
    // the ring as "capped" merely because unmapped events (server.connected,
    // and every future upstream event type) were counted and discarded — a
    // false claim of data loss on an idle feed.
    ev.seq = this.seq;
    this.last_event_at = Date.now();
    this.items.push(ev);
    if (this.items.length > this.max) this.items.splice(0, this.items.length - this.max);
    return ev;
  }

  /**
   * Admit an ALREADY-MAPPED row into the ring, exactly once.
   *
   * ── WHY THIS EXISTS ────────────────────────────────────────────────────────
   *
   * `push()` is for RAW upstream events and runs them through `mapEvent()`.
   * Two row families never come from upstream — the harness's grading rows
   * (`control/gate-events.mjs`) and the verbatim messages the model was sent
   * (`control/feedback.mjs`). Both are rebuilt FROM FILES on every poll, and
   * both are already in BoardEvent shape, so they cannot go through `push()`.
   *
   * They were therefore given a `seq` at request time, computed from the ring's
   * CURRENT cursor. That cursor moves. So the same row was re-sequenced on every
   * poll, and the renderer — which appends anything with
   *   rows.filter((e) => (e.seq ?? -1) > renderedSeq)      [live.js]
   * — cleared that gate again each time and appended a fresh copy. Measured on a
   * live run: one `task chunk (attempt 1)` row came back as seq 706, then 713,
   * then higher, and the operator saw it repeated down the whole feed.
   *
   * ADMITTING ONCE FIXES IT AT THE SOURCE. The row enters the ring like any
   * other event, takes a seq from the SAME counter (so a collision with a real
   * upstream seq is structurally impossible, not merely unlikely), and every
   * downstream mechanism — `since`, `cursor`, `capped` — works with no special
   * case. The caller keeps no seq bookkeeping of its own.
   *
   * IDENTITY IS REQUIRED. A row with no `id` cannot be recognised on the next
   * poll, so it is REFUSED rather than admitted repeatedly — silently letting it
   * in is precisely the defect above.
   *
   * Returns the admitted event, or null if it was a duplicate or had no id.
   */
  admit(ev) {
    if (!ev || typeof ev.id !== "string" || ev.id.length === 0) return null;
    if (this.admitted === undefined) this.admitted = new Set();
    if (this.admitted.has(ev.id)) return null;
    this.admitted.add(ev.id);

    this.seq += 1;
    const row = { ...ev, seq: this.seq };
    this.last_event_at = Date.now();
    this.items.push(row);
    if (this.items.length > this.max) this.items.splice(0, this.items.length - this.max);
    return row;
  }

  /**
   * A window of the ring, plus the honest counters.
   *
   * ORDER IS OLDEST-FIRST, and that is a design decision, not an accident.
   * The Episode ticker on the board is newest-first because each row is an
   * independent event you scan for. This feed is different: it is a TRANSCRIPT
   * of one continuous activity, so it must read top-to-bottom in the order the
   * agent did the work — a reversed transcript is unreadable as narrative, and
   * a reasoning delta above the tool call it preceded is actively misleading.
   * The panel header states the order so a viewer never has to infer it.
   *
   * `counts` is computed over the WHOLE retained ring, never over the returned
   * slice: the filter chips must keep showing "THINKING 768" while thinking is
   * filtered OUT, otherwise turning a filter on makes its own count vanish and
   * the operator loses the number that tells them what they are hiding.
   */
  snapshot({ limit = 100, kinds = null, since = 0 } = {}) {
    // Counts are per-kind over everything retained, independent of the filter.
    const counts = { tool: 0, file: 0, thinking: 0, error: 0, lifecycle: 0 };
    for (const e of this.items) {
      if (counts[e.kind] !== undefined) counts[e.kind] += 1;
    }

    let rows = this.items;
    if (kinds && kinds.length) rows = rows.filter((e) => kinds.includes(e.kind));
    if (since > 0) rows = rows.filter((e) => e.seq > since);

    // Take the TAIL (the most recent `limit`) but return it in chronological
    // order — newest events, oldest-first within the window.
    const returned = rows.slice(-limit);
    const filtered = kinds && kinds.length;

    return {
      connected: this.connected,
      reason: this.reason,
      connected_at: this.connected_at,
      last_event_at: this.last_event_at,
      order: "oldest_first",
      // Silence is information: how long since anything arrived.
      idle_s: this.last_event_at
        ? Math.round((Date.now() - this.last_event_at) / 1000)
        : null,
      events: returned,
      counts,
      returned: returned.length,
      retained: this.items.length,
      // `total` is every frame seen; `mapped` is how many were renderable.
      total: this.total,
      mapped: this.seq,
      unmapped: this.unmapped,
      // `capped` means the RING dropped MAPPED events (real loss from this
      // buffer). It is measured against mapped events only — counting
      // unmapped frames here reported an idle feed as "capped" purely because
      // `server.connected` had been received and discarded.
      // `windowed` means this response merely returned fewer than the ring
      // holds. The UI states them differently — a cap is "9,012 earlier events
      // not rendered", a window is just paging.
      capped: this.seq > this.items.length,
      windowed: rows.length > returned.length,
      hidden_by_filter: filtered ? this.items.length - rows.length : 0,
      max: this.max,
      cursor: this.seq,
    };
  }
}

/**
 * Subscribe to an SSE endpoint and feed a ring. Reconnects forever with
 * bounded backoff; every state change is recorded on the ring so the board can
 * render it.
 *
 * Returns a stop() function. Never throws.
 */
export function subscribe(url, ring, { minBackoffMs = 1000, maxBackoffMs = 15000 } = {}) {
  let stopped = false;
  let controller = null;
  let backoff = minBackoffMs;

  const connect = async () => {
    if (stopped) return;
    controller = new AbortController();
    try {
      const res = await fetch(url, {
        signal: controller.signal,
        headers: { accept: "text/event-stream" },
      });
      if (!res.ok || !res.body) {
        throw new Error(`HTTP ${res.status}`);
      }

      ring.connected = true;
      ring.reason = null;
      ring.connected_at = Date.now();
      backoff = minBackoffMs; // a successful connect resets the backoff

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (!stopped) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line. Parse only complete frames
        // and leave a partial tail in the buffer — a half-arrived frame is
        // normal, not an error.
        let idx;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          for (const line of frame.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trim();
            if (!payload) continue;
            try {
              ring.push(JSON.parse(payload));
            } catch {
              // A malformed frame is skipped, not fatal. Upstream owns the
              // format; we never crash the feed over one bad line.
            }
          }
        }
      }
      throw new Error("stream ended");
    } catch (err) {
      if (stopped) return;
      ring.connected = false;
      ring.reason = `event feed disconnected: ${String(err?.message ?? err)}`;
    } finally {
      try {
        controller?.abort();
      } catch {
        /* already aborted */
      }
    }

    if (stopped) return;
    const wait = backoff;
    backoff = Math.min(maxBackoffMs, Math.round(backoff * 1.7));
    setTimeout(connect, wait);
  };

  connect();

  return () => {
    stopped = true;
    try {
      controller?.abort();
    } catch {
      /* fine */
    }
  };
}
