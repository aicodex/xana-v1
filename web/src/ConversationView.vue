<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import type { ConversationState } from './useConversation'
import MessageRow from './MessageRow.vue'
const props = defineProps<{ conversation: ConversationState; compact?: boolean }>()
const emit = defineEmits<{ log: [executionId: string, runId: string]; steer: [inputId: string] }>()
const pane = ref<HTMLElement>(), follow = ref(true)
function onScroll() {
  if (pane.value) follow.value = pane.value.scrollHeight - pane.value.scrollTop - pane.value.clientHeight < 120
}
watch(() => [props.conversation.rows.value.length, props.conversation.partials.value.map(item => item.text + item.thinking).join('')], async () => {
  if (follow.value) { await nextTick(); pane.value?.scrollTo({ top: pane.value.scrollHeight }) }
})
</script>

<template>
  <section ref="pane" class="conversation" :class="{ compact }" @scroll="onScroll" data-testid="conversation">
    <button v-if="conversation.hasMore.value" class="load-more" :disabled="conversation.loadingOlder.value" @click="conversation.loadOlder()">加载更早的对话</button>
    <div v-if="conversation.issues.value.length" class="notice">部分历史记录不可用：{{ conversation.issues.value.join('；') }}</div>
    <div v-if="!conversation.rows.value.length && !conversation.running.value" class="empty-conversation">
      <div class="empty-mark">x</div><h1>让工作持续向前</h1><p>交代一个任务，或从目标开始。</p>
    </div>
    <div class="transcript">
      <MessageRow v-for="row in conversation.rows.value" :key="row.row_id" :row="row" :run-id="row.run_id" @log="(id, run) => emit('log', id, run)" />
      <article v-for="item in conversation.partials.value" :key="item.row_id" class="message assistant streaming">
        <div class="message-label">Xana <span class="live-dot" /></div>
        <details v-if="item.thinking"><summary>正在思考</summary><pre>{{ item.thinking }}</pre></details>
        <div class="stream-text">{{ item.text }}</div>
      </article>
      <article v-for="item in conversation.pending.value" :key="item.id" class="message user pending-input">
        <div class="message-label"><span>{{ item.delivery === 'steer' ? '已插话 · 等待当前步骤结束' : '已排队 · 当前回答结束后处理' }}</span><button v-if="!compact && item.delivery === 'queue' && conversation.running.value" class="text-button" @click="emit('steer', item.id)">立即插话</button></div><div class="stream-text">{{ item.text }}</div>
      </article>
      <p v-if="conversation.running.value" class="working"><span class="live-dot" /> {{ conversation.approvals.value.length ? '等待你的确认' : '正在执行' }}</p>
    </div>
  </section>
</template>
