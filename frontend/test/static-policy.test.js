import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

import { answerPolicy } from '../preview/policy.js'

const policies = JSON.parse(readFileSync(new URL('../../data/policies.json', import.meta.url), 'utf8'))
const today = '2026-11-24'

test('static policy answers cite the same clauses as the workbench', () => {
  for (const [question, ids] of [
    ['鞋买小了，能换大一码吗？', ['P-EXCHANGE']],
    ['退款一般多久到账？', ['P-REFUND-TIME']],
    ['七天退货条件、运费和发票多久能开？', ['P-RETURN', 'P-SHIPPING', 'P-INVOICE']],
  ]) {
    const result = answerPolicy(question, policies, today)
    assert.equal(result.status, 'matched')
    assert.deepEqual(result.sources.map((source) => source.id), ids)
    assert.ok(result.sources.every((source) => source.text && source.version && source.channel))
  }
})

test('static preview declines uncovered cases and personal order queries', () => {
  for (const question of [
    '活动赠品拆封了，主商品还能退吗？',
    '第三方平台买的鞋可以换货吗？',
    '这款电饭煲的额定功率是多少？',
    '为什么天空是蓝色的？',
  ]) {
    assert.deepEqual(answerPolicy(question, policies, today), { status: 'missing', sources: [] })
  }
  assert.deepEqual(answerPolicy('查 ORD-1001 的物流', policies, today), { status: 'live', sources: [] })
})

test('static evidence uses effective versions and stops on conflicts', () => {
  const original = policies.find(({ id }) => id === 'P-RETURN')
  const newer = { ...original, version: 'next', effective_from: '2026-12-01' }
  const conflicting = { ...original, version: 'same-date' }
  const question = '七天无理由退货有什么条件？'

  assert.equal(answerPolicy(question, [original, newer], today).sources[0].version, original.version)
  assert.equal(answerPolicy(question, [original, newer], '2026-12-01').sources[0].version, 'next')
  assert.deepEqual(answerPolicy(question, [original, conflicting], today), { status: 'conflict', sources: [] })
  assert.deepEqual(answerPolicy(question, [newer], today), { status: 'missing', sources: [] })
})
