export type PermissionMode = 'manual' | 'yolo' | 'auto'
export interface SessionView {
  session_id: string; agent_name: string; parent_session_id: string | null
  parent_run_id: string | null; running: boolean; title: string; updated_at: number
}
export interface ToolCall { tool_name: string; input: unknown; call_id: string; execution_id?: string }
export interface HistoryRow {
  row_id: string; session_id: string; agent_name: string; kind: string; ts: number
  payload: { role: string; content: string; media?: { kind: string; mime_type: string; size_bytes: number }[]; metadata: {
    tool_name?: string; tool_use_id?: string; tool_calls?: ToolCall[]; note_kind?: string
    reasoning_content?: string; ok?: boolean; [key: string]: unknown
  } }
  metadata: Record<string, unknown>
}
export interface RunView {
  run_id: string; session_id: string; agent_name: string; status: string
  label: string; metadata: { started_at?: number; finished_at?: number }
}
export interface ConversationTurn { run: RunView; items: HistoryRow[]; history_complete: boolean; issues: string[] }
export interface LiveItem { row_id: string; text: string; thinking: string }
export interface LiveRun { agent_name?: string; items: Record<string, LiveItem> }
export interface ChecklistItem { content: string; active_form: string; status: 'pending' | 'in_progress' | 'completed' }
export interface Approval {
  id: string; session_id: string; owner_session_id: string; run_id: string
  call_id: string; tool_name: string; arguments: unknown; cwd: string; status: string
}
export interface Goal {
  goal_id: string; objective: string; completion_criterion: string
  status: 'active' | 'paused' | 'blocked' | 'complete'; token_budget: number | null
  tokens_used: number; steps_used: number; elapsed_ms: number; reason: string
  remaining_tokens: number | null; completion_requested: boolean
}
export interface BackgroundTask {
  id: string; kind: string; label: string; status: string; started: number; parent_run: string
  rendered: string; content: { agent_id?: string; agent?: string }; progress: string[]
}
export interface ConversationPage {
  session_id: string; cursor: string; turns: ConversationTurn[]; has_more: boolean; next_before: string | null
  live: Record<string, LiveRun>; checklist?: ChecklistItem[]; approvals?: Approval[]; goal?: Goal | null
  pending_inputs?: { id: string; text: string; source: string; delivery: 'queue' | 'steer' }[]
}
export interface ExecutionEvent { kind: string; session_id: string; run_id: string; payload: Record<string, unknown> }
export interface StreamFrame { id: string; event: string; data: unknown }
export interface ProcessLogPage {
  records: { stream: string; text: string }[]; start_offset: number; next_offset: number; has_more: boolean; complete: boolean; issue: string
}
