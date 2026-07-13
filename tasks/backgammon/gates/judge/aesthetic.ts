/**
 * Polish-axis aesthetic judge hook for the backgammon benchmark.
 *
 * This path is intentionally separate from the deterministic 100%-oracle gate: in phase 2a
 * it is fully offline-stubbed so benchmark runs do not depend on external LLM availability,
 * and phase 2b will only swap in a live model call inside `callJudgeModel`.
 */

import { readFile } from "node:fs/promises";
import { extname } from "node:path";

export const JUDGE_MODEL = "anthropic/claude-opus-4.8";
export const JUDGE_TEMPERATURE = 0;

export const DEFAULT_AESTHETIC_CRITERIA: string[] = [
  "Board legibility (points, bar, and home zones are visually parseable at a glance)",
  "Checker/point contrast (checker colors remain distinct from board point colors)",
  "Layout balance with no clipping, overlap, or overflow in viewport",
  "Dice and doubling-cube clarity (faces/values are obvious and unambiguous)",
  "Turn/message readability (status text is easy to read and not visually crowded)",
  "Overall visual polish compared with the provided golden anchor screenshot",
];

export interface AestheticVerdictItem {
  criterion: string;
  pass: boolean;
  severity: "none" | "minor" | "major" | "blocker";
  observation: string;
}

export type AestheticVerdict = AestheticVerdictItem[];

export interface JudgeInput {
  screenshotPath: string;
  goldenScreenshotPath: string;
  criteria?: string[];
}

export const AESTHETIC_VERDICT_SCHEMA = {
  type: "array",
  items: {
    type: "object",
    additionalProperties: false,
    properties: {
      criterion: { type: "string" },
      pass: { type: "boolean" },
      severity: {
        type: "string",
        enum: ["none", "minor", "major", "blocker"],
      },
      observation: { type: "string" },
    },
    required: ["criterion", "pass", "severity", "observation"],
  },
} as const;

type JudgeMessagePart =
  | { type: "text"; text: string }
  | {
      type: "image_base64";
      name: "candidate_screenshot" | "golden_anchor_screenshot";
      mediaType: string;
      data: string;
    };

interface AestheticJudgeRequest {
  model: typeof JUDGE_MODEL;
  temperature: typeof JUDGE_TEMPERATURE;
  schema: typeof AESTHETIC_VERDICT_SCHEMA;
  criteria: string[];
  messages: Array<{
    role: "system" | "user";
    content: JudgeMessagePart[];
  }>;
}

function inferImageMediaType(filePath: string): string {
  switch (extname(filePath).toLowerCase()) {
    case ".jpg":
    case ".jpeg":
      return "image/jpeg";
    case ".webp":
      return "image/webp";
    case ".gif":
      return "image/gif";
    default:
      return "image/png";
  }
}

async function readImageAsBase64(filePath: string): Promise<string> {
  const bytes = await readFile(filePath);
  return bytes.toString("base64");
}

async function callJudgeModel(req: AestheticJudgeRequest): Promise<AestheticVerdict> {
  // PHASE 2b: wire to the pinned Opus-4.8 endpoint here.
  void req;
  throw new Error("live aesthetic judge not wired until phase 2b");
}

export async function judgeAesthetics(input: JudgeInput): Promise<AestheticVerdict> {
  const criteria =
    input.criteria && input.criteria.length > 0
      ? input.criteria
      : DEFAULT_AESTHETIC_CRITERIA;

  if (process.env.BENCH_JUDGE_LIVE !== "1") {
    return criteria.map((criterion) => ({
      criterion,
      pass: true,
      severity: "none",
      observation: "stub — LLM judge not invoked in offline mode",
    }));
  }

  const candidateScreenshotBase64 = await readImageAsBase64(input.screenshotPath);
  const goldenScreenshotBase64 = await readImageAsBase64(input.goldenScreenshotPath);
  const criteriaBulletList = criteria.map((criterion, i) => `${i + 1}. ${criterion}`).join("\n");

  const req: AestheticJudgeRequest = {
    model: JUDGE_MODEL,
    temperature: JUDGE_TEMPERATURE,
    schema: AESTHETIC_VERDICT_SCHEMA,
    criteria,
    messages: [
      {
        role: "system",
        content: [
          {
            type: "text",
            text:
              "You are an aesthetic judge for a backgammon benchmark. Compare the candidate screenshot against the golden anchor and output only schema-valid JSON.",
          },
        ],
      },
      {
        role: "user",
        content: [
          {
            type: "text",
            text:
              "Judge visual quality relative to the golden anchor. Evaluate all criteria and return one verdict item per criterion.\n\nCriteria:\n" +
              criteriaBulletList,
          },
          {
            type: "image_base64",
            name: "candidate_screenshot",
            mediaType: inferImageMediaType(input.screenshotPath),
            data: candidateScreenshotBase64,
          },
          {
            type: "image_base64",
            name: "golden_anchor_screenshot",
            mediaType: inferImageMediaType(input.goldenScreenshotPath),
            data: goldenScreenshotBase64,
          },
        ],
      },
    ],
  };

  return callJudgeModel(req);
}
