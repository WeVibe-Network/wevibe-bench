import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { logOp, fp } from './logger.js';
function is_object(value) {
    return typeof value === 'object' && value !== null;
}
export function servedMemoriesPath() {
    const envPath = process.env.WEVIBE_SERVED_MEMORIES_PATH;
    if (envPath)
        return envPath;
    return join(homedir(), '.wevibe', 'served-memories.json');
}
export function readUsedMemoryTexts(sessionId) {
    const t0 = Date.now();
    const sessionFp = fp(sessionId);
    if (!sessionId) {
        logOp('served_store.read', 'info', {
            session_fp: sessionFp,
            status: 'ok',
            reason: 'empty_session',
            matched: 0,
            dur_ms: Date.now() - t0,
        });
        return [];
    }
    const filePath = servedMemoriesPath();
    if (!existsSync(filePath)) {
        logOp('served_store.read', 'info', {
            session_fp: sessionFp,
            status: 'ok',
            reason: 'no_file',
            path: servedMemoriesPath(),
            exists: false,
            matched: 0,
            dur_ms: Date.now() - t0,
        });
        return [];
    }
    let parsed;
    try {
        parsed = JSON.parse(readFileSync(filePath, 'utf-8'));
    }
    catch {
        logOp('served_store.read', 'error', {
            session_fp: sessionFp,
            status: 'err',
            reason: 'parse_failed',
            path: servedMemoriesPath(),
            matched: 0,
            dur_ms: Date.now() - t0,
        });
        return [];
    }
    if (!is_object(parsed)) {
        return [];
    }
    if (parsed.version !== 1 || !is_object(parsed.memories)) {
        return [];
    }
    const matchingRecords = [];
    for (const [cid, value] of Object.entries(parsed.memories)) {
        if (!is_object(value)) {
            continue;
        }
        const recordCid = value.cid;
        const text = value.text;
        const sessionIds = value.session_ids;
        const lastUsedAt = value.last_used_at;
        if (typeof recordCid !== 'string' ||
            recordCid !== cid ||
            typeof text !== 'string' ||
            !Array.isArray(sessionIds) ||
            !sessionIds.every((id) => typeof id === 'string') ||
            typeof lastUsedAt !== 'number' ||
            !Number.isFinite(lastUsedAt)) {
            continue;
        }
        if (!sessionIds.includes(sessionId)) {
            continue;
        }
        matchingRecords.push({
            cid: recordCid,
            text,
            session_ids: sessionIds,
            last_used_at: lastUsedAt,
        });
    }
    matchingRecords.sort((a, b) => {
        if (b.last_used_at !== a.last_used_at) {
            return b.last_used_at - a.last_used_at;
        }
        return a.cid.localeCompare(b.cid);
    });
    const seenTexts = new Set();
    const result = [];
    for (const record of matchingRecords) {
        if (seenTexts.has(record.text)) {
            continue;
        }
        seenTexts.add(record.text);
        result.push(record.text);
    }
    logOp('served_store.read', 'info', {
        session_fp: sessionFp,
        status: 'ok',
        matched: result.length,
        dur_ms: Date.now() - t0,
    });
    return result;
}
//# sourceMappingURL=served-memory-store.js.map