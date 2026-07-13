import { randomUUID } from 'node:crypto';
import { appendFile, mkdir } from 'node:fs/promises';
import path from 'node:path';

const LOG_ROOT = process.env.WEVIBE_BENCH_RUNS_DIR
  ? path.join(process.env.WEVIBE_BENCH_RUNS_DIR, 'leader-signer')
  : path.resolve(__dirname, '../../../../runs/leader-signer');

type LogLevel = 'PROGRESS' | 'INFO' | 'ERROR';

function stringifyMeta(meta?: Record<string, unknown>): string {
  if (!meta) {
    return '';
  }

  return ` ${JSON.stringify(meta)}`;
}

export function formatErrorForLog(error: unknown): Record<string, unknown> {
  if (error instanceof Error) {
    return {
      name: error.name,
      message: error.message,
      stack: error.stack ?? null,
      cause: error.cause ?? null,
    };
  }

  let serialized: string;
  try {
    serialized = typeof error === 'string' ? error : JSON.stringify(error);
  } catch {
    serialized = String(error);
  }

  return {
    message: serialized,
  };
}

export class CommandLogger {
  readonly traceId: string;
  readonly logFilePath: string;

  constructor(traceId: string, logFilePath: string) {
    this.traceId = traceId;
    this.logFilePath = logFilePath;
  }

  private async write(level: LogLevel, message: string, meta?: Record<string, unknown>): Promise<void> {
    const line = `${new Date().toISOString()} [${level}] trace=${this.traceId} ${message}${stringifyMeta(meta)}\n`;
    await appendFile(this.logFilePath, line, { encoding: 'utf8' });

    if (level === 'PROGRESS' || level === 'ERROR') {
      process.stderr.write(`[${level}] trace=${this.traceId} ${message}${stringifyMeta(meta)}\n`);
    }
  }

  async progress(message: string, meta?: Record<string, unknown>): Promise<void> {
    await this.write('PROGRESS', message, meta);
  }

  async info(message: string, meta?: Record<string, unknown>): Promise<void> {
    await this.write('INFO', message, meta);
  }

  async error(message: string, meta?: Record<string, unknown>): Promise<void> {
    await this.write('ERROR', message, meta);
  }
}

export async function createCommandLogger(commandName: string): Promise<CommandLogger> {
  await mkdir(LOG_ROOT, { recursive: true });
  const timestamp = new Date().toISOString();
  const logFilePath = path.join(LOG_ROOT, `${commandName}-${timestamp}.log`);
  const traceId = randomUUID();
  await appendFile(logFilePath, '');
  return new CommandLogger(traceId, logFilePath);
}
