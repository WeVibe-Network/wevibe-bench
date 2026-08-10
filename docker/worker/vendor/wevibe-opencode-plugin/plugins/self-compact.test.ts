import assert from 'node:assert/strict';
import test from 'node:test';

// @ts-expect-error tsx test runner resolves .ts extension imports.
import SelfCompactPlugin from './self-compact.ts';

type SummarizeCall = {
  path: { id: string }
  body: { providerID: string; modelID: string; auto: boolean }
}

function makeClient(opts: { summarizeError?: unknown } = {}) {
  const summarizeCalls: SummarizeCall[] = []
  const client = {
    session: {
      messages: async () => ({
        data: [
          {
            info: {
              role: 'user',
              model: { providerID: 'local-llm-proxy', modelID: 'kimi/kimi-k3' },
            },
            parts: [{ type: 'text', text: 'chunk prompt' }],
          },
        ],
      }),
      summarize: async (req: SummarizeCall) => {
        summarizeCalls.push(req)
        return { error: opts.summarizeError }
      },
    },
  }
  return { client, summarizeCalls }
}

async function makeHooks(client: unknown) {
  // The plugin factory receives { client, directory } and returns the hooks.
  return await (SelfCompactPlugin as any)({ client, directory: '/tmp' })
}

function toolContext(agent: string) {
  return {
    agent,
    sessionID: 'ses_test',
    messageID: 'msg_test',
    metadata: () => {},
  }
}

test('self_compact denies every agent except build', async () => {
  const { client } = makeClient()
  const hooks = await makeHooks(client)
  const out = await hooks.tool.self_compact.execute({ reason: 'x' }, toolContext('manager'))
  assert.match(out.output, /^DENIED/)
})

test('arm -> session.idle fires summarize with the session model and auto:true', async () => {
  const { client, summarizeCalls } = makeClient()
  const hooks = await makeHooks(client)

  const out = await hooks.tool.self_compact.execute({ reason: 'chunk boundary' }, toolContext('build'))
  assert.match(out.output, /^ARMED/)
  assert.equal(summarizeCalls.length, 0) // arm-on-idle: nothing fires mid-turn

  await hooks.event({ type: 'session.idle', properties: { sessionID: 'ses_test' } })
  assert.equal(summarizeCalls.length, 1)
  assert.deepEqual(summarizeCalls[0].body, {
    providerID: 'local-llm-proxy',
    modelID: 'kimi/kimi-k3',
    auto: true,
  })

  // The autocontinue hook must suppress the synthetic continue turn for the
  // compaction this plugin fired (the harness sends the next chunk itself).
  const cont = { enabled: true }
  await hooks['experimental.compaction.autocontinue'](
    { sessionID: 'ses_test', agent: 'build' },
    cont,
  )
  assert.equal(cont.enabled, false)
})

test('autocontinue hook ignores compactions it did not fire (overflow stays on)', async () => {
  const { client } = makeClient()
  const hooks = await makeHooks(client)
  const cont = { enabled: true }
  await hooks['experimental.compaction.autocontinue'](
    { sessionID: 'ses_never_armed', agent: 'build' },
    cont,
  )
  assert.equal(cont.enabled, true)
})

test('a second arm inside the cooldown window is refused', async () => {
  const { client } = makeClient()
  const hooks = await makeHooks(client)
  await hooks.tool.self_compact.execute({}, toolContext('build'))
  await hooks.event({ type: 'session.idle', properties: { sessionID: 'ses_test' } })
  const out = await hooks.tool.self_compact.execute({}, toolContext('build'))
  assert.match(out.output, /^REFUSED \(cooldown\)/)
})

test('summarize failure still resolves (fail-open) and clears the fired marker', async () => {
  const { client, summarizeCalls } = makeClient({ summarizeError: { message: 'boom' } })
  const hooks = await makeHooks(client)
  await hooks.tool.self_compact.execute({}, toolContext('build'))
  await hooks.event({ type: 'session.idle', properties: { sessionID: 'ses_test' } })
  assert.equal(summarizeCalls.length, 1)
  // A failed fire clears firedSelf: a later overflow compaction's autocontinue
  // must NOT be suppressed by this plugin's stale marker.
  const cont = { enabled: true }
  await hooks['experimental.compaction.autocontinue'](
    { sessionID: 'ses_test', agent: 'build' },
    cont,
  )
  assert.equal(cont.enabled, true)
})
