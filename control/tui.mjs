// ─────────────────────────────────────────────────────────────────────────────
// TUI MIRROR — read-only capture of an `opencode attach` screen
//
// WHY A PTY AND NOT AN API
// opencode exposes 13 /tui/* routes and every one is a POST *command*
// (append-prompt, submit-prompt, execute-command). There is NO endpoint that
// returns the rendered screen, so the only way to show the operator's actual
// view is to run `opencode attach` against a real pseudo-terminal and interpret
// what it paints. Verified against a live cell: 190KB of frames in 20s.
//
// STRICTLY READ-ONLY — THIS IS THE LOAD-BEARING RULE
// We never write a single byte to the PTY master. The attached session is a
// LIVE benchmark cell; injecting a keystroke could submit a prompt, switch a
// model, or abort a run, corrupting a campaign that takes hours. The master fd
// is opened, read, and closed. `stdin` of the child is the pty slave and
// nothing ever reaches it.
//
// A SECOND ATTACH IS A SECOND CLIENT, NOT A PIXEL MIRROR
// `opencode attach` opens its OWN view of the session. It shows the same
// conversation, but its scroll position is independent of the operator's
// terminal. The surface says so rather than implying it is a screen-share.
//
// WHY A HAND-WRITTEN EMULATOR IS THE RIGHT SIZE HERE
// Measured over a real 190KB capture, the TUI uses exactly two sequences that
// move or mark anything: SGR (9075 occurrences, colour) and CUP (3345, absolute
// cursor position). No scroll regions, no insert/delete-line, no erase-display,
// no DECALN — and only 72 distinct colour payloads. So a cursor-addressable
// cell grid is sufficient and correct; a full VT100 (or a native node-pty
// dependency, which would break the dashboard's zero-dependency guarantee) is
// not needed. Sequences we do not implement are SKIPPED, never printed as
// literal garbage.
//
// ON-DEMAND WITH A HARD IDLE STOP
// The capture starts when the TUI tab is opened and stops itself once nothing
// has polled for it. A resident attach client for a 12-hour run is a cost with
// no reader, so idleness is the stop condition rather than trusting a UI to
// send a close.
// ─────────────────────────────────────────────────────────────────────────────

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";

/** Grid size. Fixed so the emitted frame is a predictable shape for the board. */
export const TUI_ROWS = 40;
export const TUI_COLS = 130;

/**
 * How long the capture survives with no reader.
 *
 * This MUST exceed the TUI's time-to-first-paint, which is ~10s against a live
 * session (the client connects, loads history, then paints). An idle window
 * shorter than that produces a capture that is repeatedly killed just before it
 * renders anything — the surface shows `painted:false` forever while the
 * process churns, which looks exactly like a broken capture.
 *
 * 30s is comfortably past first paint and still short enough that a closed
 * drawer does not leave a client attached to a live benchmark session.
 */
export const TUI_IDLE_STOP_MS = 30000;

/**
 * How long to wait for the first byte before reporting the capture as failed.
 * Startup is slow but not unbounded; silence past this is a real fault, and
 * saying so beats an empty screen with no explanation.
 */
export const TUI_FIRST_PAINT_TIMEOUT_MS = 25000;

/** Frames are only re-serialised when the screen actually changed. */
const DEFAULT_ATTACH_BIN = "opencode";

// ── the screen ───────────────────────────────────────────────────────────────

/**
 * A cursor-addressable cell grid.
 *
 * Each cell holds a character plus its foreground/background. Style is carried
 * per-cell rather than as spans because CUP lets the TUI paint anywhere at any
 * time — a span-based model would have to be rebuilt on every jump.
 */
class Screen {
  constructor(rows = TUI_ROWS, cols = TUI_COLS) {
    this.rows = rows;
    this.cols = cols;
    this.row = 0;
    this.col = 0;
    this.fg = null;
    this.bg = null;
    this.bold = false;
    this.dirty = true;
    this.cells = new Array(rows * cols);
    this.clear();
  }

  clear() {
    for (let i = 0; i < this.cells.length; i += 1) {
      this.cells[i] = { ch: " ", fg: null, bg: null, bold: false };
    }
    this.dirty = true;
  }

  put(ch) {
    if (this.row < 0 || this.row >= this.rows) return;
    if (this.col < 0 || this.col >= this.cols) return;
    const cell = this.cells[this.row * this.cols + this.col];
    cell.ch = ch;
    cell.fg = this.fg;
    cell.bg = this.bg;
    cell.bold = this.bold;
    this.col += 1;
    this.dirty = true;
  }

  /**
   * Serialise to rows of runs. Adjacent cells sharing a style collapse into one
   * run, which is what keeps a 40x130 frame small enough to poll: ~5200 cells
   * become a few hundred runs.
   */
  serialise() {
    const out = [];
    for (let r = 0; r < this.rows; r += 1) {
      const runs = [];
      let cur = null;
      for (let c = 0; c < this.cols; c += 1) {
        const cell = this.cells[r * this.cols + c];
        if (cur && cur.fg === cell.fg && cur.bg === cell.bg && cur.bold === cell.bold) {
          cur.t += cell.ch;
        } else {
          cur = { t: cell.ch, fg: cell.fg, bg: cell.bg, bold: cell.bold };
          runs.push(cur);
        }
      }
      // Trailing blank space carries no information and is a large share of a
      // mostly-empty terminal row, so it is dropped rather than transmitted.
      while (runs.length && runs[runs.length - 1].t.trim() === "" && runs[runs.length - 1].bg === null) {
        runs.pop();
      }
      out.push(runs);
    }
    return out;
  }
}

// ── the parser ───────────────────────────────────────────────────────────────

/**
 * Feed bytes, mutate the screen.
 *
 * Kept as an explicit state machine over a chunk boundary: a PTY read can split
 * an escape sequence anywhere, so a partial sequence is retained and completed
 * by the next chunk rather than being emitted as literal text.
 */
class AnsiParser {
  constructor(screen) {
    this.s = screen;
    this.pending = "";
  }

  write(str) {
    const buf = this.pending + str;
    this.pending = "";
    let i = 0;
    while (i < buf.length) {
      const ch = buf[i];

      if (ch !== "\x1b") {
        i += this.text(buf, i);
        continue;
      }

      // An ESC at the very end of a chunk is an incomplete sequence.
      if (i + 1 >= buf.length) { this.pending = buf.slice(i); return; }

      const next = buf[i + 1];

      if (next === "[") {
        // A CSI sequence is: ESC [ params INTERMEDIATES final
        // The intermediate bytes (0x20-0x2F: space ! " # $ % & ' ( ) * + , - . /)
        // are easy to forget and fatal to omit. This TUI emits DECRQM as
        // `ESC[?2026$p` — the `$` is an intermediate. A pattern that jumps
        // straight from params to a final byte does not match it, so the parser
        // treats the rest of the STREAM as one incomplete sequence and paints
        // nothing at all. That failure is total, not partial: 183073 of 183123
        // bytes were swallowed before this was fixed.
        const m = /^\x1b\[([0-9;:?<>!]*)([ -\/]*)([@-~])/.exec(buf.slice(i));
        if (!m) {
          // Genuinely incomplete only if it could still become valid; a bounded
          // guard stops a malformed stream buffering without limit.
          if (buf.length - i < 32) { this.pending = buf.slice(i); return; }
          i += 2;
          continue;
        }
        // A sequence carrying intermediates is a mode query/report, never
        // something that paints. Consume and ignore it.
        if (m[2]) { i += m[0].length; continue; }
        this.csi(m[1], m[3]);
        i += m[0].length;
        continue;
      }

      // OSC / DCS / APC: terminated by BEL or ST. These are queries and
      // notifications (title, colour probes, capability strings) that paint
      // nothing, so they are consumed and discarded.
      if (next === "]" || next === "P" || next === "_" || next === "^") {
        const rest = buf.slice(i);
        const end = /\x07|\x1b\\/.exec(rest);
        if (!end) { this.pending = rest; return; }
        i += end.index + end[0].length;
        continue;
      }

      // Two-byte escapes (charset selection, keypad mode). No visual effect.
      if (next === "(" || next === ")" || next === "=" || next === ">" || next === "<") {
        i += 2;
        continue;
      }

      i += 1;
    }
  }

  /** Consume a run of printable text, honouring the control chars that matter. */
  text(buf, i) {
    const ch = buf[i];
    if (ch === "\n") { this.s.row += 1; this.s.col = 0; return 1; }
    if (ch === "\r") { this.s.col = 0; return 1; }
    if (ch === "\t") { this.s.col = Math.min(this.s.cols - 1, (Math.floor(this.s.col / 8) + 1) * 8); return 1; }
    if (ch === "\b") { this.s.col = Math.max(0, this.s.col - 1); return 1; }
    if (ch === "\x07") return 1;
    if (ch < " ") return 1;
    this.s.put(ch);
    return 1;
  }

  csi(params, final) {
    const s = this.s;
    // `?` introduces a DEC private mode (h/l/$p). They toggle terminal
    // behaviour and paint nothing, so they are ignored wholesale.
    if (params.startsWith("?")) return;
    const nums = params.split(";").map((x) => (x === "" ? null : Number.parseInt(x, 10)));
    const n = (idx, dflt) => (Number.isFinite(nums[idx]) ? nums[idx] : dflt);

    switch (final) {
      case "H": case "f": // CUP — the workhorse: 1-based row;col
        s.row = n(0, 1) - 1;
        s.col = n(1, 1) - 1;
        return;
      case "A": s.row = Math.max(0, s.row - n(0, 1)); return;
      case "B": s.row = Math.min(s.rows - 1, s.row + n(0, 1)); return;
      case "C": s.col = Math.min(s.cols - 1, s.col + n(0, 1)); return;
      case "D": s.col = Math.max(0, s.col - n(0, 1)); return;
      case "G": s.col = n(0, 1) - 1; return;
      case "d": s.row = n(0, 1) - 1; return;
      case "J": { // ED
        const mode = n(0, 0);
        if (mode === 2 || mode === 3) { s.clear(); return; }
        const from = mode === 0 ? s.row * s.cols + s.col : 0;
        const to = mode === 0 ? s.cells.length : s.row * s.cols + s.col;
        for (let k = from; k < to; k += 1) s.cells[k] = { ch: " ", fg: null, bg: null, bold: false };
        s.dirty = true;
        return;
      }
      case "K": { // EL
        const mode = n(0, 0);
        const start = mode === 0 ? s.col : 0;
        const end = mode === 1 ? s.col + 1 : s.cols;
        for (let c = start; c < end; c += 1) {
          if (c >= 0 && c < s.cols) s.cells[s.row * s.cols + c] = { ch: " ", fg: null, bg: null, bold: false };
        }
        s.dirty = true;
        return;
      }
      case "X": { // ECH
        const count = n(0, 1);
        for (let c = s.col; c < Math.min(s.cols, s.col + count); c += 1) {
          s.cells[s.row * s.cols + c] = { ch: " ", fg: null, bg: null, bold: false };
        }
        s.dirty = true;
        return;
      }
      case "m": this.sgr(nums); return;
      default: return; // unimplemented — skipped, never printed as literal text
    }
  }

  sgr(nums) {
    const s = this.s;
    if (!nums.length || (nums.length === 1 && (nums[0] === null || nums[0] === 0))) {
      s.fg = null; s.bg = null; s.bold = false;
      return;
    }
    for (let i = 0; i < nums.length; i += 1) {
      const v = nums[i];
      if (v === null || v === 0) { s.fg = null; s.bg = null; s.bold = false; continue; }
      if (v === 1) { s.bold = true; continue; }
      if (v === 22) { s.bold = false; continue; }
      if (v === 39) { s.fg = null; continue; }
      if (v === 49) { s.bg = null; continue; }
      // 24-bit colour is what this TUI actually emits (38;2;r;g;b).
      if ((v === 38 || v === 48) && nums[i + 1] === 2) {
        const hex = rgb(nums[i + 2], nums[i + 3], nums[i + 4]);
        if (v === 38) s.fg = hex; else s.bg = hex;
        i += 4;
        continue;
      }
      if ((v === 38 || v === 48) && nums[i + 1] === 5) {
        const hex = xterm256(nums[i + 2]);
        if (v === 38) s.fg = hex; else s.bg = hex;
        i += 2;
        continue;
      }
      if (v >= 30 && v <= 37) { s.fg = BASIC[v - 30]; continue; }
      if (v >= 40 && v <= 47) { s.bg = BASIC[v - 40]; continue; }
      if (v >= 90 && v <= 97) { s.fg = BASIC_BRIGHT[v - 90]; continue; }
      if (v >= 100 && v <= 107) { s.bg = BASIC_BRIGHT[v - 100]; continue; }
    }
  }
}

const BASIC = ["#000000", "#cc5555", "#5ad27a", "#d7b562", "#82aaff", "#c792ea", "#5fb3b3", "#aeb9cc"];
const BASIC_BRIGHT = ["#43506a", "#ff6b6b", "#7ee08f", "#ffcb6b", "#9dbcff", "#e0b0ff", "#7fd8d8", "#e6edf6"];

function rgb(r, g, b) {
  const h = (x) => Math.max(0, Math.min(255, Number.isFinite(x) ? x : 0)).toString(16).padStart(2, "0");
  return `#${h(r)}${h(g)}${h(b)}`;
}

function xterm256(i) {
  if (!Number.isFinite(i)) return null;
  if (i < 8) return BASIC[i];
  if (i < 16) return BASIC_BRIGHT[i - 8];
  if (i < 232) {
    const n = i - 16;
    const steps = [0, 95, 135, 175, 215, 255];
    return rgb(steps[Math.floor(n / 36) % 6], steps[Math.floor(n / 6) % 6], steps[n % 6]);
  }
  const g = 8 + (i - 232) * 10;
  return rgb(g, g, g);
}

// ── the capture ──────────────────────────────────────────────────────────────

/**
 * One live capture of one session. Owns the child process, the screen, and the
 * idle timer that stops it.
 */
class Capture {
  constructor({ sessionId, serveUrl, bin }) {
    this.sessionId = sessionId;
    this.serveUrl = serveUrl;
    this.bin = bin;
    this.screen = new Screen();
    this.parser = new AnsiParser(this.screen);
    this.child = null;
    this.startedAt = null;
    this.lastReadAt = null;
    this.lastPollAt = Date.now();
    this.bytes = 0;
    this.error = null;
    this.exited = null;
    this.frame = null;
  }

  start() {
    if (this.child) return;
    // `script` gives us a pty without a native dependency. On darwin the form
    // is `script -q /dev/null <cmd> …`, which allocates a tty and relays the
    // child's output to OUR stdout pipe. We never write to the child's stdin —
    // it is set to 'ignore' so there is no path for a keystroke to reach a live
    // session even by accident.
    const argv = [
      "-q", "/dev/null",
      this.bin, "attach", this.serveUrl, "--session", this.sessionId,
    ];
    try {
      this.child = spawn("script", argv, {
        stdio: ["ignore", "pipe", "pipe"],
        env: {
          ...process.env,
          TERM: "xterm-256color",
          COLUMNS: String(TUI_COLS),
          LINES: String(TUI_ROWS),
          // Stop the attached client from trying to be clever about the
          // terminal it thinks it has.
          NO_COLOR: "",
          CI: "",
        },
      });
    } catch (err) {
      this.error = `could not spawn capture: ${String(err?.message ?? err)}`;
      return;
    }

    this.startedAt = Date.now();

    this.child.stdout.on("data", (chunk) => {
      this.bytes += chunk.length;
      this.lastReadAt = Date.now();
      this.parser.write(chunk.toString("utf8"));
    });
    // stderr is captured for diagnosis but never parsed as screen content.
    this.child.stderr.on("data", (chunk) => {
      const s = chunk.toString("utf8").trim();
      if (s) this.error = s.slice(0, 300);
    });
    this.child.on("error", (err) => { this.error = String(err?.message ?? err); });
    this.child.on("exit", (code, signal) => {
      this.exited = { code, signal: signal ?? null, at: Date.now() };
      this.child = null;
    });
  }

  stop() {
    const c = this.child;
    this.child = null;
    if (!c) return;
    // SIGTERM first so the client can drop its session connection cleanly; the
    // process is not left to linger if it ignores that.
    try { c.kill("SIGTERM"); } catch { /* already gone */ }
    setTimeout(() => { try { c.kill("SIGKILL"); } catch { /* already gone */ } }, 1500);
  }

  /** Serialise only when the screen changed since the last poll. */
  read() {
    this.lastPollAt = Date.now();
    if (this.screen.dirty || !this.frame) {
      this.frame = this.screen.serialise();
      this.screen.dirty = false;
    }
    // A blank screen has several distinct causes and they are NOT
    // interchangeable — "still starting" and "the client died" look identical
    // on screen and demand opposite reactions from the operator.
    const waited = this.startedAt ? Date.now() - this.startedAt : 0;
    let status = "live";
    let reason = null;
    if (this.exited) {
      status = "exited";
      reason = `capture client exited (code ${this.exited.code ?? "?"}${this.exited.signal ? `, ${this.exited.signal}` : ""})`;
    } else if (this.error && this.bytes === 0) {
      status = "failed";
      reason = this.error;
    } else if (this.bytes === 0 && waited < TUI_FIRST_PAINT_TIMEOUT_MS) {
      status = "starting";
      reason = "attaching — the client connects and loads history before it paints";
    } else if (this.bytes === 0) {
      status = "silent";
      reason = `no output after ${Math.round(waited / 1000)}s — the attach client produced nothing`;
    }

    return {
      session_id: this.sessionId,
      running: Boolean(this.child),
      status,
      reason,
      rows: this.screen.rows,
      cols: this.screen.cols,
      frame: this.frame,
      bytes: this.bytes,
      started_at: this.startedAt,
      last_data_at: this.lastReadAt,
      painted: this.bytes > 0,
      error: this.error,
      exited: this.exited,
    };
  }

  idleFor(now) {
    return now - this.lastPollAt;
  }
}

// ── the manager ──────────────────────────────────────────────────────────────

/**
 * At most ONE capture at a time. A second attach to a second session would
 * double the cost for a surface that shows one session, and the drawer only
 * ever displays the current cell.
 */
export class TuiMirror {
  constructor({ serveUrl, bin = DEFAULT_ATTACH_BIN }) {
    this.serveUrl = serveUrl;
    this.bin = bin;
    this.capture = null;
    this.sweeper = setInterval(() => this.sweep(), 2000);
    // Never hold the process open on this timer alone.
    if (this.sweeper.unref) this.sweeper.unref();
  }

  /** Whether the attach binary is even present. Reported, never assumed. */
  available() {
    if (this.bin.includes("/")) return existsSync(this.bin);
    return true; // resolved from PATH by spawn; a failure surfaces as `error`
  }

  /**
   * Poll for a frame, starting the capture if needed. Polling IS the keepalive:
   * the surface that reads the frames is what keeps them being produced.
   */
  poll(sessionId) {
    if (!sessionId) {
      return {
        running: false,
        session_id: null,
        frame: null,
        reason: "no session observed yet — the TUI mirror attaches to the running cell's session.",
      };
    }

    if (this.capture && this.capture.sessionId !== sessionId) {
      // The cell rolled over to a new session. The old view is not the current
      // one, so it is dropped rather than shown as if it were live.
      this.capture.stop();
      this.capture = null;
    }

    if (!this.capture) {
      this.capture = new Capture({ sessionId, serveUrl: this.serveUrl, bin: this.bin });
      this.capture.start();
    }

    return this.capture.read();
  }

  /** Stop a capture nobody is reading. */
  sweep() {
    if (!this.capture) return;
    if (this.capture.idleFor(Date.now()) > TUI_IDLE_STOP_MS) {
      this.capture.stop();
      this.capture = null;
    }
  }

  shutdown() {
    clearInterval(this.sweeper);
    if (this.capture) this.capture.stop();
    this.capture = null;
  }
}

// Exported for tests: the emulator is pure and can be driven without a process.
export { Screen, AnsiParser };
