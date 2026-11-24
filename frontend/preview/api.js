import policies from '../../data/policies.json' with { type: 'json' }
import { savedState, storeState } from '../src/display.js'
import examples from './analytics.json' with { type: 'json' }
import { answerPolicy } from './policy.js'

const storageKey = 'support-pages:workbench:v1'
const orders = {
  'ORD-1001': { customer: 'demo-alice', delivery: '运输中，已到达杭州转运中心。运单号 YT731001。' },
  'ORD-1002': { customer: 'demo-alice', delivery: '已由本人签收。运单号 SF731002。', refund: '退款 REF-1002 正在处理。' },
  'ORD-2001': { customer: 'demo-bob', delivery: '已送达指定地址。运单号 JD732001。' },
}
const sampleTicket = {
  id: 'T-PREVIEW-01', run_id: 'preview-sample', customer: 'demo-alice', status: 'pending',
  owner: null, order_id: 'ORD-1001', created_at: '2026-11-24T02:20:00Z',
  reason: 'insufficient_evidence', customer_message: '活动赠品已经拆封，主商品还能退吗？',
}
const orderPattern = /\bORD-\d+\b/i
const handoffPattern = /(?:转|找|联系)人工|投诉|破损|催过/
const policyPattern = /退货|换货|退款|保修|政策|售后|赠品|发票|包裹|签收/
const normalizeQuestion = (question) => question.replace(/[\s？?。！!]+/g, '')

function requestError(status, message) {
  const error = new Error(message)
  error.status = status
  return error
}

function resolveAnswer(token, message, today) {
  if (handoffPattern.test(message)) {
    return { route: 'handoff', reason: '需要客服核实顾客诉求。', answer: '已创建演示工单，可切换到「客服工单」查看和接手。' }
  }

  const orderId = message.match(orderPattern)?.[0].toUpperCase()
  const { status, sources } = answerPolicy(message, policies, today)
  if (status === 'live') {
    if (!orderId) return { route: 'clarify', reason: 'missing_order_id', answer: '请提供一个演示订单号，例如 ORD-1001。' }
    const order = orders[orderId]
    const refund = /退款|售后进度/.test(message)
    const route = refund ? 'aftersale' : 'logistics'
    if (order?.customer !== token) return { route, answer: '在当前演示顾客名下找不到这个订单。' }
    const answer = refund ? order.refund || '此演示订单没有退款记录。' : order.delivery
    return { route, answer: orderId + ' ' + answer }
  }
  if (status === 'matched') {
    return { route: 'policy', reason: '命中适用的样例政策。', answer: sources.map(({ text }) => text).join('\n\n'), sources }
  }
  if (status === 'conflict' || policyPattern.test(message)) {
    const reason = status === 'conflict' ? 'conflicting_policy_versions' : 'insufficient_evidence'
    return { route: 'handoff', reason, answer: '目前的样例政策不足以确认。已创建演示工单，可在「客服工单」查看。' }
  }
  return { route: 'clarify', answer: '在线演示支持查询样例售后政策和订单。可以试试「退款一般多久到账？」。' }
}

function ticketView(ticket, staff = false) {
  const { customer, customer_message, ...fields } = ticket
  return staff ? { ...fields, customer_message } : fields
}

export function createPreviewApi({ read = savedState, write = storeState, today } = {}) {
  const state = read(storageKey) ?? { runs: [], tickets: [{ ...sampleTicket }], feedback: {} }
  const persist = () => write(storageKey, state)

  return {
    async health() {
      return { status: 'ok', mode: 'static' }
    },

    async sessions(token) {
      const sessions = new Map()
      for (const { customer, session, message, created_at } of state.runs) {
        if (customer !== token) continue
        const title = sessions.get(session)?.title ?? message
        sessions.set(session, { id: session, title, updated_at: created_at })
      }
      const items = [...sessions.values()].sort((first, second) => second.updated_at.localeCompare(first.updated_at))
      return { items }
    },

    async messages(token, session, offset = 0) {
      const history = state.runs.filter((run) => run.customer === token && run.session === session)
        .flatMap(({ message, created_at, result }) => {
          const { run_id, answer, ...details } = result
          return [
            { id: run_id + ':user', role: 'user', created_at, content: message },
            { id: run_id + ':assistant', role: 'assistant', created_at, content: answer, run_id, ...details },
          ]
        })
      const end = Math.max(0, history.length - offset)
      const start = Math.max(0, end - 50)
      return { items: history.slice(start, end), next_offset: start ? offset + 50 : null }
    },

    async chat(token, { session_id, request_id, message }, signal, onEvent) {
      signal?.throwIfAborted()
      const existing = state.runs.find((run) => run.customer === token && run.session === session_id && run.request === request_id)
      if (existing) {
        if (existing.message !== message) throw requestError(409, '同一请求编号已对应另一条消息。')
        return existing.result
      }
      const run_id = crypto.randomUUID().replaceAll('-', '')
      const created_at = new Date().toISOString()
      const result = { run_id, status: 'ok', sources: [], ticket: null, ...resolveAnswer(token, message, today) }
      if (result.route === 'handoff') {
        const orderId = message.match(orderPattern)?.[0].toUpperCase()
        const ticket = {
          id: 'T-' + run_id.slice(0, 12).toUpperCase(), run_id, created_at, customer: token, status: 'pending',
          owner: null, order_id: orders[orderId]?.customer === token ? orderId : null,
          reason: result.reason, customer_message: message,
        }
        state.tickets.unshift(ticket)
        result.ticket = ticketView(ticket)
      }
      state.runs.push({ customer: token, session: session_id, request: request_id, message, created_at, result })
      persist()
      onEvent?.('route', { route: result.route, reason: result.reason, run_id })
      return result
    },

    async resume(token, id) {
      const run = state.runs.find(({ customer, result }) => customer === token && result.run_id === id)
      if (!run) throw requestError(404, '找不到这个演示会话。')
      return run.result
    },

    async tickets(token) {
      const staff = token === 'demo-support'
      const items = state.tickets.filter(({ customer }) => staff || customer === token)
      return { items: items.map((ticket) => ticketView(ticket, staff)) }
    },

    async claim(token, id) {
      if (token !== 'demo-support') throw requestError(403, '请切换到客服工作区。')
      const ticket = state.tickets.find((item) => item.id === id)
      if (!ticket) throw requestError(404, '找不到这个演示工单。')
      ticket.status = 'claimed'
      ticket.owner = '客服专员'
      persist()
      return ticketView(ticket, true)
    },

    async analytics(token, question) {
      if (token !== 'demo-ops') throw requestError(403, '请切换到运营工作区。')
      const example = Object.entries(examples).find(([prompt]) => normalizeQuestion(prompt) === normalizeQuestion(question))
      return example?.[1] ?? {
        status: 'clarify', answer: '在线演示仅展示预先计算的样例结果，请选择上方的一个示例问题。', rows: [], columns: [],
      }
    },

    async feedback(token, { run_id, rating, comment }) {
      const run = state.runs.find(({ customer, result }) => customer === token && result.run_id === run_id)
      if (!run) throw requestError(404, '找不到可反馈的演示会话。')
      state.feedback[run_id] = { rating, comment }
      persist()
      return state.feedback[run_id]
    },
  }
}

export const api = createPreviewApi()
