export const profiles = {
  'demo-alice': { name: 'Alice', role: '顾客', initial: 'A', order: 'ORD-1001' },
  'demo-bob': { name: 'Bob', role: '顾客', initial: 'B', order: 'ORD-2001' },
  'demo-support': { name: '客服专员', role: '客服', initial: '服' },
  'demo-ops': { name: '运营分析师', role: '运营', initial: '运' },
}

const dates = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })

export function formatDate(value) {
  const date = new Date(value)
  return value && !Number.isNaN(date.getTime()) ? dates.format(date) : ''
}

const statuses = {
  pending: '待接单', open: '待接单', queued: '待接单', new: '待接单',
  claimed: '已接单', assigned: '已接单', in_progress: '处理中',
  resolved: '已解决', closed: '已关闭',
}
export const ticketStatus = (status) => statuses[status] || status || '待同步'

const routes = {
  policy: '政策咨询', policy_qa: '政策咨询', rag: '政策检索',
  order: '订单查询', logistics: '物流查询', order_logistics: '订单与物流',
  aftersale: '售后进度', after_sales: '售后进度', refund: '退款查询',
  complaint: '售后投诉', ticket: '创建工单', human: '转人工', handoff: '转人工',
  agent: '复杂售后', complex: '复杂售后', clarify: '补充信息', fallback: '降级处理',
  general: '通用问答', professional: '专业资料查询',
}
export const routeLabel = (route) => routes[route] || route

const reasons = {
  'matched a deterministic rule': '命中明确的售后处理规则。',
  'continued a pending order lookup': '已补齐订单号，继续上一轮查询。',
  'continued the previous policy question': '接着上一轮的话题查询政策。',
  'classifier threshold applied': '当前意图无法确认，需要顾客补充信息。',
  'classifier probability': '按分类置信度选择对应流程。',
  missing_order_id: '需要提供订单号。',
  model_unavailable: '回复模型暂不可用，已转入降级流程。',
  intent_service_unavailable: '意图识别服务暂不可用，已转入人工处理。',
  evidence_service_unavailable: '知识库暂时无法查询。',
  insufficient_evidence: '现有资料不足以确认这个问题。',
  conflicting_policy_versions: '相关政策版本存在冲突。',
  general_model_unavailable: '通用问答服务暂时不可用。',
  answer_service_unavailable: '回复暂无法生成，需要人工处理。',
  complex_case: '需要综合订单状态、顾客陈述和政策条款。',
}
export const reasonLabel = (reason) => reasons[reason] || reason

export function savedState(key, fallback) {
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback
  } catch {
    return fallback
  }
}

export function storeState(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    // Storage can be unavailable in private or restricted browser contexts.
  }
}
