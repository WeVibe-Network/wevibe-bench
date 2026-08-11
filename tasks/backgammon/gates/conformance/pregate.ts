import { chromium } from "@playwright/test";
import {
  type ServerHandle,
  BASE_URL,
  api,
  debugRoll,
  debugSetState,
  health,
  makeState,
  startServer,
  stopServer,
} from "../lib/harness.ts";

export interface Problem {
  check: string;
  expected: string;
  observed: string;
}

export const REQUIRED_STATIC_TESTIDS: string[] = [
  "scoreWhite",
  "scoreBlack",
  "difficulty",
  "newGameBtn",
  "board",
  "playfield",
  "checkerLayer",
  "pointHints",
  "turnIndicator",
  "pipWhite",
  "pipBlack",
  "cube",
  "cubeVal",
  "cubeOwner",
  "dice",
  "rollBtn",
  "doubleBtn",
  "undoBtn",
  "endTurnBtn",
  "message",
  "modalOverlay",
  "modalTitle",
  "modalBody",
  "modalBtns",
];

export const REQUIRED_STATE_KEYS: string[] = [
  "points",
  "bar",
  "off",
  "turn",
  "phase",
  "dice",
  "remainingDice",
  "cube",
  "difficulty",
  "score",
  "winner",
  "winType",
  "pointsWon",
  "doubleOfferedBy",
  "message",
  "turnOver",
  "gamesPlayed",
  "pip",
  "legalMoves",
  "canDouble",
];

function firstNonEmptyLine(text: string): string {
  const lines = text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  return lines[0] ?? "<empty>";
}

function asObserved(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  try {
    const json = JSON.stringify(value);
    return json === undefined ? String(value) : json;
  } catch {
    return String(value);
  }
}

function errorLine(error: unknown): string {
  if (error instanceof Error) {
    return firstNonEmptyLine(error.message || String(error));
  }
  return firstNonEmptyLine(String(error));
}

function bootObserved(error: unknown): string {
  const raw = error instanceof Error ? error.message : String(error);
  const lines = raw
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  const stderrLine = lines.find((line) => line.toLowerCase().startsWith("stderr:"));
  if (stderrLine) {
    const onSameLine = stderrLine.slice("stderr:".length).trim();
    if (onSameLine.length > 0) {
      return onSameLine;
    }
    const idx = lines.indexOf(stderrLine);
    if (idx >= 0 && lines[idx + 1]) {
      return lines[idx + 1];
    }
  }
  return lines[0] ?? "<unknown boot failure>";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function sortedDice(value: unknown): number[] | null {
  if (!Array.isArray(value) || value.some((die) => typeof die !== "number")) {
    return null;
  }
  return [...value].sort((a, b) => a - b);
}

export async function runPreGate(): Promise<Problem[]> {
  const problems: Problem[] = [];
  const add = (check: string, expected: string, observed: string) => {
    problems.push({ check, expected, observed });
  };

  let server: ServerHandle | null = null;
  let browser: Awaited<ReturnType<typeof chromium.launch>> | null = null;

  try {
    try {
      server = await startServer({ debug: true });
    } catch (error) {
      add(
        "REQ-BIND/boot — server boots and listens on :8002 with /health ok",
        "server listening on :8002 with /health ok",
        bootObserved(error),
      );
      return problems;
    }

    try {
      const response = await health();
      if (response.status !== 200) {
        add("REQ-API/health.status — GET /health returns 200", "200", String(response.status));
      }

      let body: unknown;
      try {
        body = await response.json();
      } catch (error) {
        add("REQ-API/health.body — /health responds with JSON {\"status\":\"ok\",...}", '{"status":"ok",...}', errorLine(error));
        body = undefined;
      }

      if (!isRecord(body) || body.status !== "ok") {
        add("REQ-API/health.body.status — /health body carries \"status\":\"ok\"", '{"status":"ok",...}', asObserved(body));
      }
    } catch (error) {
      add("REQ-BIND/health — GET /health succeeds", "GET /health succeeds", errorLine(error));
    }

    try {
      const echoed = await debugSetState(
        makeState({
          off: { white: 7, black: 0 },
          turn: "white",
          phase: "roll",
        }),
      );

      for (const key of REQUIRED_STATE_KEYS) {
        if (!isRecord(echoed) || !Object.prototype.hasOwnProperty.call(echoed, key)) {
          add(`REQ-STATE/state.${key} — /api/state response carries the "${key}" field`, "present", "missing");
        }
      }

      const offWhite = (echoed as any)?.off?.white;
      if (offWhite !== 7) {
        add("REQ-STATE/state.off.white — seeded off counts survive the state echo", "7", String(offWhite));
      }

      const pip = (echoed as any)?.pip;
      const pipOk =
        isRecord(pip) && typeof pip.white === "number" && typeof pip.black === "number";
      if (!pipOk) {
        add(
          "REQ-STATE/state.pip — state carries pip as an object with numeric white and black",
          "object with numeric white and black",
          asObserved(pip),
        );
      }

      if (!Array.isArray((echoed as any)?.legalMoves)) {
        add("REQ-STATE/state.legalMoves — state carries legalMoves as an array", "array", asObserved((echoed as any)?.legalMoves));
      }

      if (typeof (echoed as any)?.canDouble !== "boolean") {
        add("REQ-STATE/state.canDouble — state carries canDouble as a boolean", "boolean", asObserved((echoed as any)?.canDouble));
      }
    } catch (error) {
      add("REQ-DEBUG/debug.setState — debug.setState seeds a board and /api/state echoes it", "debug state can be set and echoed", errorLine(error));
    }

    try {
      await debugRoll([6, 1]);
      const rolled = await api("/api/roll");
      const dice = sortedDice((rolled as any)?.dice);
      const honored = dice !== null && dice.length === 2 && dice[0] === 1 && dice[1] === 6;
      if (!honored) {
        add("REQ-DEBUG/debug.roll — the debug roll queue is honored by /api/roll", "dice [1,6] after /api/roll", asObserved((rolled as any)?.dice));
      }
    } catch (error) {
      add("REQ-DEBUG/debug.roll — the debug roll queue is honored by /api/roll", "debug roll queue is honored", errorLine(error));
    }

    try {
      browser = await chromium.launch();
      const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });

      await page.goto(BASE_URL, { waitUntil: "domcontentloaded" });
      await page.waitForSelector('[data-testid="board"]', { timeout: 5_000 });

      for (const testId of REQUIRED_STATIC_TESTIDS) {
        const count = await page
          .locator(`[data-testid="${testId}"]`)
          .count();
        if (count < 1) {
          add(`REQ-TESTID/testid.${testId} — page exposes data-testid "${testId}"`, "present", "missing");
        }
      }

      await api("/api/new", {});
      await debugRoll([3, 1]);
      await api("/api/roll");
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.waitForSelector('[data-testid="board"]', { timeout: 5_000 });

      await page
        .waitForFunction(
          () => document.querySelectorAll('[data-testid="checker"]').length === 30,
          undefined,
          { timeout: 2_000 },
        )
        .catch(() => undefined);
      await page
        .waitForFunction(
          () => document.querySelectorAll('[data-testid="die"]').length >= 2,
          undefined,
          { timeout: 2_000 },
        )
        .catch(() => undefined);

      const pointCount = await page.locator('[data-testid="point"]').count();
      if (pointCount !== 24) {
        add("REQ-TESTID/point — board renders 24 data-testid \"point\" elements", "24", String(pointCount));
      }

      const checkerCount = await page.locator('[data-testid="checker"]').count();
      if (checkerCount !== 30) {
        add("REQ-TESTID/checker — board renders 30 data-testid \"checker\" elements (15 per side)", "30", String(checkerCount));
      }

      const barCount = await page.locator('[data-testid="bar"]').count();
      if (barCount < 1) {
        add("REQ-TESTID/bar — a data-testid \"bar\" element is present", ">=1", String(barCount));
      }

      const offTrayCount = await page.locator('[data-testid="off-tray"]').count();
      if (offTrayCount < 1) {
        add("REQ-TESTID/off-tray — a data-testid \"off-tray\" element is present", ">=1", String(offTrayCount));
      }

      const dieCount = await page.locator('[data-testid="die"]').count();
      if (dieCount < 2) {
        add("REQ-TESTID/die — at least two data-testid \"die\" elements are present", ">=2", String(dieCount));
      }

      let hintCount = await page.locator('[data-testid="hint"]').count();
      if (hintCount < 1) {
        const selectableWhites = page.locator(
          '[data-testid="checker"][data-color="white"].selectable',
        );
        const selectableCount = await selectableWhites.count();

        if (selectableCount > 0) {
          // Fire the DOM click handler directly (dispatchEvent) so it works even
          // if the checker is scrolled outside this browser's default viewport.
          await selectableWhites.first().dispatchEvent("click");
          await page.waitForTimeout(200);
        } else {
          const whiteCheckers = page.locator(
            '[data-testid="checker"][data-color="white"]',
          );
          const whiteCount = await whiteCheckers.count();
          for (let i = 0; i < whiteCount; i++) {
            try {
              await whiteCheckers.nth(i).dispatchEvent("click");
            } catch {
              // Keep probing other white checkers.
            }
            await page
              .waitForFunction(
                () => document.querySelectorAll('[data-testid="hint"]').length > 0,
                undefined,
                { timeout: 250 },
              )
              .catch(() => undefined);
            hintCount = await page.locator('[data-testid="hint"]').count();
            if (hintCount > 0) {
              break;
            }
          }
        }

        hintCount = await page.locator('[data-testid="hint"]').count();
      }

      if (hintCount < 1) {
        add(
          "REQ-HINT/hint — selecting a movable checker shows one hint per playable die",
          "hints appear after selecting a movable checker",
          "none",
        );
      }

      await page.close();
    } catch (error) {
      add("REQ-TESTID/dom — page DOM exposes the required testids and hint flow", "DOM testids and hint flow are present", errorLine(error));
    }
  } catch (error) {
    add("REQ-TESTID/pregate — conformance pre-gate completes without errors", "pre-gate completes", errorLine(error));
  } finally {
    if (browser) {
      try {
        await browser.close();
      } catch {
        // best effort
      }
    }
    if (server) {
      try {
        await stopServer(server);
      } catch {
        // best effort
      }
    }
  }

  return problems;
}
