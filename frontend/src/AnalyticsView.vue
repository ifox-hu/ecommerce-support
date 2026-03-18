<script setup>
import { computed, ref } from 'vue'
import { ElButton, ElInput, ElTable, ElTableColumn } from 'element-plus'
import { ArrowRight, DataAnalysis, Document, List, Promotion } from '@element-plus/icons-vue'
import { api } from './api'

const props = defineProps({ token: { type: String, required: true } })
const question = ref('')
const submitted = ref('')
const result = ref(null)
const queryOn = ref(false)
const error = ref('')
const input = ref(null)
const samples = ['上周华东渠道各品类退款率是多少？', '按渠道统计销售额', '按品类统计订单量']
const stepNames = {
  jieba_tfidf: '关键词提取',
  tokenize: '关键词提取',
  extract_terms: '关键词提取',
  llm_expand: '同义词扩展',
  retrieve: '业务口径召回',
  select_context: '筛选指标与字段',
  rank: '筛选指标与字段',
  refine: '筛选指标与字段',
  generate_sql: '生成 SQL',
  validate: 'SQL 校验',
  validate_sql: 'SQL 校验',
  explain: 'EXPLAIN 查询计划',
  execute: '执行查询',
  explain_result: '解释查询结果',
}
const stepStatuses = { repair: '修正', blocked: '拦截', error: '失败', degraded: '降级', demo: '演示' }
const columns = computed(() => (result.value?.columns || []).map((column) =>
  typeof column === 'string' ? { key: column, label: column } : { key: column.key || column.name, label: column.label || column.name }))
const rows = computed(() => (result.value?.rows || []).map((row) => Array.isArray(row)
  ? Object.fromEntries(row.map((value, index) => [columns.value[index]?.key, value]))
  : row))
const steps = computed(() => (result.value?.steps || []).map((step) => {
  if (typeof step === 'string') return { label: step }
  const name = step.label || step.name || step.step || step.stage || step.node || step.action
  return {
    label: stepNames[name] || name || '校验步骤',
    status: stepStatuses[step.status],
    detail: step.detail || step.message || step.reason,
    evidence: step.data && Object.keys(step.data).length ? JSON.stringify(step.data, null, 2) : '',
  }
}))
const blocked = computed(() => ['clarify', 'clarification', 'needs_clarification', 'rejected', 'blocked', 'error', 'failed'].includes(result.value?.status))

function chooseSample(sample) {
  question.value = sample
  input.value?.focus()
}

async function ask() {
  const text = question.value.trim()
  if (!text || queryOn.value) return
  queryOn.value = true
  error.value = ''
  result.value = null
  submitted.value = text
  try {
    result.value = await api.analytics(props.token, text)
  } catch (cause) {
    error.value = cause.message
  } finally {
    queryOn.value = false
  }
}
</script>

<template>
  <div class="workarea analytics-area">
    <header class="view-header" data-enter>
      <div class="view-title">
        <span class="view-icon"><DataAnalysis aria-hidden="true" /></span>
        <div><h1>运营问数</h1><p>查询订单、退款及渠道数据，核对指标口径和实际查询语句。</p></div>
      </div>
      <span class="status-pill">只读查询</span>
    </header>

    <section class="panel analytics-form" aria-label="提出运营问题">
      <form @submit.prevent="ask">
        <label for="analytics-question">你的数据问题</label>
        <div class="analytics-input">
          <el-input id="analytics-question" ref="input" v-model="question" :maxlength="1000" placeholder="例如，上周华东渠道各品类退款率是多少？" />
          <el-button type="primary" native-type="submit" :disabled="!question.trim()" :loading="queryOn"><Promotion aria-hidden="true" /> 查询</el-button>
        </div>
      </form>
      <div class="analytics-samples" aria-label="示例问题">
        <button v-for="sample in samples" :key="sample" type="button" @click="chooseSample(sample)">{{ sample }} <ArrowRight aria-hidden="true" /></button>
      </div>
    </section>

    <p v-if="error" class="notice warning" role="alert">{{ error }}</p>
    <div class="analytics-layout">
      <section class="panel analytics-results" aria-labelledby="result-title" :aria-busy="queryOn">
        <div class="table-toolbar"><h2 id="result-title">查询结果</h2><span v-if="rows.length" class="count">{{ rows.length }} 行</span></div>
        <div v-if="queryOn" class="empty-pane" role="status">正在匹配指标并校验查询…</div>
        <div v-else-if="result" class="result-content">
          <p class="query-question">{{ submitted }}</p>
          <p v-if="result.answer" class="query-answer" :class="{ 'query-warning': blocked }" role="status">{{ result.answer }}</p>
          <dl v-if="result.metric || result.metric_definition || result.time_range" class="definition">
            <template v-if="result.metric || result.metric_definition"><dt>指标口径</dt><dd>{{ result.metric_definition || result.metric }}</dd></template>
            <template v-if="result.time_range"><dt>时间范围</dt><dd>{{ result.time_range }}</dd></template>
          </dl>
          <el-table v-if="columns.length" :data="rows" stripe empty-text="没有匹配的数据">
            <el-table-column v-for="column in columns" :key="column.key" :prop="column.key" :label="column.label" min-width="105" />
          </el-table>
        </div>
        <p v-else class="empty-pane">提交数据问题后，结果会显示在这里。</p>
      </section>

      <aside class="panel query-details" aria-label="查询证据">
        <div class="section-title"><Document aria-hidden="true" /><h2>查询证据</h2></div>
        <section>
          <h3>实际 SQL</h3>
          <pre v-if="result?.sql"><code>{{ result.sql }}</code></pre>
          <p v-else class="context-empty">执行的 SQL 会在这里显示。</p>
        </section>
        <section>
          <h3><List aria-hidden="true" /> 校验与修正 <span v-if="result?.repairs" class="count">修正 {{ result.repairs }} 次</span></h3>
          <ol v-if="steps.length" class="query-steps">
            <li v-for="(step, index) in steps" :key="index">
              <strong>{{ step.label }}</strong> <span v-if="step.status" class="count">{{ step.status }}</span>
              <p v-if="step.detail">{{ step.detail }}</p>
              <details v-if="step.evidence" class="step-evidence">
                <summary>查看运行记录</summary><pre>{{ step.evidence }}</pre>
              </details>
            </li>
          </ol>
          <p v-else class="context-empty">查看查询校验及修正记录。</p>
        </section>
      </aside>
    </div>
  </div>
</template>
