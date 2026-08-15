import { appendFileSync, existsSync, mkdirSync, statSync } from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
export const TRACE_HEADER = 'x-wevibe-trace-id';
let memoizedLogDir;
function safeExists(filePath) {
    try {
        return existsSync(filePath);
    }
    catch {
        return false;
    }
}
function safeIsDirectory(filePath) {
    try {
        return statSync(filePath).isDirectory();
    }
    catch {
        return false;
    }
}
function safeMkdir(dirPath) {
    try {
        mkdirSync(dirPath, { recursive: true });
    }
    catch {
        // best-effort logging only
    }
}
function safeAppend(filePath, content) {
    try {
        appendFileSync(filePath, content, 'utf8');
    }
    catch {
        // best-effort logging only
    }
}
function safeStderrWrite(content) {
    try {
        process.stderr.write(content);
    }
    catch {
        // best-effort logging only
    }
}
function findWorkspaceLogDir(startDir) {
    let current;
    try {
        current = path.resolve(startDir);
    }
    catch {
        return undefined;
    }
    while (true) {
        const metaDir = path.join(current, 'wevibe-meta');
        if (safeExists(metaDir) && safeIsDirectory(metaDir)) {
            return path.join(metaDir, '.logs');
        }
        const parent = path.dirname(current);
        if (parent === current) {
            return undefined;
        }
        current = parent;
    }
}
function resolveLogDir() {
    if (memoizedLogDir) {
        return memoizedLogDir;
    }
    const envLogDir = process.env.WEVIBE_LOG_DIR;
    if (typeof envLogDir === 'string' && envLogDir.trim() !== '') {
        memoizedLogDir = envLogDir;
        return memoizedLogDir;
    }
    // Test-mode: NEVER write to the shared production .logs (it pollutes the
    // /diagnostics error surface). Redirect to a throwaway temp dir. stderr
    // writes (logOp) are unconditional and still fire, so stderr-capturing
    // tests keep working. Production (NODE_ENV!=='test') is unchanged.
    if (process.env.NODE_ENV === 'test' || process.env.VITEST) {
        memoizedLogDir = path.join(os.tmpdir(), 'wevibe-test-logs');
        return memoizedLogDir;
    }
    const moduleDir = path.dirname(fileURLToPath(import.meta.url));
    const candidates = [process.cwd(), moduleDir];
    for (const startDir of candidates) {
        const discovered = findWorkspaceLogDir(startDir);
        if (discovered) {
            memoizedLogDir = discovered;
            return memoizedLogDir;
        }
    }
    try {
        memoizedLogDir = path.join(os.homedir(), '.wevibe', 'logs');
    }
    catch {
        memoizedLogDir = path.join('.wevibe', 'logs');
    }
    return memoizedLogDir;
}
function serializeValue(value) {
    if (value === null || value === undefined) {
        return '-';
    }
    const valueType = typeof value;
    if (valueType === 'string' || valueType === 'number' || valueType === 'boolean') {
        return String(value);
    }
    if (valueType === 'bigint') {
        return String(value);
    }
    try {
        const json = JSON.stringify(value);
        return json ?? '-';
    }
    catch {
        return '-';
    }
}
function sanitizeAndQuote(value) {
    const sanitized = value.replace(/[\r\n]/g, ' ');
    if (sanitized.includes(' ') || sanitized.includes('=')) {
        const escaped = sanitized.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
        return `"${escaped}"`;
    }
    return sanitized;
}
function formatValue(value) {
    return sanitizeAndQuote(serializeValue(value));
}
function utcDayStamp() {
    return new Date().toISOString().slice(0, 10).replace(/-/g, '');
}
export function logOp(op, level, fields) {
    try {
        const timestamp = new Date().toISOString();
        const levelText = level.toUpperCase();
        const parts = [
            `op=${formatValue(op)}`,
            `trace=${formatValue(fields.trace ?? '-')}`,
        ];
        for (const [key, value] of Object.entries(fields)) {
            if (key === 'trace') {
                continue;
            }
            parts.push(`${key}=${formatValue(value)}`);
        }
        const line = `${timestamp} ${levelText} ${parts.join(' ')}`;
        const lineWithNewline = `${line}\n`;
        safeStderrWrite(lineWithNewline);
        const logDir = resolveLogDir();
        const opsDir = path.join(logDir, 'ops');
        safeMkdir(logDir);
        safeMkdir(opsDir);
        const filePath = path.join(opsDir, `${op}-${utcDayStamp()}.log`);
        safeAppend(filePath, lineWithNewline);
    }
    catch {
        // best-effort logging only
    }
}
function hashFirst8(bytes) {
    return createHash('sha256').update(bytes).digest('hex').slice(0, 8);
}
function isHexLike(value) {
    return value.length >= 2 && value.length % 2 === 0 && /^[0-9a-fA-F]+$/.test(value);
}
export function fp(input) {
    try {
        if (input === null || input === undefined) {
            return '-';
        }
        if (typeof input === 'string') {
            if (input.length === 0) {
                return '-';
            }
            const bytes = isHexLike(input) ? Buffer.from(input, 'hex') : Buffer.from(input, 'utf8');
            if (bytes.length === 0) {
                return '-';
            }
            return hashFirst8(bytes);
        }
        if (input.length === 0) {
            return '-';
        }
        return hashFirst8(Buffer.isBuffer(input) ? input : Buffer.from(input));
    }
    catch {
        return '-';
    }
}
export function resolveTraceId(header) {
    try {
        const value = Array.isArray(header) ? header[0] : header;
        if (typeof value === 'string' && value.trim() !== '') {
            return value;
        }
    }
    catch {
        // best-effort parsing only
    }
    return newTraceId();
}
export function newTraceId() {
    try {
        return randomUUID();
    }
    catch {
        return `trace-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    }
}
export function appendRaw(fileName, text) {
    try {
        const logDir = resolveLogDir();
        safeMkdir(logDir);
        const filePath = path.join(logDir, fileName);
        safeMkdir(path.dirname(filePath));
        safeAppend(filePath, text);
    }
    catch {
        // best-effort logging only
    }
}
//# sourceMappingURL=logger.js.map