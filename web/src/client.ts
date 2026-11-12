import type { StreamFrame } from './contracts'

export class WorkbenchClient {
  constructor(private readonly token: string) {}

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(path, { ...init, headers: {
      Authorization: `Bearer ${this.token}`, 'Content-Type': 'application/json', ...init.headers,
    } })
    if (!response.ok) throw new Error(response.status === 401 ? '访问令牌无效，请重新连接。' : await response.text())
    return await response.json() as T
  }

  post<T>(path: string, data: unknown = {}): Promise<T> {
    return this.request<T>(path, { method: 'POST', body: JSON.stringify(data) })
  }

  async upload<T>(path: string, file: File): Promise<T> {
    const response = await fetch(path, { method: 'POST', headers: { Authorization: `Bearer ${this.token}` }, body: file })
    if (!response.ok) throw new Error(await response.text())
    return await response.json() as T
  }

  async download(path: string, filename: string): Promise<void> {
    const response = await fetch(path, { headers: { Authorization: `Bearer ${this.token}` } })
    if (!response.ok) throw new Error(await response.text())
    const url = URL.createObjectURL(await response.blob()), link = document.createElement('a')
    link.href = url; link.download = filename; link.click()
    setTimeout(() => URL.revokeObjectURL(url), 10_000)
  }

  async *stream(sid: string, cursor: string, signal: AbortSignal): AsyncGenerator<StreamFrame> {
    const response = await fetch(`/sessions/${encodeURIComponent(sid)}/events`, {
      headers: { Authorization: `Bearer ${this.token}`, ...(cursor ? { 'Last-Event-ID': cursor } : {}) }, signal,
    })
    if (!response.ok || !response.body) throw new Error(`连接失败 (${response.status})`)
    const reader = response.body.getReader(), decoder = new TextDecoder()
    let pending = ''
    try {
      while (true) {
        const { value, done } = await reader.read()
        if (done) break
        pending = (pending + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')
        let end: number
        while ((end = pending.indexOf('\n\n')) !== -1) {
          const block = pending.slice(0, end)
          pending = pending.slice(end + 2)
          const fields: Record<string, string[]> = {}
          for (const line of block.split('\n')) {
            if (line.startsWith(':')) continue
            const colon = line.indexOf(':')
            if (colon !== -1) (fields[line.slice(0, colon)] ??= []).push(line.slice(colon + 1).replace(/^ /, ''))
          }
          if (fields.data) yield { id: fields.id?.[0] ?? '', event: fields.event?.[0] ?? 'message', data: JSON.parse(fields.data.join('\n')) }
        }
      }
    } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
  }
}
