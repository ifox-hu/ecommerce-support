export async function readChatStream(body, onEvent) {
  const reader = body?.getReader()
  if (!reader) throw new Error('当前连接无法接收流式回复。')
  const decoder = new TextDecoder()
  let buffer = ''
  let event = 'message'
  let data = []
  let done

  function consume(line) {
    if (!line) {
      const type = event
      const parts = data
      event = 'message'
      data = []
      if (!parts.length || !['started', 'route', 'tool', 'sources', 'content', 'ticket', 'done', 'error'].includes(type)) return
      const payload = JSON.parse(parts.join('\n'))
      onEvent(type, payload)
      if (type === 'done') done = payload
      if (type === 'error') {
        const error = new Error(payload.message || '服务暂时无法完成请求。')
        error.runId = payload.run_id
        throw error
      }
      return
    }
    const separator = line.indexOf(':')
    const field = separator < 0 ? line : line.slice(0, separator)
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '')
    if (field === 'event') event = value
    if (field === 'data') data.push(value)
  }

  function drainLines(endOfStream) {
    while (!done) {
      const index = buffer.search(/[\r\n]/)
      if (index < 0) break
      if (!endOfStream && index === buffer.length - 1 && buffer[index] === '\r') break
      const line = buffer.slice(0, index)
      const ending = buffer.slice(index, index + 2) === '\r\n' ? 2 : 1
      buffer = buffer.slice(index + ending)
      consume(line)
    }
  }

  try {
    while (!done) {
      const chunk = await reader.read()
      buffer += decoder.decode(chunk.value, { stream: !chunk.done })
      drainLines(chunk.done)
      if (chunk.done) break
    }
    if (done) return done
    const error = new Error('连接中断，回复可能已保存在会话中。')
    error.code = 'stream_closed'
    throw error
  } finally {
    void reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
