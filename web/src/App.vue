<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import { WorkbenchClient } from './client'
import { useConversation } from './useConversation'
import ConversationView from './ConversationView.vue'
import type { BackgroundTask, PermissionMode, ProcessLogPage, SessionView } from './contracts'

const fragment = new URLSearchParams(location.hash.slice(1))
const token = ref(fragment.get('token') ?? sessionStorage.getItem('xana.token') ?? '')
if (fragment.has('token')) history.replaceState(null, '', location.pathname + location.search)
const api = shallowRef<WorkbenchClient | null>(null), sessions = ref<SessionView[]>([])
const sid = ref(''), childSid = ref(''), text = ref(''), error = ref(''), connecting = ref(false), sending = ref(false)
const workspace = ref('工作区'), permission = ref<PermissionMode>('yolo'), tasks = ref<BackgroundTask[]>([])
const main = useConversation(api, sid), child = useConversation(api, childSid)
const showGoal = ref(false), objective = ref(''), criterion = ref(''), tokenBudget = ref('')
const logTarget = ref<{ sid: string; run: string; execution: string } | null>(null), logText = ref(''), logStatus = ref('')
const navigationOpen = ref(false), draftId = ref('')
const logBefore = ref<number | null>(null), logEarlier = ref(false), logLoading = ref(false)
const logView = ref<'tail' | 'page'>('tail'), logRevision = ref(0)
let permissionRevision = 0
const modalities = ref<string[]>([]), uploading = ref(false)
const attachments = ref<{ upload_id: string; name: string; kind: string }[]>([])
const mediaAccept = computed(() => [modalities.value.includes('image') ? 'image/png,image/jpeg,image/webp,image/gif' : '', modalities.value.includes('video') ? 'video/mp4' : ''].filter(Boolean).join(','))
const roots = computed(() => sessions.value.filter(item => !item.parent_session_id))
const title = computed(() => sessions.value.find(item => item.session_id === sid.value)?.title || '新会话')
const goalLabels = { active: '进行中', paused: '已暂停', blocked: '等待处理', complete: '已完成' }

async function refreshSessions() {
  const current = api.value
  if (!current) return
  const result = await current.request<{ sessions: SessionView[] }>('/sessions')
  if (api.value === current) sessions.value = result.sessions
}
async function login() {
  if (!token.value.trim()) { error.value = '请输入服务访问令牌。'; return }
  connecting.value = true; error.value = ''
  try {
    const client = new WorkbenchClient(token.value.trim())
    const [listing, info] = await Promise.all([
      client.request<{ sessions: SessionView[] }>('/sessions'), client.request<{ name: string }>('/workspace'),
    ])
    sessions.value = listing.sessions; workspace.value = info.name
    api.value = client; sessionStorage.setItem('xana.token', token.value.trim())
    const requested = new URLSearchParams(location.search).get('session')
    sid.value = listing.sessions.find(item => item.session_id === requested)?.session_id ?? roots.value[0]?.session_id ?? ''
    if (!sid.value) await newSession()
  } catch (failure) { error.value = String(failure); api.value = null }
  finally { connecting.value = false }
}
function logout() { api.value = null; sid.value = ''; childSid.value = ''; sessionStorage.removeItem('xana.token') }
async function newSession() {
  try {
    const result = await api.value!.post<{ session_id: string }>('/sessions')
    sid.value = result.session_id; childSid.value = ''; logTarget.value = null
    navigationOpen.value = false
    await refreshSessions()
  } catch (failure) { error.value = String(failure) }
}
async function send() {
  if (!api.value || !sid.value || (!text.value.trim() && !attachments.value.length) || sending.value || uploading.value) return
  sending.value = true; error.value = ''
  const sentText = text.value
  const sentAttachments = attachments.value
  try {
    draftId.value ||= crypto.randomUUID()
    await api.value.post(`/sessions/${encodeURIComponent(sid.value)}/submit`, { text: sentText, metadata: { input_id: draftId.value }, attachments: sentAttachments.map(item => ({ upload_id: item.upload_id })) })
    if (text.value === sentText) text.value = ''
    if (attachments.value === sentAttachments) attachments.value = []
    await refreshSessions()
  } catch (failure) { error.value = String(failure) }
  finally { sending.value = false }
}
function onKey(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.isComposing && !event.shiftKey && !event.ctrlKey && !event.altKey && !event.metaKey) {
    event.preventDefault(); void send()
  }
}
async function uploadMedia(event: Event) {
  const input = event.target as HTMLInputElement, files = Array.from(input.files ?? [])
  const session = sid.value, client = api.value
  input.value = ''
  if (!client || !files.length || uploading.value) return
  uploading.value = true
  try {
    if (attachments.value.length + files.length > 16) throw new Error('每条消息最多 16 个附件')
    for (const file of files) {
      const limit = file.type === 'video/mp4' ? 100 * 1024 * 1024 : 10 * 1024 * 1024
      if (file.size > limit) throw new Error('附件过大：图片最多 10 MiB，视频最多 100 MiB')
      const result = await client.upload<{ upload_id: string; kind: string }>(`/sessions/${encodeURIComponent(session)}/media`, file)
      if (sid.value !== session || api.value !== client) return
      attachments.value = [...attachments.value, { ...result, name: file.name }]
      draftId.value = ''
    }
  } catch (failure) { error.value = String(failure) }
  finally { uploading.value = false }
}
async function cancel() {
  const runId = main.activeRunId.value
  if (!runId) return
  try { await api.value!.post(`/sessions/${encodeURIComponent(sid.value)}/cancel`, { run_id: runId }) }
  catch (failure) { error.value = String(failure) }
}
async function steer(inputId: string) {
  try { await api.value!.post(`/sessions/${encodeURIComponent(sid.value)}/inputs/${encodeURIComponent(inputId)}/steer`) }
  catch (failure) { error.value = String(failure) }
}
async function setPermission() {
  const revision = ++permissionRevision, session = sid.value
  try {
    const result = await api.value!.post<{ mode: PermissionMode }>(`/sessions/${encodeURIComponent(session)}/permissions`, { mode: permission.value })
    if (sid.value === session && permissionRevision === revision) permission.value = result.mode
  }
  catch (failure) { error.value = String(failure) }
}
async function respond(id: string, decision: string, scope = 'once') {
  try { await api.value!.post(`/sessions/${encodeURIComponent(sid.value)}/approvals/${id}`, { decision, scope }) }
  catch (failure) { error.value = String(failure) }
}
async function controlGoal(action: string) {
  try {
    await api.value!.post(`/sessions/${encodeURIComponent(sid.value)}/goal`, { action,
      ...(action === 'start' ? { objective: objective.value, completion_criterion: criterion.value,
        ...(tokenBudget.value ? { token_budget: Number(tokenBudget.value) } : {}) } : {}),
    })
    if (action === 'start') { showGoal.value = false; objective.value = ''; criterion.value = ''; tokenBudget.value = '' }
  } catch (failure) { error.value = String(failure) }
}
async function stopTask(id: string) {
  try { await api.value!.post(`/sessions/${encodeURIComponent(sid.value)}/tasks/${encodeURIComponent(id)}/stop`) }
  catch (failure) { error.value = String(failure) }
}
function openChild(session: string) { childSid.value = session; logTarget.value = null }
function openLog(execution: string, run: string, session: string) {
  logView.value = 'tail'; logBefore.value = null
  logTarget.value = { sid: session, run, execution }; logText.value = ''; logStatus.value = '读取日志'
}
function logPath(target: { sid: string; run: string; execution: string }) {
  return `/sessions/${encodeURIComponent(target.sid)}/runs/${encodeURIComponent(target.run)}/logs/${encodeURIComponent(target.execution)}`
}
async function earlierLogs() {
  if (!logTarget.value || logBefore.value === null || logLoading.value) return
  const target = logTarget.value, boundary = logBefore.value
  logView.value = 'page'; logLoading.value = true
  try {
    const page = await api.value!.request<ProcessLogPage>(`${logPath(target)}?before=${boundary}`)
    if (logTarget.value !== target) return
    logText.value = page.records.map(item => item.text).join('')
    logBefore.value = page.start_offset; logEarlier.value = page.start_offset > 0
    logStatus.value = page.issue || '历史日志 · 已暂停跟随'
  } catch (failure) { logStatus.value = String(failure) }
  finally { logLoading.value = false }
}
async function downloadLog() {
  if (!logTarget.value) return
  try { await api.value!.download(`${logPath(logTarget.value)}/download`, 'command.log') }
  catch (failure) { logStatus.value = String(failure) }
}
watch(text, () => { draftId.value = '' }, { flush: 'sync' })

watch([api, sid], ([client, session], _, cleanup) => {
  tasks.value = []; childSid.value = ''; logTarget.value = null
  attachments.value = []; modalities.value = []
  if (!client || !session) return
  const abort = new AbortController()
  const revision = ++permissionRevision
  cleanup(() => abort.abort())
  const url = new URL(location.href); url.searchParams.set('session', session); history.replaceState(null, '', url)
  void client.request<{ input_modalities: string[] }>(`/sessions/${encodeURIComponent(session)}/media`, { signal: abort.signal })
    .then(result => { if (!abort.signal.aborted) modalities.value = result.input_modalities }).catch(failure => { if (!abort.signal.aborted) error.value = String(failure) })
  void client.request<{ mode: PermissionMode }>(`/sessions/${encodeURIComponent(session)}/permissions`, { signal: abort.signal })
    .then(result => { if (!abort.signal.aborted && permissionRevision === revision) permission.value = result.mode }).catch(failure => { if (!abort.signal.aborted) error.value = String(failure) })
  let loading = false
  const poll = async () => {
    if (loading) return
    loading = true
    try {
      const result = await client.request<{ tasks: BackgroundTask[] }>(`/sessions/${encodeURIComponent(session)}/tasks`, { signal: abort.signal })
      if (!abort.signal.aborted) tasks.value = result.tasks
    } catch (failure) { if (!abort.signal.aborted) error.value = String(failure) }
    finally { loading = false }
  }
  void poll(); const timer = setInterval(() => void poll(), 2000)
  cleanup(() => clearInterval(timer))
})
watch([logTarget, logView, logRevision], ([target, view], _, cleanup) => {
  if (!target || !api.value || view !== 'tail') return
  const abort = new AbortController(), client = api.value
  cleanup(() => abort.abort())
  void (async () => {
    let offset: number | null = null
    logText.value = ''
    while (!abort.signal.aborted) {
      try {
        const page: ProcessLogPage = await client.request<ProcessLogPage>(logPath(target) + (offset === null ? '' : `?offset=${offset}`), { signal: abort.signal })
        if (abort.signal.aborted) return
        if (offset === null) logBefore.value = page.start_offset
        const next = logText.value + page.records.map(item => item.text).join('')
        if (next.length > 524288) {
          // Re-read a bounded tail rather than accumulating an unbounded browser buffer.
          offset = null; logBefore.value = null; continue
        }
        logText.value = next
        logEarlier.value = (logBefore.value ?? 0) > 0
        offset = page.next_offset
        logStatus.value = page.issue || (page.complete ? '命令已结束' : '实时跟随')
        if (page.complete && !page.has_more) return
        if (page.has_more) continue
      } catch (failure) { if (abort.signal.aborted) return; logStatus.value = String(failure) }
      await new Promise<void>(resolve => {
        const wake = () => { clearTimeout(timer); abort.signal.removeEventListener('abort', wake); resolve() }
        const timer = setTimeout(wake, 1000); abort.signal.addEventListener('abort', wake, { once: true })
      })
    }
  })()
})
onMounted(() => { if (token.value) void login() })
</script>

<template>
  <div v-if="!api" class="login-screen">
    <form class="login-card" @submit.prevent="login">
      <span class="brand">xana<span class="brand-dot">.</span></span>
      <h1>回到你的工作区</h1><p>连接服务，继续对话和长期任务。</p>
      <label for="token">服务访问令牌</label><input id="token" v-model="token" type="password" autocomplete="off" autofocus>
      <p v-if="error" role="alert" class="danger">{{ error }}</p>
      <button class="primary" :disabled="connecting">{{ connecting ? '连接中…' : '连接工作区' }}</button>
    </form>
  </div>
  <div v-else class="workbench" :class="{ 'with-detail': childSid || logTarget, 'navigation-open': navigationOpen }">
    <aside class="sidebar">
      <div class="sidebar-top"><span class="brand">xana<span class="brand-dot">.</span></span><button class="icon-button" title="新建会话" @click="newSession">＋</button></div>
      <div class="workspace-label">工作区<span>{{ workspace }}</span></div>
      <button class="new-chat" @click="newSession">＋ 新建会话</button>
      <div class="section-label">最近会话 <button class="text-button" title="刷新会话" @click="refreshSessions">↻</button></div>
      <nav aria-label="会话列表">
        <button v-for="session in roots" :key="session.session_id" :class="{ selected: session.session_id === sid }" @click="sid = session.session_id; navigationOpen = false">
          <span class="session-title">{{ session.title || '新会话' }}</span><span v-if="session.running" class="live-dot" />
        </button>
      </nav>
      <div class="sidebar-bottom"><span>长期运行 · 持续迭代</span><button class="text-button" @click="logout">断开连接</button></div>
    </aside>
    <main class="main-pane">
      <header class="topbar"><button class="icon-button mobile-menu" aria-label="切换会话" @click="navigationOpen = !navigationOpen">☰</button><span class="session-title">{{ title }}</span><span class="connection"><i :class="{ online: main.connection.value === '已连接' }" />{{ main.connection.value }}</span></header>
      <div v-if="error || main.error.value" class="notice" role="alert">{{ error || main.error.value }}<button class="text-button" @click="error = ''; main.error.value = ''">关闭</button></div>
      <ConversationView :conversation="main" @steer="steer" @log="(id, run) => openLog(id, run, sid)" />
      <section v-if="main.approvals.value.length" class="approval-list" aria-label="待审批操作">
        <article v-for="approval in main.approvals.value" :key="approval.id" class="approval-card">
          <strong>需要确认 · {{ approval.tool_name }}</strong>
          <button v-if="approval.session_id !== sid" class="text-button" @click="openChild(approval.session_id)">查看子 agent 会话</button>
          <pre>{{ JSON.stringify(approval.arguments, null, 2) }}</pre>
          <div class="button-row"><button @click="respond(approval.id, 'rejected')">拒绝</button><button @click="respond(approval.id, 'approved', 'session')">本会话批准相同操作</button><button class="primary" @click="respond(approval.id, 'approved')">单次批准</button></div>
        </article>
      </section>
      <section class="task-dock" aria-label="任务进度">
        <div v-if="main.goal.value" class="goal-strip">
          <span class="status-pill">{{ goalLabels[main.goal.value.status] }}</span><strong>{{ main.goal.value.objective }}</strong>
          <span class="muted">{{ main.goal.value.tokens_used.toLocaleString() }} tokens<span v-if="main.goal.value.token_budget"> / {{ main.goal.value.token_budget.toLocaleString() }}</span></span>
          <button v-if="main.goal.value.status === 'active'" @click="controlGoal('pause')">暂停目标</button>
          <button v-if="['paused', 'blocked'].includes(main.goal.value.status)" @click="controlGoal('resume')">继续目标</button>
          <button v-if="main.goal.value.status !== 'complete'" @click="controlGoal('cancel')">取消目标</button>
        </div>
        <details v-if="main.checklist.value.length" class="checklist"><summary>Checklist · {{ main.checklist.value.filter(item => item.status === 'completed').length }}/{{ main.checklist.value.length }}</summary>
          <div v-for="(item, index) in main.checklist.value" :key="index" :class="item.status"><span>{{ item.status === 'completed' ? '✓' : item.status === 'in_progress' ? '◉' : '○' }}</span>{{ item.content }}</div>
        </details>
        <div v-if="tasks.length" class="background-tasks"><span class="section-label">后台任务</span>
          <article v-for="task in tasks" :key="task.id" class="task-card">
            <button v-if="task.content.agent_id" class="task-open" @click="openChild(task.content.agent_id)"><span :class="{ 'live-dot': task.status === 'running' }" /><strong>{{ task.label }}</strong><span>{{ task.status === 'running' ? '运行中 · 查看会话' : '查看会话' }}</span></button>
            <span v-else>{{ task.label }} · {{ task.status }}</span>
            <button v-if="task.status === 'running'" class="text-button danger" @click="stopTask(task.id)">停止任务</button>
          </article>
        </div>
      </section>
      <form class="composer" @submit.prevent="send">
        <div v-if="showGoal" class="goal-form"><label>目标<input v-model="objective" placeholder="希望完成什么？"></label><label>完成条件<input v-model="criterion" placeholder="如何确认完成（可选）"></label><label>Token 预算<input v-model="tokenBudget" type="number" min="1" placeholder="可选"></label><div class="button-row"><button type="button" @click="showGoal = false">取消</button><button type="button" class="primary" :disabled="!objective.trim()" @click="controlGoal('start')">开始目标</button></div></div>
        <textarea v-model="text" :placeholder="main.running.value ? '排队下一个任务，或发送后点击立即插话…' : '交代任务，开始对话…'" rows="3" aria-label="消息" @keydown="onKey" />
        <div v-if="modalities.length" class="media-input"><label>{{ uploading ? '上传中…' : '＋ 图片 / 视频' }}<input type="file" aria-label="添加图片或视频" :accept="mediaAccept" multiple :disabled="uploading" @change="uploadMedia"></label><button v-for="item in attachments" :key="item.upload_id" type="button" @click="attachments = attachments.filter(other => other !== item); draftId = ''">{{ item.name }} ×</button></div>
        <footer><div class="composer-controls"><select v-model="permission" aria-label="本会话权限" @change="setPermission"><option value="manual">始终询问</option><option value="yolo">必要时询问</option><option value="auto">全自动</option></select><button v-if="!main.goal.value || main.goal.value.status === 'complete'" type="button" class="text-button" @click="showGoal = !showGoal">＋ 目标</button></div><div class="button-row"><button v-if="main.running.value" type="button" class="stop-button" @click="cancel">■ 停止回答</button><button class="primary send-button" :disabled="(!text.trim() && !attachments.length) || sending || uploading">{{ sending ? '发送中' : main.running.value ? '排队 ↑' : '发送 ↑' }}</button></div></footer>
      </form>
      <p class="composer-hint">Enter 发送 · Shift + Enter 换行 · 关闭页面后任务继续运行</p>
    </main>
    <aside v-if="childSid || logTarget" class="detail-pane">
      <header class="topbar"><strong>{{ logTarget ? '命令日志' : '子 agent 会话' }}</strong><button class="icon-button" title="关闭详情" @click="childSid = ''; logTarget = null">×</button></header>
      <template v-if="logTarget"><div class="detail-meta">{{ logStatus }}<div class="button-row"><button v-if="logEarlier" :disabled="logLoading" @click="earlierLogs">更早日志</button><button v-if="logView === 'page'" @click="logView = 'tail'; logRevision++">返回最新</button><button @click="downloadLog">下载完整日志</button></div></div><pre class="command-log" data-testid="command-log">{{ logText || '等待命令输出…' }}</pre></template>
      <template v-else><div class="detail-meta">{{ child.running.value ? '运行中' : '已结束 / 空闲' }} · {{ child.connection.value }}</div><ConversationView :conversation="child" compact @log="(id, run) => openLog(id, run, childSid)" /></template>
    </aside>
  </div>
</template>
