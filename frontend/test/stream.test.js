import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readChatStream } from '../src/stream.js'

function bodyFromChunks(chunks) {
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk)
      controller.close()
    },
  })
}

test('reads fragmented UTF-8, CRLF boundaries and multiline streaming events in order', async () => {
  const bytes = new TextEncoder().encode(
    ': heartbeat\r\n\r\nevent: started\r\ndata: {"run_id":"run-1","session_id":"chat-1"}\r\n\r\n' +
    'id: run-1\r\nevent: route\r\ndata: {\r\ndata: "route":"政策"\r\ndata: }\r\n\r\n' +
    'event: content\ndata: {"text":"您好"}\n\n' +
    'event: done\ndata: {"run_id":"run-1","status":"completed"}\n\n',
  )
  const seen = []
  const run = await readChatStream(
    bodyFromChunks(Array.from(bytes, (byte) => Uint8Array.of(byte))),
    (event, data) => seen.push([event, data]),
  )
  assert.deepEqual(seen, [
    ['started', { run_id: 'run-1', session_id: 'chat-1' }],
    ['route', { route: '政策' }],
    ['content', { text: '您好' }],
    ['done', { run_id: 'run-1', status: 'completed' }],
  ])
  assert.equal(run.run_id, 'run-1')
})

test('reports an early connection close so it can be recovered', async () => {
  const text = 'event: content\ndata: {"text":"部分答复"}\n\n'
  await assert.rejects(
    readChatStream(bodyFromChunks([new TextEncoder().encode(text)]), () => {}),
    { code: 'stream_closed' },
  )
})

test('keeps the interrupted execution ID when the server reports an error', async () => {
  const text = 'event: error\ndata: {"message":"写入后中断","run_id":"run-2"}\n\n'
  await assert.rejects(
    readChatStream(bodyFromChunks([new TextEncoder().encode(text)]), () => {}),
    { message: '写入后中断', runId: 'run-2' },
  )
})
