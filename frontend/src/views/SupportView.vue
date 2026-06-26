<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElButton } from 'element-plus'
import { ArrowRight, CircleCheck, Refresh, Tickets, Timer } from '@element-plus/icons-vue'
import { api } from '../api'
import { formatDate, reasonLabel, ticketStatus } from '../display'

const props = defineProps({ token: { type: String, required: true } })
const items = ref([])
const selectedId = ref(null)
const tab = ref('all')
const loading = ref(false)
const claiming = ref(false)
const error = ref('')
const note = ref('')

const canClaim = (ticket) => ticketStatus(ticket.status) === '待接单'
const waiting = computed(() => items.value.filter(canClaim))
const handled = computed(() => items.value.filter((item) => !canClaim(item)))
const visible = computed(() => {
  if (tab.value === 'waiting') return waiting.value
  if (tab.value === 'handled') return handled.value
  return items.value
})
const selected = computed(() => visible.value.find(({ id }) => id === selectedId.value) || visible.value[0])
const filters = [
  { id: 'all', label: '全部工单' },
  { id: 'waiting', label: '待接单' },
  { id: 'handled', label: '已处理' },
]

async function reload() {
  error.value = ''
  loading.value = true
  try {
    const result = await api.tickets(props.token)
    items.value = result.items
  } catch (cause) {
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

async function claim() {
  const ticket = selected.value
  if (!ticket || !canClaim(ticket) || claiming.value) return
  claiming.value = true
  error.value = ''
  note.value = ''
  try {
    const result = await api.claim(props.token, ticket.id)
    items.value = items.value.map((item) => item.id === ticket.id ? { ...item, ...result } : item)
    selectedId.value = ticket.id
    tab.value = 'all'
    note.value = `已接手工单 ${ticket.id}。`
  } catch (cause) {
    await reload()
    error.value = cause.message
  } finally {
    claiming.value = false
  }
}

onMounted(() => void reload())
</script>

<template>
  <div class="workarea">
    <header class="view-header" data-enter>
      <div class="view-title">
        <span class="view-icon"><Tickets aria-hidden="true" /></span>
        <div><h1>客服工单</h1><p>查阅售后诉求、相关证据，并接手待处理工单。</p></div>
      </div>
      <el-button :loading="loading" :disabled="claiming" @click="reload"><Refresh aria-hidden="true" /> 刷新工单</el-button>
    </header>

    <div class="metrics" aria-label="工单概览">
      <div><Tickets aria-hidden="true" /><span>全部工单<strong>{{ items.length }}</strong></span></div>
      <div><Timer aria-hidden="true" /><span>待接单<strong>{{ waiting.length }}</strong></span></div>
      <div><CircleCheck aria-hidden="true" /><span>已接单或处理<strong>{{ handled.length }}</strong></span></div>
    </div>

    <div v-if="error" class="notice warning" role="alert">{{ error }}</div>
    <div v-if="note" class="notice success" role="status">{{ note }}</div>

    <div class="support-layout">
      <section class="panel ticket-list" aria-label="工单列表">
        <div class="table-toolbar">
          <h2>工单队列</h2>
          <div class="context-switch" aria-label="筛选工单">
            <button v-for="filter in filters" :key="filter.id" type="button" :aria-pressed="tab === filter.id" :class="{ selected: tab === filter.id }" @click="tab = filter.id">
              {{ filter.label }}
            </button>
          </div>
        </div>
        <p v-if="!visible.length" class="empty-pane">{{ loading ? '正在读取工单…' : '这个队列暂无工单。' }}</p>
        <button v-for="item in visible" :key="item.id" type="button" class="ticket-row" :class="{ selected: item.id === selected?.id }" :aria-pressed="item.id === selected?.id" @click="selectedId = item.id">
          <span class="tool-icon"><Tickets aria-hidden="true" /></span>
          <span class="ticket-row-text"><strong>{{ item.id }}</strong><small>{{ reasonLabel(item.reason) }}</small></span>
          <span class="status-pill" :class="{ 'status-waiting': canClaim(item) }">{{ ticketStatus(item.status) }}</span>
          <ArrowRight class="row-arrow" aria-hidden="true" />
        </button>
      </section>
      <section class="panel detail-pane" aria-labelledby="ticket-heading">
        <h2 id="ticket-heading">工单详情</h2>
        <div v-if="selected" class="ticket-detail">
          <p class="detail-id">{{ selected.id }}</p>
          <dl>
            <dt>状态</dt><dd><span class="status-pill" :class="{ 'status-waiting': canClaim(selected) }">{{ ticketStatus(selected.status) }}</span></dd>
            <dt>订单号</dt><dd>{{ selected.order_id || '未提供' }}</dd>
            <dt>创建时间</dt><dd>{{ formatDate(selected.created_at) || '—' }}</dd>
            <dt>处理人</dt><dd>{{ selected.owner || selected.assignee || '待认领' }}</dd>
          </dl>
          <h3>客户诉求</h3>
          <p class="ticket-reason">{{ selected.customer_message || reasonLabel(selected.reason) }}</p>
          <template v-if="selected.case_summary?.analysis">
            <h3>客服助手整理</h3>
            <p class="ticket-reason">{{ selected.case_summary.analysis }}</p>
          </template>
          <template v-if="selected.evidence || selected.sources?.length">
            <h3>相关证据</h3>
            <p v-if="typeof selected.evidence === 'string'" class="ticket-reason">{{ selected.evidence }}</p>
            <ul v-if="selected.sources?.length || Array.isArray(selected.evidence)" class="ticket-evidence">
              <li v-for="(source, index) in selected.sources || selected.evidence" :key="source.id || index">
                <strong>{{ source.title || source.id || source }}</strong>
                <p v-if="source.text">{{ source.text }}</p>
              </li>
            </ul>
          </template>
          <el-button type="primary" :loading="claiming" :disabled="!canClaim(selected)" @click="claim">
            <CircleCheck aria-hidden="true" /> {{ canClaim(selected) ? '接手工单' : ticketStatus(selected.status) }}
          </el-button>
        </div>
        <p v-else class="empty-pane">从队列选择工单查看详情。</p>
      </section>
    </div>
  </div>
</template>
