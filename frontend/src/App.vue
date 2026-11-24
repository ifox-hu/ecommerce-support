<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { ElButton, ElInput } from 'element-plus'
import { ChatDotRound, Connection, DataAnalysis, Headset, Key, Switch, Tickets } from '@element-plus/icons-vue'
import { api } from '#workbench-api'
import ChatView from './views/ChatView.vue'
import SupportView from './views/SupportView.vue'
import AnalyticsView from './views/AnalyticsView.vue'
import { profiles, savedState, storeState } from './display'

const staticPreview = import.meta.env.MODE === 'pages'
const settingsKey = staticPreview ? 'support-pages' : 'support-demo'
const navigation = [
  { page: 'customer', label: '顾客对话', role: '顾客', token: 'demo-alice', icon: ChatDotRound },
  { page: 'support', label: '客服工单', role: '客服', token: 'demo-support', icon: Tickets },
  { page: 'analytics', label: '运营问数', role: '运营', token: 'demo-ops', icon: DataAnalysis },
]

function roleProfile(item) {
  const customer = savedState(settingsKey + ':customer', 'demo-alice')
  return item.role === '顾客' && profiles[customer]?.role === '顾客' ? customer : item.token
}

function profileFromHash() {
  const page = window.location.hash.slice(1)
  return roleProfile(navigation.find((item) => item.page === page) ?? navigation[0])
}

const storedProfile = savedState(settingsKey + ':profile', 'demo-alice')
const initialProfile = Object.hasOwn(profiles, storedProfile) ? storedProfile : 'demo-alice'
const profileId = ref(staticPreview ? profileFromHash() : initialProfile)
const profile = computed(() => profiles[profileId.value])
const service = ref(null)
const offline = ref(false)
const fullMode = computed(() => service.value?.mode === 'full')
const credentials = ref({})
const access = computed(() => fullMode.value
  ? credentials.value[profileId.value]
  : { token: profileId.value, storageKey: staticPreview ? 'pages:' + profileId.value : profileId.value })
const credentialDialog = ref(null)
const editingProfile = ref(null)
const tokenDraft = ref('')
const saving = ref(false)
let credentialVersion = 0

const healthLabel = computed(() => {
  if (offline.value) return '连接失败'
  if (!service.value) return '正在连接'
  return ['ok', 'healthy', 'up', 'ready'].includes(service.value.status) ? '服务在线' : '服务状态异常'
})
const modeLabel = computed(() => {
  if (staticPreview) return '在线演示'
  const mode = service.value?.mode
  if (['real', 'live', 'production', 'full'].includes(mode)) return '真实依赖'
  if (['demo', 'local', 'mock', 'synthetic'].includes(mode)) return '演示模式'
  return mode || '演示工作区'
})

function switchRole(item) {
  if (!staticPreview && profile.value.role !== item.role) profileId.value = roleProfile(item)
}

function followPage() {
  profileId.value = profileFromHash()
}

function selectIdentity(event) {
  if (!staticPreview) return
  const role = profiles[event.target.value].role
  window.location.hash = navigation.find((item) => item.role === role).page
}

function skipToWorkspace(event) {
  if (!staticPreview) return
  event.preventDefault()
  document.getElementById('workspace').focus()
}

async function refreshHealth() {
  try {
    service.value = await api.health()
    offline.value = false
  } catch {
    offline.value = true
  }
}

function editCredential() {
  editingProfile.value = profileId.value
  tokenDraft.value = ''
  credentialDialog.value.showModal()
}

async function saveCredential() {
  const token = tokenDraft.value.trim()
  const profileKey = editingProfile.value
  if (!token || saving.value) return
  saving.value = true
  try {
    let storageKey = ''
    if (crypto.subtle) {
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(token))
      storageKey = `full:${Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')}`
    }
    credentials.value = { ...credentials.value, [profileKey]: { token, storageKey, version: ++credentialVersion } }
    credentialDialog.value.close()
  } finally {
    saving.value = false
  }
}

function clearCredential() {
  const updated = { ...credentials.value }
  delete updated[editingProfile.value]
  credentials.value = updated
  credentialDialog.value.close()
}

watch(profileId, (current) => {
  storeState(settingsKey + ':profile', current)
  if (profiles[current].role === '顾客') storeState(settingsKey + ':customer', current)
  if (!staticPreview) void refreshHealth()
})
onMounted(() => {
  if (staticPreview) window.addEventListener('hashchange', followPage)
  void refreshHealth()
})
onUnmounted(() => {
  if (staticPreview) window.removeEventListener('hashchange', followPage)
})
</script>

<template>
  <a class="skip-link" href="#workspace" @click="skipToWorkspace">跳到工作区</a>
  <header class="topbar" :class="{ 'static-topbar': staticPreview }">
    <div class="brand">
      <span class="brand-mark" aria-hidden="true"><Headset /></span>
      <div>
        <div class="brand-name">智能售后服务台</div>
        <div class="brand-caption">电商客服工作区</div>
      </div>
    </div>

    <nav class="topnav" aria-label="工作区">
      <component
        v-for="item in navigation"
        :key="item.role"
        :is="staticPreview ? 'a' : 'button'"
        :href="staticPreview ? '#' + item.page : undefined"
        :type="staticPreview ? undefined : 'button'"
        :class="{ selected: profile.role === item.role }"
        :aria-current="profile.role === item.role ? 'page' : undefined"
        @click="switchRole(item)"
      >
        <component :is="item.icon" aria-hidden="true" />
        {{ item.label }}
      </component>
    </nav>

    <div class="top-actions">
      <a v-if="staticPreview" class="preview-local-link" href="https://github.com/ifox-hu/ecommerce-support#快速开始">本地版</a>
      <button v-else class="health" type="button" title="重新检查服务" @click="refreshHealth">
        <span class="health-dot" :class="{ offline: offline || healthLabel === '服务状态异常', waiting: !service && !offline }"></span>
        <span>{{ healthLabel }}</span>
      </button>
      <label class="identity">
        <span class="identity-avatar" aria-hidden="true">{{ profile.initial }}</span>
        <span class="visually-hidden">演示账号</span>
        <select v-model="profileId" @change="selectIdentity">
          <option v-for="(item, id) in profiles" :key="id" :value="id">{{ item.name }} · {{ item.role }}</option>
        </select>
        <Switch class="identity-switch" aria-hidden="true" />
      </label>
    </div>
  </header>
  <main id="workspace" class="workspace" tabindex="-1">
    <div class="workspace-bar">
      <p><Connection aria-hidden="true" /> {{ modeLabel }} <span>/</span> {{ navigation.find((item) => item.role === profile.role)?.label }}</p>
      <button v-if="fullMode" type="button" class="credential-action" @click="editCredential">
        <Key aria-hidden="true" /> {{ access ? '更换访问令牌' : '输入访问令牌' }}
      </button>
      <span v-else-if="staticPreview" class="workspace-role">样例数据，仅存于本浏览器</span>
      <span v-else-if="profile.role === '顾客'" class="workspace-role">当前顾客 <strong>{{ profile.name }}</strong></span>
    </div>
    <div v-if="!service && !offline" class="panel empty-pane" role="status">正在连接服务…</div>
    <div v-else-if="!access" class="panel credential-pane">
      <span class="view-icon"><Key aria-hidden="true" /></span>
      <h1>连接{{ profile.role }}工作区</h1>
      <p>输入为当前角色配置的访问令牌。刷新页面后需要重新输入。</p>
      <el-button type="primary" @click="editCredential">配置访问令牌</el-button>
    </div>
    <ChatView v-else-if="profile.role === '顾客'" :key="`${profileId}:${access.version || 0}`" :token="access.token" :storage-key="access.storageKey" :profile="profile" :static-preview="staticPreview" />
    <SupportView v-else-if="profile.role === '客服'" :key="`${profileId}:${access.version || 0}`" :token="access.token" />
    <AnalyticsView v-else :key="`${profileId}:${access.version || 0}`" :token="access.token" :static-preview="staticPreview" />
  </main>
  <dialog ref="credentialDialog" class="evidence-dialog" aria-labelledby="access-heading" @close="tokenDraft = ''">
    <article>
      <header><h2 id="access-heading">访问令牌</h2><button type="button" class="text-button" :disabled="saving" @click="credentialDialog.close()">取消</button></header>
      <form class="credential-form" @submit.prevent="saveCredential">
        <label for="credential-token">{{ profiles[editingProfile]?.name }} · {{ profiles[editingProfile]?.role }}</label>
        <el-input id="credential-token" v-model="tokenDraft" type="password" placeholder="输入服务端配置的令牌" autocomplete="off" />
        <div>
          <button v-if="credentials[editingProfile]" type="button" class="text-button" :disabled="saving" @click="clearCredential">清除令牌</button>
          <el-button type="primary" native-type="submit" :disabled="!tokenDraft.trim()" :loading="saving">连接工作区</el-button>
        </div>
      </form>
    </article>
  </dialog>
</template>
