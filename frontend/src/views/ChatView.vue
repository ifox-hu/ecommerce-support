<script setup>
import { computed, nextTick, ref } from 'vue'
import { ElButton, ElInput } from 'element-plus'
import {
  ArrowRight, ChatDotRound, Connection, Document, Goods, Headset, List, Plus, Promotion, Refresh, Tickets, Van,
} from '@element-plus/icons-vue'
import { api } from '../api'
import { formatDate, reasonLabel, routeLabel, savedState, storeState, ticketStatus } from '../display'
import { useChat } from '../useChat'

const props = defineProps({
  token: { type: String, required: true },
  storageKey: { type: String, required: true },
  profile: { type: Object, required: true },
})

const {
  composer, currentSources, currentTicket, currentTitle, degraded, draft, historyError, loading,
  messages, nextOffset, openSession, pending, readHistory, recover, refreshTickets, reopen, requestError,
  retrieval, route, running, send, sessionId, sessions, setDraft, ticketError, tickets, timeline, toolCalls,
} = useChat(props.token, props.storageKey)

const evidenceDialog = ref(null)
const detailSource = ref(null)
const feedbackDialog = ref(null)
const feedbackTarget = ref('')
const rating = ref(0)
const comment = ref('')
const feedbackError = ref('')
const submittingFeedback = ref(false)
const feedbackKey = props.storageKey && `support-demo:${props.storageKey}:feedback`
const submittedFeedback = ref(new Set(feedbackKey ? savedState(feedbackKey, []) : []))
const selectedTab = ref('evidence')
const samples = computed(() => [
  { title: '查售后政策', text: '普通商品七天无理由退货需要什么条件？', icon: Document },
  { title: '查订单物流', text: `订单 ${props.profile.order} 现在到哪了？`, icon: Van },
  { title: '查售后进度', text: `请查订单 ${props.profile.order} 的退款进度。`, icon: Goods },
  { title: '反馈商品破损', text: `订单 ${props.profile.order} 收到破损商品，已经催过两次，想投诉。`, icon: Headset },
])
const otherTickets = computed(() => tickets.value.filter(({ id }) => id !== currentTicket.value?.id))
const probabilities = computed(() => Object.entries(route.value?.probabilities || {})
  .filter(([, score]) => Number.isFinite(score))
  .sort(([, first], [, second]) => second - first)
  .slice(0, 3))
const rankingLabels = { dense: '语义召回', bm25: 'BM25 召回', rrf: 'RRF 融合', reranked: '候选精排' }
const toolLabels = { lookup_order: '查询订单', create_ticket: '创建工单' }

function showSource(source) {
  detailSource.value = source
  nextTick(() => evidenceDialog.value?.showModal())
}

function responseId(message) {
  if (message.role !== 'assistant' || message.failed || message.streaming) return ''
  return message.run_id || message.id.match(/^([a-f0-9]{32}):assistant$/)?.[1] || ''
}

function rateReply(id) {
  feedbackTarget.value = id
  rating.value = 0
  comment.value = ''
  feedbackError.value = ''
  feedbackDialog.value.showModal()
}

async function submitFeedback() {
  if (submittingFeedback.value || !rating.value || submittedFeedback.value.has(feedbackTarget.value)) return
  submittingFeedback.value = true
  try {
    await api.feedback(props.token, { run_id: feedbackTarget.value, rating: rating.value, comment: comment.value.trim() })
    submittedFeedback.value.add(feedbackTarget.value)
    if (feedbackKey) storeState(feedbackKey, [...submittedFeedback.value])
    feedbackDialog.value.close()
  } catch (error) {
    feedbackError.value = error.message
  } finally {
    submittingFeedback.value = false
  }
}

function onComposerKey(event) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault()
    send()
  }
}

</script>

<template>
  <div class="chat-layout">
    <aside class="panel history-panel" aria-label="会话列表">
      <div class="history-top">
        <div class="section-title"><List aria-hidden="true" /><h2>会话记录</h2></div>
        <el-button class="new-session" type="primary" :disabled="running" @click="openSession()">
          <Plus aria-hidden="true" /> 新建会话
        </el-button>
      </div>
      <div class="session-list">
        <p class="session-caption">最近会话</p>
        <button
          v-for="session in sessions"
          :key="session.id"
          type="button"
          class="session-link"
          :class="{ 'is-current': session.id === sessionId }"
          :aria-current="session.id === sessionId ? 'true' : undefined"
          :disabled="running"
          @click="openSession(session.id)"
        >
          <ChatDotRound aria-hidden="true" />
          <span>
            <strong>{{ session.title || '售后咨询' }}</strong>
            <small>{{ formatDate(session.updated_at) }}</small>
          </span>
        </button>
        <p v-if="!sessions.length && !historyError" class="history-empty">发起咨询后，可在这里继续会话。</p>
        <div v-if="historyError" class="history-empty">
          <p>{{ historyError }}</p>
          <button type="button" class="text-button" @click="reopen">重新读取</button>
        </div>
      </div>
      <div class="history-account">
        <span class="mini-avatar">{{ profile.initial }}</span>
        <div><strong>{{ profile.name }}</strong><small>当前演示顾客</small></div>
      </div>
    </aside>

    <section class="panel conversation" aria-labelledby="chat-heading">
      <header class="panel-heading conversation-heading">
        <div class="panel-title">
          <span class="tool-icon"><Headset aria-hidden="true" /></span>
          <div><h1 id="chat-heading">智能客服</h1><p>{{ currentTitle }}</p></div>
        </div>
        <button type="button" class="icon-button" title="重新读取会话" aria-label="重新读取会话" :disabled="running || loading" @click="reopen">
          <Refresh aria-hidden="true" />
        </button>
      </header>

      <div ref="timeline" class="timeline">
        <div v-if="loading && !messages.length" class="timeline-empty" role="status">正在读取会话…</div>
        <div v-else-if="!messages.length" class="chat-welcome" data-enter>
          <div class="welcome-illustration"><Headset aria-hidden="true" /><span></span></div>
          <h2>{{ profile.name }}，你好</h2>
          <p>可以查订单、了解商城政策，也可以直接问日常问题。</p>

          <div class="sample-grid" aria-label="常见问题">
            <button v-for="sample in samples" :key="sample.title" type="button" class="sample-card" @click="setDraft(sample.text)">
              <span class="sample-icon"><component :is="sample.icon" aria-hidden="true" /></span>
              <span><strong>{{ sample.title }}</strong><small>{{ sample.text }}</small></span>
              <ArrowRight class="sample-arrow" aria-hidden="true" />
            </button>
          </div>
        </div>
        <div v-else class="message-list" aria-live="polite" aria-label="客服对话">
          <div class="timeline-divider"><span>会话记录</span></div>
          <article v-for="message in messages" :key="message.id" class="message" :class="message.role">
            <span v-if="message.role === 'assistant'" class="message-avatar"><Headset aria-hidden="true" /></span>
            <span v-else class="message-avatar user-avatar" aria-hidden="true">{{ profile.initial }}</span>
            <div class="message-body">
              <div class="message-meta">
                <strong>{{ message.role === 'assistant' ? '智能客服' : profile.name }}</strong>
                <time v-if="message.created_at" :datetime="message.created_at">{{ formatDate(message.created_at) }}</time>
              </div>
              <div class="message-bubble" :class="{ failed: message.failed }">
                <p v-if="message.content">{{ message.content }}</p>
                <span v-if="message.streaming" class="generating"><span></span>{{ message.content ? '回复中…' : '正在处理请求…' }}</span>
                <span v-else-if="message.failed && !message.content">当前回复未完成。</span>
                <div v-if="message.sources?.length" class="inline-sources" aria-label="回复引用">
                  <button v-for="(source, index) in message.sources" :key="source.id" type="button" @click="showSource(source)">
                    {{ index + 1 }} · {{ source.title || source.id }}
                  </button>
                </div>
                <p v-if="message.ticket" class="message-ticket"><Tickets aria-hidden="true" /> 工单 {{ message.ticket.id }} · {{ ticketStatus(tickets.find(({ id }) => id === message.ticket.id)?.status || message.ticket.status) }}</p>
              </div>
              <template v-if="responseId(message)">
                <small v-if="submittedFeedback.has(responseId(message))" class="feedback-sent" role="status">评价已提交</small>
                <button v-else type="button" class="text-button feedback-link" @click="rateReply(responseId(message))">评价答复</button>
              </template>
            </div>
          </article>
          <button v-if="nextOffset !== null" type="button" class="text-button more-messages" :disabled="loading || running" @click="readHistory({ append: true })">
            {{ loading ? '正在读取…' : '读取更多记录' }}
          </button>
        </div>
      </div>

      <div class="chat-footer">
        <div v-if="(pending && !running) || requestError" class="notice warning" role="status">
          <p>{{ requestError || '上一次请求仍可恢复。' }}</p>
          <div v-if="pending" class="notice-actions">
            <button type="button" class="text-button" :disabled="loading" @click="recover">{{ pending.runId ? '恢复执行' : '重新连接' }}</button>
            <button type="button" class="text-button" :disabled="loading" @click="reopen">重新读取历史</button>
          </div>
        </div>
        <div v-else-if="degraded" class="notice warning" role="status">当前使用降级流程，详见本次回复。</div>
        <form class="composer" @submit.prevent="send">
          <el-input ref="composer" v-model="draft" type="textarea" :rows="2" resize="none" :maxlength="1500" aria-label="消息" placeholder="输入你想问的问题…" @keydown="onComposerKey" />
          <div class="composer-actions">
            <span>Enter 发送 · Shift + Enter 换行</span>
            <el-button type="primary" native-type="submit" :disabled="!draft.trim() || loading || running || Boolean(pending)">
              <Promotion aria-hidden="true" /> 发送
            </el-button>
          </div>
        </form>
      </div>
    </section>

    <aside class="panel context-panel" aria-labelledby="context-heading">
      <div class="context-heading">
        <div class="section-title"><Document aria-hidden="true" /><h2 id="context-heading">会话详情</h2></div>
        <div class="context-switch" aria-label="会话详情视图">
          <button type="button" :class="{ selected: selectedTab === 'evidence' }" :aria-pressed="selectedTab === 'evidence'" @click="selectedTab = 'evidence'">引用与路由</button>
          <button type="button" :class="{ selected: selectedTab === 'tickets' }" :aria-pressed="selectedTab === 'tickets'" @click="selectedTab = 'tickets'; refreshTickets()">我的工单</button>
        </div>
      </div>
      <div v-if="selectedTab === 'evidence'" class="context-content">
        <section>
          <h3>政策引用 <span class="count">{{ currentSources.length }}</span></h3>
          <p v-if="!currentSources.length" class="context-empty">答复引用的政策条款会在这里显示。</p>
          <button v-for="(source, index) in currentSources" :key="source.id" class="citation" type="button" @click="showSource(source)">
            <span class="citation-number">{{ String(index + 1).padStart(2, '0') }}</span>
            <span><strong>{{ source.title || source.id }}</strong><small>{{ source.version ? `版本 ${source.version}` : source.id }}</small></span>
            <ArrowRight aria-hidden="true" />
          </button>
          <details v-if="retrieval.length" class="retrieval-details">
            <summary>查看检索过程</summary>
            <section v-for="attempt in retrieval" :key="attempt.attempt">
              <h4>第 {{ attempt.attempt }} 次检索 · 证据充分度 {{ Math.round(attempt.sufficiency * 100) }}%</h4>
              <dl class="retrieval-rankings">
                <template v-for="(ranking, name) in attempt.rankings" :key="name">
                  <dt>{{ rankingLabels[name] || name }}</dt>
                  <dd>{{ ranking.map(({ id, score }) => `${id} (${score})`).join('、') || '无结果' }}</dd>
                </template>
              </dl>
            </section>
          </details>
        </section>
        <section class="route-section">
          <h3>处理路由</h3>
          <p v-if="!route" class="context-empty">发送问题后，可查看服务如何选择处理方式。</p>
          <template v-else>
            <p class="route-pill"><Connection aria-hidden="true" /> {{ routeLabel(route.route) }}</p>
            <p v-if="route.reason" class="route-reason">{{ reasonLabel(route.reason) }}</p>
            <dl v-if="probabilities.length" class="probabilities">
              <template v-for="[name, score] in probabilities" :key="name">
                <dt>{{ routeLabel(name) }}</dt><dd>{{ Math.round(score * 100) }}%</dd>
              </template>
            </dl>
            <small v-if="route.run_id" class="execution-id">执行编号 {{ route.run_id }}</small>
          </template>
          <ul v-if="toolCalls.length" class="tool-calls">
            <li v-for="(tool, index) in toolCalls" :key="index">
              {{ toolLabels[tool.name] || tool.name }} <span class="count">{{ tool.outcome === 'ok' ? '成功' : '暂不可用' }}</span>
            </li>
          </ul>
        </section>
      </div>
      <div v-else class="context-content tickets-context">
        <div class="small-heading">
          <h3>我的工单</h3>
          <button type="button" class="icon-button" aria-label="刷新我的工单" @click="refreshTickets"><Refresh aria-hidden="true" /></button>
        </div>
        <p v-if="ticketError" class="context-empty" role="alert">{{ ticketError }}</p>
        <p v-else-if="!currentTicket && !otherTickets.length" class="context-empty">转人工后，工单进度会在这里显示。</p>
        <article v-for="ticket in (currentTicket ? [currentTicket, ...otherTickets] : otherTickets)" :key="ticket.id" class="compact-ticket">
          <div><strong>{{ ticket.id }}</strong><span class="status-pill">{{ ticketStatus(ticket.status) }}</span></div>
          <p>{{ reasonLabel(ticket.reason) }}</p>
          <small v-if="ticket.order_id">订单 {{ ticket.order_id }}</small>
        </article>
      </div>
    </aside>

    <dialog ref="evidenceDialog" class="evidence-dialog" aria-labelledby="evidence-title" @click.self="evidenceDialog.close()">
      <article v-if="detailSource">
        <header><h2 id="evidence-title">{{ detailSource.title || detailSource.id }}</h2><button type="button" class="text-button" @click="evidenceDialog.close()">关闭</button></header>
        <p class="source-meta">
          {{ detailSource.id }}
          <span v-if="detailSource.version"> · 版本 {{ detailSource.version }}</span>
          <span v-if="detailSource.effective_from"> · {{ detailSource.effective_from }} 生效</span>
        </p>
        <p class="source-text">{{ detailSource.text }}</p>
      </article>
    </dialog>
    <dialog ref="feedbackDialog" class="evidence-dialog" aria-labelledby="feedback-title" @cancel="submittingFeedback && $event.preventDefault()">
      <article>
        <header><h2 id="feedback-title">评价这条答复</h2><button type="button" class="text-button" :disabled="submittingFeedback" @click="feedbackDialog.close()">取消</button></header>
        <form class="credential-form" @submit.prevent="submitFeedback">
          <fieldset class="rating-options">
            <legend>满意度（1 分最低，5 分最高）</legend>
            <label v-for="score in 5" :key="score"><input v-model="rating" type="radio" name="reply-rating" :value="score" /> {{ score }} 分</label>
          </fieldset>
          <label for="feedback-comment">补充意见（可选）</label>
          <el-input id="feedback-comment" v-model="comment" type="textarea" :rows="2" :maxlength="500" />
          <p v-if="feedbackError" class="notice warning" role="alert">{{ feedbackError }}</p>
          <div><el-button type="primary" native-type="submit" :disabled="!rating" :loading="submittingFeedback">提交评价</el-button></div>
        </form>
      </article>
    </dialog>
  </div>
</template>
