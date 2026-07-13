// Backgammon HTTP server — Node standard library only, serves API + static UI on :8002.
import http from "node:http";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

import {
  type GameState,
  type Move,
  type AppliedMove,
  type Player,
  type Board,
  BAR,
  OFF,
  createGame,
  cloneBoard,
  legalMovesNow,
  applyMove,
  pipCount,
  checkWin,
  opponent,
} from "./game.ts";
import {
  chooseMoves,
  shouldAiDouble,
  shouldAiAccept,
  winProbability,
} from "./ai.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(__dirname, "..", "public");
const PORT = 8002;
const BENCH_DEBUG = process.env.BENCH_DEBUG === "1";
const HUMAN: Player = "white";
const AI: Player = "black";

// ---- single in-memory game with a couple of transient turn flags ----
interface FullState extends GameState {
  aiCubeDone: boolean;
  debugDiceQueue?: number[][];
}

let game: FullState = {} as FullState;

function init(difficulty: GameState["difficulty"]): FullState {
  throw new Error("not implemented");
}

function boardView(g: GameState): Board {
  throw new Error("not implemented");
}

function multiplier(type: "single" | "gammon" | "backgammon" | null): number {
  throw new Error("not implemented");
}

function humanCanDouble(g: FullState): boolean {
  throw new Error("not implemented");
}

// serialize the state the client needs
function serialize(g: FullState, extra: Record<string, unknown> = {}) {
  throw new Error("not implemented");
}

function finishGame(g: FullState, winner: Player, type: "single" | "gammon" | "backgammon" | null, declined: boolean) {
  throw new Error("not implemented");
}

// After a checker action, refresh turn-over status for the human.
function refreshHumanTurn(g: FullState) {
  throw new Error("not implemented");
}

function reverseMove(b: Board, player: Player, am: AppliedMove) {
  throw new Error("not implemented");
}

function rollDice(): number[] {
  throw new Error("not implemented");
}

// ---------------- action handlers ----------------

function actionNew(body: any) {
  throw new Error("not implemented");
}

function actionRoll() {
  throw new Error("not implemented");
}

function actionMove(body: any) {
  throw new Error("not implemented");
}

function actionUndo() {
  throw new Error("not implemented");
}

function actionEndTurn() {
  throw new Error("not implemented");
}

function handOverToAi() {
  throw new Error("not implemented");
}

// Human offers a double; AI decides immediately.
function actionDouble() {
  throw new Error("not implemented");
}

// Human responds to an AI-offered double.
function actionRespondDouble(body: any) {
  throw new Error("not implemented");
}

// Run one AI step: possibly offer a double, otherwise roll + move.
function actionAi() {
  throw new Error("not implemented");
}

function actionDebugState(body: any) {
  throw new Error("not implemented");
}

function actionDebugRoll(body: any) {
  throw new Error("not implemented");
}

// ---------------- HTTP plumbing ----------------

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};

function readBody(req: http.IncomingMessage): Promise<any> {
  return new Promise((resolve) => {
    let data = "";
    req.on("data", (c) => (data += c));
    req.on("end", () => {
      if (!data) return resolve({});
      try {
        resolve(JSON.parse(data));
      } catch {
        resolve({});
      }
    });
  });
}

async function serveStatic(req: http.IncomingMessage, res: http.ServerResponse, urlPath: string) {
  throw new Error("not implemented");
}

const server = http.createServer(async (req, res) => {
  const url = req.url || "/";
  try {
    if (req.method === "GET" && url.split("?")[0] === "/health") {
      const payload = JSON.stringify({ status: "ok", port: PORT });
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(payload);
      return;
    }

    if (url.startsWith("/api/")) {
      const body = req.method === "POST" ? await readBody(req) : {};
      let extra: Record<string, unknown> = {};
      switch (url.split("?")[0]) {
        case "/api/state":
          break;
        case "/api/new":
          actionNew(body);
          break;
        case "/api/roll":
          actionRoll();
          break;
        case "/api/move":
          actionMove(body);
          break;
        case "/api/undo":
          actionUndo();
          break;
        case "/api/endturn":
          actionEndTurn();
          break;
        case "/api/double":
          extra = actionDouble() || {};
          break;
        case "/api/double/respond":
          actionRespondDouble(body);
          break;
        case "/api/ai":
          extra = actionAi() || {};
          break;
        case "/api/debug/state":
          if (!BENCH_DEBUG) {
            res.writeHead(404, { "Content-Type": "application/json" }).end('{"error":"unknown endpoint"}');
            return;
          }
          extra = actionDebugState(body) || {};
          break;
        case "/api/debug/roll":
          if (!BENCH_DEBUG) {
            res.writeHead(404, { "Content-Type": "application/json" }).end('{"error":"unknown endpoint"}');
            return;
          }
          extra = actionDebugRoll(body) || {};
          break;
        default:
          res.writeHead(404, { "Content-Type": "application/json" }).end('{"error":"unknown endpoint"}');
          return;
      }
      const payload = JSON.stringify(serialize(game, extra));
      res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
      res.end(payload);
      return;
    }
    await serveStatic(req, res, url);
  } catch (err) {
    console.error("Request error:", err);
    res.writeHead(500, { "Content-Type": "application/json" }).end(JSON.stringify({ error: String(err) }));
  }
});

server.on("error", (err: NodeJS.ErrnoException) => {
  if (err.code === "EADDRINUSE") {
    console.error(`Port ${PORT} is already in use.`);
    process.exit(1);
    return;
  }
  console.error(String(err));
  process.exit(1);
});

server.listen(PORT, () => {
  console.log(`Backgammon server running at http://localhost:${PORT}/`);
});
