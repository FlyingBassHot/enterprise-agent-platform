import { onBeforeUnmount, ref } from 'vue'

export interface ExecutionEventDto {
  seq: number
  execution_id: string
  type: string
  payload: Record<string, unknown>
  created_at?: string
}

export interface StreamOptions {
  onEvent?: (event: ExecutionEventDto) => void
  onClose?: (reason: string) => void
}

const TERMINAL_EVENTS: Record<string, true> = {
  execution_completed: true,
  execution_failed: true,
  execution_cancelled: true,
}

/**
 * SSE over fetch: EventSource cannot send the Authorization header.
 * Reconnects with the last seen sequence, so missed events are replayed.
 */
export function useExecutionStream(options: StreamOptions = {}) {
  const events = ref<ExecutionEventDto[]>([])
  const connected = ref(false)
  const error = ref('')
  let controller: AbortController | null = null
  let stopped = false
  let lastSeq = 0
  let executionId = ''

  async function connect(afterSeq = lastSeq) {
    controller = new AbortController()
    const token = localStorage.getItem('access_token') || ''
    try {
      const response = await fetch(
        `/api/v1/executions/${executionId}/stream?after=${afterSeq}`,
        {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          signal: controller.signal,
        },
      )
      if (!response.ok || !response.body) {
        throw new Error(`stream failed: HTTP ${response.status}`)
      }
      connected.value = true
      error.value = ''
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      while (!stopped) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() || ''
        for (const block of blocks) {
          const parsed = parseEventBlock(block)
          if (!parsed) continue
          if (parsed.seq <= lastSeq) continue
          lastSeq = parsed.seq
          events.value.push(parsed)
          options.onEvent?.(parsed)
          if (TERMINAL_EVENTS[parsed.type]) {
            connected.value = false
            options.onClose?.(parsed.type)
            return
          }
        }
      }
      connected.value = false
    } catch (err) {
      connected.value = false
      if (stopped) return
      error.value = err instanceof Error ? err.message : String(err)
      window.setTimeout(() => {
        if (!stopped) void connect(lastSeq)
      }, 1500)
    }
  }

  function start(id: string, afterSeq = 0) {
    executionId = id
    lastSeq = afterSeq
    stopped = false
    events.value = []
    void connect(afterSeq)
  }

  function stop() {
    stopped = true
    connected.value = false
    controller?.abort()
  }

  onBeforeUnmount(stop)

  return { events, connected, error, start, stop }
}

function parseEventBlock(block: string): ExecutionEventDto | null {
  let seq = 0
  let type = 'message'
  const dataLines: string[] = []
  for (const line of block.split('\n')) {
    if (line.startsWith('id:')) seq = Number(line.slice(3).trim())
    else if (line.startsWith('event:')) type = line.slice(6).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim())
    else if (line.startsWith(':')) continue
  }
  if (!dataLines.length) return null
  try {
    const data = JSON.parse(dataLines.join('\n'))
    return {
      seq: seq || Number(data.seq) || 0,
      execution_id: data.execution_id || '',
      type: data.type || type,
      payload: data.payload || data,
      created_at: data.created_at,
    }
  } catch {
    return null
  }
}
