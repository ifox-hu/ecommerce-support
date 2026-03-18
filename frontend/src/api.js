import { readChatStream } from './stream.js'

async function send(path, token, { body, method = 'GET', signal, accept = 'application/json' } = {}) {
  const headers = { Accept: accept }
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const response = await fetch(`/api${path}`, {
    method,
    signal,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  if (!response.ok) {
    const result = await response.json().catch(() => ({}))
    const detail = result.detail
    const message = result.message || (typeof detail === 'string' ? detail : detail?.message)
    const error = new Error(message || `服务请求失败（${response.status}）。`)
    error.status = response.status
    error.runId = result.run_id || detail?.run_id
    throw error
  }

  return response
}

async function json(path, token, options) {
  const response = await send(path, token, options)
  return response.json()
}

export const api = {
  health: () => json('/health'),
  sessions: (token) => json('/sessions', token),
  messages: (token, id, offset = 0) =>
    json(`/sessions/${encodeURIComponent(id)}/messages?offset=${offset}&limit=50`, token),
  async chat(token, body, signal, onEvent) {
    const response = await send('/chat/stream', token, {
      body,
      signal,
      method: 'POST',
      accept: 'text/event-stream',
    })
    return readChatStream(response.body, onEvent)
  },
  resume: (token, id) => json(`/runs/${encodeURIComponent(id)}/resume`, token, { method: 'POST' }),
  tickets: (token) => json('/tickets', token),
  claim: (token, id) => json(`/tickets/${encodeURIComponent(id)}/claim`, token, { method: 'POST' }),
  analytics: (token, question) => json('/analytics', token, { method: 'POST', body: { question } }),
  feedback: (token, body) => json('/feedback', token, { method: 'POST', body }),
}
