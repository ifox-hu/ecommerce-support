import assert from 'node:assert/strict'
import { test } from 'node:test'

import { createPreviewApi } from '../preview/api.js'

const chat = (api, message, request = 'first', token = 'demo-alice') =>
  api.chat(token, { message, session_id: 'preview', request_id: request })

test('the static workbench links customer evidence, browser history, and support tickets', async () => {
  let snapshot
  const storage = {
    today: '2026-11-24',
    read: () => snapshot,
    write: (_, value) => { snapshot = structuredClone(value) },
  }
  const api = createPreviewApi(storage)

  const policy = await chat(api, '退款一般多久到账？')
  assert.equal(policy.route, 'policy')
  assert.deepEqual(policy.sources.map(({ id }) => id), ['P-REFUND-TIME'])

  const request = '活动赠品拆封了，主商品还能退吗？'
  const handoff = await chat(api, request, 'handoff')
  assert.equal(handoff.route, 'handoff')
  assert.equal((await chat(api, request, 'handoff')).ticket.id, handoff.ticket.id)
  assert.equal((await api.sessions('demo-alice')).items.length, 1)
  assert.deepEqual((await api.sessions('demo-bob')).items, [])

  const reopened = createPreviewApi(storage)
  const history = (await reopened.messages('demo-alice', 'preview')).items
  assert.deepEqual(history.map(({ role }) => role), ['user', 'assistant', 'user', 'assistant'])
  const { items } = await reopened.tickets('demo-support')
  assert.equal(items.filter(({ id }) => id === handoff.ticket.id).length, 1)
  assert.equal(items.find(({ id }) => id === handoff.ticket.id).customer_message, request)
  assert.deepEqual((await reopened.tickets('demo-bob')).items, [])
  assert.equal((await reopened.claim('demo-support', handoff.ticket.id)).status, 'claimed')
  assert.equal((await reopened.tickets('demo-alice')).items.find(({ id }) => id === handoff.ticket.id).status, 'claimed')
})

test('the static workbench only returns a sample order to its demo customer', async () => {
  const api = createPreviewApi()
  const question = '订单 ORD-1001 现在到哪了？'
  assert.match((await chat(api, question)).answer, /杭州/)
  assert.doesNotMatch((await chat(api, question, 'first', 'demo-bob')).answer, /杭州/)
})

test('static analytics shows recorded sample results and declines other queries', async () => {
  const api = createPreviewApi()
  const examples = [
    ['上周华东渠道各品类退款率是多少？', [['家居', 0], ['数码', 0.5], ['鞋服', 0.5]]],
    ['按渠道统计销售额', [['华东官方商城', 2830], ['华东微信小程序', 820], ['华南官方商城', 340]]],
    ['按品类统计订单量', [['数码', 5], ['鞋服', 4], ['家居', 3]]],
  ]
  for (const [question, expected] of examples) {
    const sample = await api.analytics('demo-ops', question)
    assert.equal(sample.status, 'ok')
    assert.deepEqual(sample.rows, expected)
    assert.match(sample.sql, /^SELECT/i)
  }
  const unknown = await api.analytics('demo-ops', '查询今年四月的订单量')
  assert.equal(unknown.status, 'clarify')
  assert.deepEqual(unknown.rows, [])
})
