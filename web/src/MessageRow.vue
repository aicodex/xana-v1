<script setup lang="ts">
import { computed } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import type { HistoryRow } from './contracts'

const props = defineProps<{ row: HistoryRow; runId: string }>()
const emit = defineEmits<{ log: [executionId: string, runId: string] }>()
const html = computed(() => DOMPurify.sanitize(marked.parse(props.row.payload.content, { async: false })))
const metadata = computed(() => props.row.payload.metadata)
const label = computed(() => ({ user: '你', assistant: 'Xana', tool: metadata.value.tool_name ?? '工具', compact: '上下文已压缩', system: '系统' })[props.row.payload.role] ?? props.row.payload.role)
</script>

<template>
  <details v-if="metadata.note_kind || row.payload.role === 'compact'" class="context-note">
    <summary>{{ metadata.note_kind ?? '上下文压缩' }}</summary><pre>{{ row.payload.content }}</pre>
  </details>
  <article v-else class="message" :class="row.payload.role" :data-testid="`message-${row.payload.role}`">
    <div class="message-label"><span>{{ label }}</span><span v-if="row.payload.role === 'tool' && metadata.ok === false" class="danger">执行未成功</span></div>
    <details v-if="metadata.reasoning_content" class="thinking"><summary>思考过程</summary><pre>{{ metadata.reasoning_content }}</pre></details>
    <template v-if="row.payload.role === 'tool'">
      <details><summary>查看执行结果</summary><pre>{{ row.payload.content }}</pre></details>
      <button v-if="metadata.tool_name === 'Bash' && metadata.log_available && metadata.execution_id" class="text-button" @click="emit('log', String(metadata.execution_id), runId)">查看命令日志</button>
    </template>
    <div v-else class="markdown" v-html="html" />
    <p v-if="row.payload.media?.length" class="muted">附件：{{ row.payload.media.map(item => `${item.kind === 'image' ? '图片' : '视频'} (${(item.size_bytes / 1024).toFixed(0)} KB)`).join('、') }}</p>
    <details v-for="call in metadata.tool_calls ?? []" :key="call.execution_id ?? call.call_id" class="tool-call">
      <summary>{{ call.tool_name }} <span class="muted">工具调用</span></summary>
      <pre>{{ JSON.stringify(call.input, null, 2) }}</pre>
      <button v-if="call.tool_name === 'Bash' && call.execution_id" class="text-button" @click="emit('log', call.execution_id, runId)">查看命令日志</button>
    </details>
  </article>
</template>
