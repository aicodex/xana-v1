<script setup lang="ts">
import { onMounted, ref } from 'vue'
import type { WorkbenchClient } from './client'
import type { McpServerView, McpServersPayload, McpToolView } from './contracts'

const props = defineProps<{ client: WorkbenchClient }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const payload = ref<McpServersPayload | null>(null)
const error = ref(''), busy = ref(false), showForm = ref(false)
const form = ref({
  name: '', transport: 'stdio' as 'stdio' | 'http' | 'sse',
  command: '', args: '', cwd: '', url: '',
  env: [{ key: '', value: '' }], headers: [{ key: '', value: '' }],
})

const statusLabels: Record<string, string> = {
  connecting: '连接中', connected: '已连接', retry_waiting: '等待重试',
  disabled: '已停用', failed: '失败', removed: '已移除',
}
const toolLabels: Record<string, string> = {
  enabled: '已启用', disabled_user: '用户停用', quarantined: '已隔离', unavailable: '不可用',
}
const originLabels: Record<string, string> = { user: '用户级', project: '项目级' }

async function refresh() {
  payload.value = await props.client.request<McpServersPayload>('/mcp/servers')
}

async function run(action: () => Promise<unknown>) {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    await action()
    await refresh()   // 每次操作完成后刷新权威 server 状态
  } catch (failure) {
    error.value = String(failure)
    await refresh().catch(() => {})
  } finally { busy.value = false }
}

function encode(name: string) { return encodeURIComponent(name) }

function toggleServer(server: McpServerView, enabled: boolean) {
  void run(() => props.client.put(`/mcp/servers/${encode(server.name)}/availability`, { enabled }))
}

function toggleTool(server: McpServerView, tool: McpToolView, checked: boolean) {
  const names = new Set(server.tools.filter(item => item.status === 'enabled').map(item => item.raw_name))
  if (checked) names.add(tool.raw_name)
  else names.delete(tool.raw_name)
  void run(() => props.client.put(`/mcp/servers/${encode(server.name)}/availability`, { enabled_tools: [...names] }))
}

function reloadServer(name: string) { void run(() => props.client.post(`/mcp/servers/${encode(name)}/reload`)) }
function reloadAll() { void run(() => props.client.post('/mcp/reload')) }
function removeServer(name: string) { void run(() => props.client.remove(`/mcp/servers/${encode(name)}`)) }

function pairs(rows: { key: string; value: string }[]) {
  return Object.fromEntries(rows.filter(row => row.key.trim()).map(row => [row.key.trim(), row.value]))
}

async function register() {
  const f = form.value
  const body: Record<string, unknown> = { name: f.name.trim(), transport: f.transport }
  if (f.transport === 'stdio') {
    if (!f.command.trim()) { error.value = 'stdio transport 需要启动命令。'; return }
    body.command = f.command.trim()
    body.args = f.args.split('\n').map(item => item.trim()).filter(Boolean)
    if (f.cwd.trim()) body.cwd = f.cwd.trim()
    const env = pairs(f.env)
    if (Object.keys(env).length) body.env = env
  } else {
    if (!f.url.trim()) { error.value = `${f.transport} transport 需要 URL。`; return }
    body.url = f.url.trim()
    const headers = pairs(f.headers)
    if (Object.keys(headers).length) body.headers = headers
  }
  await run(async () => {
    await props.client.post('/mcp/servers', body)
    showForm.value = false
    form.value = { name: '', transport: f.transport, command: '', args: '', cwd: '', url: '',
      env: [{ key: '', value: '' }], headers: [{ key: '', value: '' }] }
  })
}

onMounted(() => { void refresh().catch(failure => { error.value = String(failure) }) })
</script>

<template>
  <section class="mcp-panel" data-testid="mcp-panel" aria-label="MCP 服务器">
    <header class="topbar">
      <strong>MCP 服务器</strong>
      <div class="button-row">
        <button :disabled="busy" @click="reloadAll">全部重连</button>
        <button class="primary" :disabled="busy" @click="showForm = !showForm">注册 MCP server</button>
        <button class="icon-button" title="关闭面板" @click="emit('close')">×</button>
      </div>
    </header>
    <div class="mcp-body">
      <p v-if="error" role="alert" class="danger">{{ error }}</p>
      <p v-if="payload?.load_error" role="alert" class="danger">配置加载失败:{{ payload.load_error }}</p>
      <p v-if="!payload" class="muted">加载中…</p>
      <p v-else-if="!payload.servers.length" class="muted">尚未配置 MCP server。</p>
      <article v-for="server in payload?.servers ?? []" :key="server.name" class="mcp-server" :data-testid="`mcp-server-${server.name}`">
        <header>
          <input type="checkbox" :aria-label="`启用 ${server.name}`" :checked="server.enabled" :disabled="busy" @change="toggleServer(server, ($event.target as HTMLInputElement).checked)">
          <strong>{{ server.name }}</strong>
          <span class="status-pill">{{ originLabels[server.origin] }}</span>
          <span class="status-pill">{{ server.transport }}</span>
          <span class="status-pill" :class="`mcp-${server.status}`">{{ statusLabels[server.status] }}</span>
          <span v-if="server.retry_in_s != null" class="muted">{{ Math.ceil(server.retry_in_s) }}s 后重试</span>
          <div class="button-row">
            <button :disabled="busy" @click="reloadServer(server.name)">重连</button>
            <button v-if="server.origin === 'project'" class="mcp-delete" :disabled="busy" @click="removeServer(server.name)">删除</button>
          </div>
        </header>
        <p v-if="server.config_error" class="danger">配置错误:{{ server.config_error }}</p>
        <p v-if="server.error" class="danger">{{ server.error }}</p>
        <ul class="mcp-tools">
          <li v-for="tool in server.tools" :key="tool.name">
            <label v-if="tool.status === 'enabled' || tool.status === 'disabled_user'">
              <input type="checkbox" :aria-label="tool.raw_name" :checked="tool.status === 'enabled'" :disabled="busy" @change="toggleTool(server, tool, ($event.target as HTMLInputElement).checked)">
              <span>{{ tool.raw_name }}</span>
            </label>
            <span v-else>{{ tool.raw_name }}</span>
            <span class="status-pill">{{ toolLabels[tool.status] }}</span>
            <ul v-if="tool.findings.length" class="mcp-findings">
              <li v-for="(finding, index) in tool.findings" :key="index" class="danger">{{ finding.rule }} · {{ finding.message }}</li>
            </ul>
          </li>
        </ul>
      </article>
      <form v-if="showForm" class="mcp-form" @submit.prevent="register">
        <label>server 名称<input v-model="form.name" aria-label="server 名称" required></label>
        <label>transport
          <select v-model="form.transport" aria-label="transport">
            <option value="stdio">stdio</option><option value="http">http</option><option value="sse">sse</option>
          </select>
        </label>
        <template v-if="form.transport === 'stdio'">
          <label>启动命令<input v-model="form.command" aria-label="启动命令" placeholder="可执行文件路径"></label>
          <label>参数<textarea v-model="form.args" aria-label="参数" rows="2" placeholder="每行一个参数"></textarea></label>
          <label>工作目录<input v-model="form.cwd" aria-label="工作目录"></label>
          <fieldset class="mcp-kv">
            <legend>环境变量(值仅保存,不回显)</legend>
            <div v-for="(row, index) in form.env" :key="index" class="mcp-kv-row">
              <input v-model="row.key" aria-label="环境变量名" placeholder="名称">
              <input v-model="row.value" type="password" autocomplete="new-password" aria-label="环境变量值" placeholder="值">
              <button type="button" class="text-button" :disabled="form.env.length <= 1" @click="form.env.splice(index, 1)">移除</button>
            </div>
            <button type="button" class="text-button" @click="form.env.push({ key: '', value: '' })">＋ 添加环境变量</button>
          </fieldset>
        </template>
        <template v-else>
          <label>URL<input v-model="form.url" aria-label="URL" placeholder="https://…"></label>
          <fieldset class="mcp-kv">
            <legend>请求头(值仅保存,不回显)</legend>
            <div v-for="(row, index) in form.headers" :key="index" class="mcp-kv-row">
              <input v-model="row.key" aria-label="请求头名" placeholder="名称">
              <input v-model="row.value" type="password" autocomplete="new-password" aria-label="请求头值" placeholder="值">
              <button type="button" class="text-button" :disabled="form.headers.length <= 1" @click="form.headers.splice(index, 1)">移除</button>
            </div>
            <button type="button" class="text-button" @click="form.headers.push({ key: '', value: '' })">＋ 添加请求头</button>
          </fieldset>
        </template>
        <div class="button-row mcp-form-actions">
          <button type="button" @click="showForm = false">取消</button>
          <button class="primary" type="submit" :disabled="busy">注册</button>
        </div>
      </form>
    </div>
  </section>
</template>
