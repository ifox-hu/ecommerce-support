import rules from '../../data/policy-search.json' with { type: 'json' }

const storeDate = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
})
const specificOrder = /\bORD-\d+\b|(?:我的|这笔|那笔|这个|这单).{0,12}(?:订单|物流|退款).{0,12}(?:到哪|状态|进度|发货|签收|到账)/i

function activePolicies(records, today) {
  const versions = new Map()
  for (const policy of records) {
    if (policy.effective_from > today) continue
    const current = versions.get(policy.id)
    if (!current || current[0].effective_from < policy.effective_from) {
      versions.set(policy.id, [policy])
    } else if (current[0].effective_from === policy.effective_from) {
      current.push(policy)
    }
  }
  return [...versions.values()]
}

export function answerPolicy(question, records, today = storeDate.format()) {
  const query = question.trim().normalize('NFKC')
  if (specificOrder.test(query)) return { status: 'live', sources: [] }

  const topics = new Set(Object.entries(rules.topics)
    .filter(([, terms]) => terms.some((term) => query.includes(term.normalize('NFKC'))))
    .map(([topic]) => topic))

  if (['shipping', 'exchange', 'refund_time'].some((topic) => topics.has(topic))
      && !/七天|退货期限|退货条件|能否退|还能退/.test(query)) {
    topics.delete('return')
  }
  if (topics.has('invoice_change')) topics.delete('invoice')

  const sources = []
  for (const versions of activePolicies(records, today)) {
    if (!versions.some(({ topic }) => topics.has(topic))) continue
    if (versions.length > 1) return { status: 'conflict', sources: [] }
    sources.push(versions[0])
  }

  const constraints = [...rules.uncovered, ...rules.product_specs].filter((term) => query.includes(term))
  const covered = new Set(sources.flatMap(({ covers = [] }) => covers))
  const matchedTopics = new Set(sources.map(({ topic }) => topic))
  if (!sources.length || [...topics].some((topic) => !matchedTopics.has(topic))
      || constraints.some((term) => !covered.has(term))) {
    return { status: 'missing', sources: [] }
  }
  return { status: 'matched', sources }
}
