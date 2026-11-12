import { computed, ref, watch, type Ref } from 'vue'
import type { Approval, ChecklistItem, ConversationPage, ConversationTurn, ExecutionEvent, Goal, HistoryRow, LiveRun } from './contracts'
import type { WorkbenchClient } from './client'

export function useConversation(client: Ref<WorkbenchClient | null>, sid: Ref<string>) {
  const turns = ref<Map<string, ConversationTurn>>(new Map())
  const live = ref<Record<string, LiveRun>>({}), approvals = ref<Approval[]>([])
  const checklist = ref<ChecklistItem[]>([]), goal = ref<Goal | null>(null)
  const pending = ref<{ id: string; text: string; source: string; delivery: 'queue' | 'steer' }[]>([])
  const connection = ref('未连接'), error = ref(''), hasMore = ref(false), before = ref<string | null>(null)
  const loadingOlder = ref(false)
  let generation = 0
  const running = computed(() => Object.keys(live.value).length > 0)
  const activeRunId = computed(() => Object.keys(live.value)[0] ?? '')
  const rows = computed(() => {
    const seen = new Set<string>()
    return [...turns.value.values()].sort((a, b) =>
      (a.run.metadata.started_at ?? 0) - (b.run.metadata.started_at ?? 0) || a.run.run_id.localeCompare(b.run.run_id)
    ).flatMap(turn => turn.items.filter(item => {
      if (seen.has(item.row_id)) return false
      seen.add(item.row_id); return true
    }).map(item => ({ ...item, run_id: turn.run.run_id })))
  })
  const partials = computed(() => Object.entries(live.value).flatMap(([runId, run]) =>
    Object.values(run.items).map(item => ({ ...item, run_id: runId }))))
  const issues = computed(() => [...turns.value.values()].flatMap(turn => turn.issues))

  function ensureTurn(id: string, agent = ''): ConversationTurn {
    let turn = turns.value.get(id)
    if (!turn) {
      turn = { run: { run_id: id, session_id: sid.value, agent_name: agent, status: 'running', label: '', metadata: { started_at: Date.now() / 1000 } },
        items: [], history_complete: true, issues: [] }
      turns.value.set(id, turn)
      turn = turns.value.get(id)!
    }
    return turn
  }

  function merge(page: ConversationPage, replace = false) {
    if (replace) turns.value.clear()
    for (const turn of page.turns) turns.value.set(turn.run.run_id, turn)
    hasMore.value = page.has_more; before.value = page.next_before
    if (replace) {
      live.value = page.live
      approvals.value = page.approvals ?? []
      checklist.value = page.checklist ?? []
      goal.value = page.goal ?? null
      pending.value = (page.pending_inputs ?? []).filter(item => !['background', 'goal'].includes(item.source))
    }
  }

  function apply(event: ExecutionEvent) {
    const p = event.payload, runId = event.run_id
    switch (event.kind) {
      case 'input_accepted':
        if (!p.duplicate && !['background', 'goal'].includes(String(p.source))) {
          pending.value = [...pending.value.filter(item => item.id !== p.input_id),
            { id: p.input_id as string, text: p.user_input as string, source: p.source as string, delivery: p.delivery as 'queue' | 'steer' }]
        }
        break
      case 'input_promoted':
        pending.value = pending.value.map(item => item.id === p.input_id ? { ...item, delivery: 'steer' } : item)
        break
      case 'turn_started':
        live.value[runId] = { agent_name: p.agent_name as string, items: {} }
        ensureTurn(runId, p.agent_name as string)
        break
      case 'model_delta': {
        const run = live.value[runId] ??= { items: {} }
        const id = p.item_id as string
        const item = run.items[id] ??= { row_id: id, text: '', thinking: '' }
        item[p.channel === 'thinking' ? 'thinking' : 'text'] += p.text as string
        break
      }
      case 'row_committed': {
        const row = p as unknown as HistoryRow, turn = ensureTurn(runId, row.agent_name)
        const index = turn.items.findIndex(item => item.row_id === row.row_id)
        if (index === -1) turn.items.push(row); else turn.items[index] = row
        if (live.value[runId]) delete live.value[runId].items[row.row_id]
        pending.value = pending.value.filter(item => item.id !== row.payload.metadata.input_id)
        break
      }
      case 'run_closed': delete live.value[runId]; break
      case 'final': ensureTurn(runId).run.status = 'completed'; break
      case 'cancelled': ensureTurn(runId).run.status = 'cancelled'; break
      case 'error': error.value = String(p.error ?? '执行失败'); ensureTurn(runId).run.status = 'failed'; break
      case 'checklist_changed': checklist.value = p.items as ChecklistItem[]; break
      case 'goal_changed': goal.value = p.goal as Goal | null; break
      case 'approval_requested': {
        const approval = p as unknown as Approval
        approvals.value = [...approvals.value.filter(item => item.id !== approval.id), approval]
        break
      }
      case 'approval_resolved': approvals.value = approvals.value.filter(item => item.id !== p.id); break
    }
  }

  async function loadOlder() {
    if (!client.value || !before.value || loadingOlder.value) return
    const current = generation
    loadingOlder.value = true
    try {
      const page = await client.value.request<ConversationPage>(`/sessions/${encodeURIComponent(sid.value)}/conversation?before=${encodeURIComponent(before.value)}`)
      if (current === generation) merge(page)
    }
    catch (failure) { if (current === generation) error.value = String(failure) }
    finally { if (current === generation) loadingOlder.value = false }
  }

  watch([client, sid], ([api, session], _, cleanup) => {
    generation++
    const abort = new AbortController()
    cleanup(() => abort.abort())
    turns.value.clear(); live.value = {}; approvals.value = []; checklist.value = []; goal.value = null; pending.value = []
    loadingOlder.value = false
    connection.value = '未连接'; error.value = ''; hasMore.value = false; before.value = null
    if (!api || !session) return
    void (async () => {
      let cursor = '', delay = 500
      while (!abort.signal.aborted) {
        try {
          connection.value = cursor ? '重连中' : '连接中'
          for await (const frame of api.stream(session, cursor, abort.signal)) {
            if (abort.signal.aborted) return
            if (connection.value !== '已连接') error.value = ''
            if (frame.event === 'snapshot') merge(frame.data as ConversationPage, true)
            else if (frame.event === 'execution') apply(frame.data as ExecutionEvent)
            cursor = frame.id || cursor
            connection.value = '已连接'; delay = 500
          }
        } catch (failure) {
          if (abort.signal.aborted) return
          connection.value = '重连中'; error.value = String(failure)
        }
        await new Promise<void>(resolve => {
          const wake = () => { clearTimeout(timer); abort.signal.removeEventListener('abort', wake); resolve() }
          const timer = setTimeout(wake, delay)
          abort.signal.addEventListener('abort', wake, { once: true })
        })
        delay = Math.min(delay * 2, 10_000)
      }
    })()
  }, { immediate: true })

  return { rows, partials, pending, turns, running, activeRunId, approvals, checklist, goal, connection, error, issues, hasMore, loadingOlder, loadOlder }
}

export type ConversationState = ReturnType<typeof useConversation>
