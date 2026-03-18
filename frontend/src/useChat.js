import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { api } from './api'
import { savedState, storeState } from './display'

function newId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export function useChat(token, storageKey) {
  const identity = `support-demo:${storageKey}`
  const sessionId = ref((storageKey && savedState(`${identity}:session`, '')) || newId())
  const recovery = ref(storageKey ? savedState(`${identity}:recovery`, {}) : {})
  const sessions = ref([])
  const messages = ref([])
  const nextOffset = ref(null)
  const draft = ref('')
  const tickets = ref([])
  const route = ref(null)
  const retrieval = ref([])
  const toolCalls = ref([])
  const requestError = ref('')
  const historyError = ref('')
  const ticketError = ref('')
  const loading = ref(false)
  const running = ref(false)
  const degraded = ref(false)
  const activeTicket = ref(null)
  const timeline = ref(null)
  const composer = ref(null)
  let request
  let lookup = 0
  let disposed = false

  const pending = computed(() => recovery.value[sessionId.value])
  const lastAssistant = computed(() => messages.value.findLast((message) => message.role === 'assistant'))
  const currentSources = computed(() => lastAssistant.value?.sources || [])
  const currentTicket = computed(() => {
    const linked = activeTicket.value || lastAssistant.value?.ticket
    return tickets.value.find(({ id }) => id === linked?.id) || linked
  })
  const currentTitle = computed(() => sessions.value.find(({ id }) => id === sessionId.value)?.title || '新会话')

  if (storageKey) {
    watch(sessionId, (value) => storeState(`${identity}:session`, value), { immediate: true, flush: 'sync' })
    watch(recovery, (value) => storeState(`${identity}:recovery`, value), { deep: true, flush: 'sync' })
  }

  function updateRecovery(value) {
    const updated = { ...recovery.value }
    if (value) updated[sessionId.value] = value
    else delete updated[sessionId.value]
    recovery.value = updated
  }

  function scrollDown() {
    nextTick(() => {
      if (timeline.value) timeline.value.scrollTop = timeline.value.scrollHeight
    })
  }

  async function refreshSessions() {
    try {
      const result = await api.sessions(token)
      if (!disposed) sessions.value = result.items
    } catch (error) {
      if (!disposed) historyError.value = error.message
    }
  }

  async function refreshTickets() {
    ticketError.value = ''
    try {
      const result = await api.tickets(token)
      if (!disposed) tickets.value = result.items
    } catch (error) {
      if (!disposed) ticketError.value = error.message
    }
  }

  async function readHistory({ append = false } = {}) {
    const call = ++lookup
    const id = sessionId.value
    loading.value = true
    historyError.value = ''
    try {
      const result = await api.messages(token, id, append ? nextOffset.value : 0)
      if (call !== lookup || disposed) return
      if (append) {
        const merged = new Map(messages.value.map((message) => [message.id, message]))
        for (const message of result.items) merged.set(message.id, { ...merged.get(message.id), ...message })
        messages.value = Array.from(merged.values()).sort((left, right) => new Date(left.created_at) - new Date(right.created_at))
      } else {
        messages.value = result.items
      }
      nextOffset.value = result.next_offset ?? null
      if (!append) {
        const restored = lastAssistant.value
        degraded.value = Boolean(restored?.degraded)
        if (typeof restored?.route === 'string') {
          route.value = { route: restored.route, reason: restored.reason, run_id: restored.run_id }
        }
        if (restored?.retrieval) retrieval.value = restored.retrieval
      }
      if (!append) scrollDown()
    } catch (error) {
      if (call === lookup && !disposed && error.status !== 404) historyError.value = error.message
    } finally {
      if (call === lookup) loading.value = false
    }
  }

  function openSession(id = newId()) {
    if (running.value || id === sessionId.value) return
    sessionId.value = id
    lookup += 1
    loading.value = false
    messages.value = []
    nextOffset.value = null
    draft.value = ''
    activeTicket.value = null
    degraded.value = false
    route.value = null
    retrieval.value = []
    toolCalls.value = []
    requestError.value = ''
    historyError.value = ''
    if (sessions.value.some((session) => session.id === id) || pending.value) void readHistory()
  }

  function applyAnswer(message, result) {
    const runId = result.run_id
    if (runId) {
      const question = messages.value.find(({ id }) => id === pending.value?.requestId)
      if (question) question.id = `${runId}:user`
      const answerId = `${runId}:assistant`
      if (messages.value.some((item) => item !== message && item.id === answerId)) {
        messages.value = messages.value.filter((item) => item !== message)
      } else {
        message.id = answerId
      }
    }
    message.content = result.answer ?? message.content
    message.sources = result.sources ?? message.sources
    message.ticket = result.ticket ?? message.ticket
    message.streaming = false
    degraded.value = Boolean(result.degraded)
    if (result.retrieval) retrieval.value = result.retrieval
    activeTicket.value = message.ticket
    if (result.route) {
      route.value = {
        ...route.value,
        route: result.route,
        reason: result.reason ?? route.value?.reason,
        run_id: runId || route.value?.run_id,
      }
    }
  }

  function onEvent(message, event, data) {
    if (data.run_id && pending.value?.runId !== data.run_id) {
      updateRecovery({ ...pending.value, runId: data.run_id })
    }
    if (event === 'route') route.value = data
    if (event === 'tool') toolCalls.value.push(data)
    if (event === 'sources') {
      message.sources = data.items || []
      retrieval.value = data.diagnostics || []
    }
    if (event === 'content') message.content += data.text || ''
    if (event === 'ticket') {
      message.ticket = data
      activeTicket.value = data
    }
    scrollDown()
  }

  async function perform(text, runId, retryRequestId) {
    if (running.value || loading.value) return
    const now = new Date().toISOString()
    const requestId = retryRequestId || newId()
    updateRecovery({ requestId, text, runId })
    requestError.value = ''
    running.value = true
    if (!runId) {
      route.value = null
      retrieval.value = []
      toolCalls.value = []
      degraded.value = false
      activeTicket.value = null
    }
    if (text && !retryRequestId && !runId) {
      messages.value.push({ id: requestId, role: 'user', content: text, created_at: now })
    }
    messages.value = messages.value.filter(({ failed }) => !failed)
    messages.value.push({
      id: `assistant-${requestId}`,
      role: 'assistant',
      content: '',
      sources: [],
      ticket: null,
      created_at: now,
      streaming: true,
    })
    const response = messages.value.at(-1)
    request = new AbortController()
    scrollDown()

    try {
      const result = runId
        ? await api.resume(token, runId)
        : await api.chat(token, { session_id: sessionId.value, message: text, request_id: requestId }, request.signal, (event, data) => {
            if (!disposed) onEvent(response, event, data)
          })
      if (disposed) return
      if (result.retryable || ['interrupted', 'pending', 'failed'].includes(result.status)) {
        const error = new Error(result.answer || result.message || '执行已暂停，可以恢复这次请求。')
        error.runId = result.run_id
        throw error
      }
      applyAnswer(response, result)
      updateRecovery(null)
      await Promise.all([refreshSessions(), refreshTickets()])
      if (retryRequestId) await readHistory()
    } catch (error) {
      if (disposed) return
      response.streaming = false
      response.failed = true
      requestError.value = error.message || '连接中断，回复可能已保存在会话中。'
      if (error.runId) updateRecovery({ ...pending.value, runId: error.runId })
      if ([400, 401, 403, 422].includes(error.status)) updateRecovery(null)
      if (runId && error.status === 404) updateRecovery({ ...pending.value, runId: undefined })
      if (error instanceof TypeError) requestError.value = '网络连接中断，回复可能已保存在会话中。'
    } finally {
      request = null
      if (!disposed) {
        running.value = false
        scrollDown()
      }
    }
  }

  function send() {
    const text = draft.value.trim()
    if (!text || running.value || loading.value || pending.value) return
    draft.value = ''
    void perform(text)
  }

  function recover() {
    if (!pending.value) return
    const { runId, text, requestId } = pending.value
    void perform(text, runId, requestId)
  }

  async function reopen() {
    await Promise.all([readHistory(), refreshSessions(), refreshTickets()])
  }

  function setDraft(value) {
    draft.value = value
    composer.value?.focus()
  }

  onMounted(() => void reopen())
  onUnmounted(() => {
    disposed = true
    request?.abort()
  })

  return {
    composer, currentSources, currentTicket, currentTitle, degraded, draft, historyError,
    loading, messages, nextOffset, pending, readHistory, recover, refreshTickets, reopen, requestError,
    retrieval, route, running, send, sessionId, sessions, setDraft, openSession, ticketError, tickets, timeline, toolCalls,
  }
}
