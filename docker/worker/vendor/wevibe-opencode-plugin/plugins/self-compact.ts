import { type Plugin } from "@opencode-ai/plugin"
import { z } from "zod"
import { appendFileSync, mkdirSync } from "node:fs"
import { join } from "node:path"

const LOG_DIR = join(
  process.env.HOME ?? "/tmp",
  ".local/state/opencode/self-compact",
)

const ALLOWED_AGENTS = new Set(["build"])

function log(line: string, fields: Record<string, unknown> = {}): void {
  const ts = new Date().toISOString()
  try {
    mkdirSync(LOG_DIR, { recursive: true })
    appendFileSync(
      join(LOG_DIR, `${ts.slice(0, 10)}-self-compact.log`),
      JSON.stringify({ ts, msg: line, ...fields }) + "\n",
    )
  } catch {
    // Compaction must never fail because logging failed.
  }
}

/**
 * Bench-worker self-compaction (arm-on-idle pattern), adapted from the
 * workspace host plugin for the wevibe-bench chunked first pass.
 *
 * WHY THIS SHAPE
 * - The bench drives one serve session through N chunk prompts. Between
 *   chunks the context window only grows; compacting at each chunk boundary
 *   keeps every chunk's window small (fewer stalls, no overflow compaction
 *   mid-chunk).
 * - The chunk prompt tells the worker to print CHUNK FINISHED and then call
 *   this tool as the last action of its turn. The tool only ARMS a one-shot
 *   trigger; when the turn ends (session.idle) the hook fires
 *   session.summarize. The harness watches for the compaction part and sends
 *   the next chunk — autocontinue is therefore ALWAYS suppressed (no manager
 *   exception; a synthetic continue turn would burn worker tokens on nothing).
 * - The harness runs a fail-open backstop (its own summarize call) when the
 *   worker never armed, so a refused or forgotten arm never wedges the loop.
 * - No context-size floor: compaction here is harness-paced per chunk
 *   boundary, not model-self-assessed. A short cooldown guards against a
 *   degenerate re-arm loop.
 */
const SelfCompactPlugin: Plugin = async ({ client, directory }) => {
  /** sessionID -> arm metadata; one-shot, disarmed before firing. */
  const armed = new Map<string, { agent: string; reason: string }>()
  /** Sessions whose in-flight compaction was fired by THIS plugin (not overflow). TTL'd. */
  const firedSelf = new Map<string, number>()
  /** sessionID -> last successful self-compaction fire time (cooldown guard). */
  const lastFired = new Map<string, number>()
  /** agent name -> "provider/model" captured from merged config (fallback resolution). */
  const agentModels = new Map<string, string>()
  /** "provider/model" main-model fallback captured from merged config. */
  let mainModel: string | null = null

  const FIRED_TTL_MS = 10 * 60 * 1000 // stale suppression entry must NEVER eat a later overflow autocontinue
  const COOLDOWN_MS = 60 * 1000 // compact-loop guard: refuse re-arming too soon

  function firedSelfHas(sessionID: string): boolean {
    const t = firedSelf.get(sessionID)
    if (t === undefined) return false
    if (Date.now() - t > FIRED_TTL_MS) {
      firedSelf.delete(sessionID)
      log("firedSelf-expired", { sessionID })
      return false
    }
    return true
  }

  /**
   * Resolve the model the SESSION is actually running on: the model of the
   * last REAL user message (skipping synthetic compaction parents). Whatever
   * model we pass to summarize gets stamped on the compaction parent message,
   * so it must be the worker's own model.
   */
  async function resolveSessionModel(
    sessionID: string,
    agent: string,
  ): Promise<{ providerID: string; modelID: string } | null> {
    try {
      const res = await client.session.messages({
        path: { id: sessionID },
        query: { directory },
      })
      const messages: any[] = (res as any)?.data ?? []
      for (let i = messages.length - 1; i >= 0; i--) {
        const m = messages[i]
        const info = m?.info
        if (info?.role !== "user") continue
        if ((m?.parts ?? []).some((p: any) => p?.type === "compaction")) continue
        if (info?.model?.providerID && info?.model?.modelID) {
          return { providerID: info.model.providerID, modelID: info.model.modelID }
        }
      }
    } catch (err) {
      log("resolve-session-model-failed", { sessionID, error: String(err) })
    }
    const fallback = agentModels.get(agent) ?? mainModel
    if (fallback && fallback.includes("/")) {
      // Config strings are "provider/model" where the model ID may itself
      // contain slashes — split on the FIRST slash only.
      const slash = fallback.indexOf("/")
      return { providerID: fallback.slice(0, slash), modelID: fallback.slice(slash + 1) }
    }
    return null
  }

  async function fireSummarize(sessionID: string): Promise<void> {
    const meta = armed.get(sessionID)
    if (!meta) return
    armed.delete(sessionID) // disarm FIRST: summarize itself causes busy->idle again

    // providerID/modelID are REQUIRED by the summarize payload schema (a
    // body-less call 400s every time), and auto:true is REQUIRED for the
    // engine to run its compaction path at all.
    const model = await resolveSessionModel(sessionID, meta.agent)
    if (!model) {
      log("summarize-skipped-no-model", { sessionID, ...meta })
      return
    }

    firedSelf.set(sessionID, Date.now())
    log("firing-summarize", { sessionID, ...meta, ...model })

    const result = await client.session.summarize({
      path: { id: sessionID },
      query: { directory },
      // The vendored SDK 1.4.10 types predate the `auto` field; the 1.18.x
      // server accepts it (the autocontinue hook this plugin relies on is
      // part of the same auto-gated path — verified present in the pinned
      // opencode 1.18.1 binary).
      body: { providerID: model.providerID, modelID: model.modelID, auto: true } as any,
    })

    if (result.error) {
      firedSelf.delete(sessionID) // nothing in flight after all
    } else {
      lastFired.set(sessionID, Date.now()) // start the cooldown window
    }

    log("summarize-result", {
      sessionID,
      ok: !result.error,
      error: result.error ? JSON.stringify(result.error) : null,
    })
  }

  return {
    config: async (cfg: any) => {
      mainModel = cfg?.model ?? null
      for (const [name, def] of Object.entries<any>(cfg?.agent ?? {})) {
        if (def?.model) agentModels.set(name, def.model)
      }
    },

    event: async (input: any) => {
      const e = input?.event ?? input
      const type = e?.type
      const sessionID = e?.properties?.sessionID
      if (type === "session.idle" && sessionID && armed.has(sessionID)) {
        await fireSummarize(sessionID)
      } else if (type === "session.compacted" && sessionID) {
        log("session-compacted", { sessionID, selfFired: firedSelfHas(sessionID) })
      }
    },

    "experimental.compaction.autocontinue": async (input: any, output: { enabled: boolean }) => {
      // Only touch compactions THIS plugin fired; overflow auto-compaction
      // keeps its default autocontinue (it is what keeps a mid-chunk turn
      // alive). For self-fired compactions the autocontinue is ALWAYS
      // suppressed: the bench harness sends the next chunk prompt itself.
      if (!firedSelfHas(input.sessionID)) return
      firedSelf.delete(input.sessionID)
      output.enabled = false
      log("autocontinue-suppressed", { sessionID: input.sessionID, agent: input.agent })
    },

    tool: {
      // Structural tool definition (type-only SDK import, zod args) — the
      // same discipline as wevibe-plugin.ts so the file also loads under the
      // tsx test runner, not just the in-image opencode plugin loader.
      self_compact: {
        description:
          "Bench worker: ARM compaction for this session. Fires OpenCode's real compaction (session.summarize) the moment your turn ends — no autocontinue, the harness sends the next chunk after compaction completes. Call once, right after printing CHUNK FINISHED, as the last action of your turn.",
        args: {
          reason: z
            .string()
            .optional()
            .describe("Why compaction is safe now (chunk work complete, marker printed)."),
        },
        async execute(args: { reason?: string }, context: any) {
          if (!ALLOWED_AGENTS.has(context.agent)) {
            return {
              output: `DENIED: self_compact is for the bench worker (build agent) only, not '${context.agent}'.`,
              metadata: { allowed: false, agent: context.agent },
            }
          }

          // Compact-loop guard: a session that just compacted must do real
          // work before compacting again.
          const last = lastFired.get(context.sessionID)
          if (last !== undefined && Date.now() - last < COOLDOWN_MS) {
            const waitS = Math.ceil((COOLDOWN_MS - (Date.now() - last)) / 1000)
            log("arm-refused-cooldown", { sessionID: context.sessionID, agent: context.agent, waitS })
            return {
              output: `REFUSED (cooldown): this session compacted ${Math.round((Date.now() - last) / 1000)}s ago. The next chunk arrives after compaction; retry in ~${waitS}s only if you truly need it.`,
              metadata: { ok: false, cooldown: true, waitS },
            }
          }

          armed.set(context.sessionID, {
            agent: context.agent,
            reason: args.reason ?? "chunk boundary",
          })

          context.metadata({
            title: "Compaction armed (fires on idle)",
            metadata: { agent: context.agent, reason: args.reason ?? "chunk boundary" },
          })

          log("armed", {
            sessionID: context.sessionID,
            messageID: context.messageID,
            agent: context.agent,
            directory,
            reason: args.reason ?? "chunk boundary",
          })

          return {
            output:
              "ARMED: compaction fires when this turn ends. End your turn now — the harness sends the next chunk after compaction completes.",
            metadata: { ok: true, armed: true, agent: context.agent },
          }
        },
      } as any,
    },
  }
}

export default SelfCompactPlugin
