// One-line summaries for the run timeline (doc 17c).

export type TimelineEvent = {
  seq: number
  ts: string
  type: string
  payload: Record<string, unknown>
}

function stringValue(value: unknown): string | null {
  if (typeof value === 'string') {
    return value
  }
  if (value === null || value === undefined) {
    return null
  }
  return String(value)
}

function truncate(value: string, max: number): string {
  return value.length <= max ? value : `${value.slice(0, max)}…`
}

function summarizeArgs(args: unknown): string | null {
  if (args === null || args === undefined) {
    return null
  }
  if (typeof args === 'string') {
    return truncate(args, 160)
  }
  const json = JSON.stringify(args)
  return json === undefined ? null : truncate(json, 160)
}

export function summarizeEvent(event: TimelineEvent): string {
  const payload = event.payload
  switch (event.type) {
    case 'agent.message':
      // The runtimes use `content`; the task-start message uses `message`.
      return stringValue(payload.content) ?? stringValue(payload.message) ?? '—'
    case 'agent.tool_call': {
      const tool = stringValue(payload.tool) ?? 'unknown tool'
      const args = summarizeArgs(payload.args)
      return args ? `${tool} ${args}` : tool
    }
    case 'agent.tool_result': {
      if (payload.ok === true) {
        return 'ok'
      }
      const decision = stringValue(payload.decision)
      return decision ? `decision: ${decision}` : 'not ok'
    }
    case 'run.status_changed':
      return `status: ${stringValue(payload.status) ?? 'unknown'}`
    case 'run.node_started':
    case 'run.node_finished':
    case 'run.failed':
      return stringValue(payload.node) ?? ''
    case 'approval.requested':
      return stringValue(payload.kind) ?? ''
    case 'approval.decided':
      return stringValue(payload.action) ?? ''
    default:
      // The type is already shown beside the summary; don't repeat it.
      return ''
  }
}
