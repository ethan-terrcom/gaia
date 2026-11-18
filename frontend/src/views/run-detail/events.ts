// Timeline helpers and one-line summaries (doc 17c).

export type TimelineEvent = {
  seq: number
  ts: string
  type: string
  payload: Record<string, unknown>
}

export const BASE_ROW_HEIGHT = 36
export const TALL_ROW_HEIGHT = 72

const WRITE_LIKE_TOOL =
  /^(write_file|edit_file|apply_patch|git_commit|commit|patch|create_file|delete_file|rename_file)/i

export function isWriteLikeTool(tool: string): boolean {
  return WRITE_LIKE_TOOL.test(tool) || /(?:write|edit|commit|patch)/i.test(tool)
}

export function truncatedPaths(payload: Record<string, unknown>): string[] {
  const raw = payload._truncated
  if (!Array.isArray(raw)) {
    return []
  }
  return raw.filter((item): item is string => typeof item === 'string')
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

function numberValue(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value
  }
  return null
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

export function messageContent(payload: Record<string, unknown>): string {
  return stringValue(payload.content) ?? stringValue(payload.message) ?? '—'
}

export function toolNameFromEvent(event: TimelineEvent): string | null {
  return stringValue(event.payload.tool)
}

export function timelineRowHeight(event: TimelineEvent): number {
  switch (event.type) {
    case 'agent.message':
    case 'agent.tool_call':
    case 'agent.tool_result':
    case 'agent.usage':
      return TALL_ROW_HEIGHT
    default:
      return BASE_ROW_HEIGHT
  }
}

export function summarizeEvent(event: TimelineEvent): string {
  const payload = event.payload
  const truncated = truncatedPaths(payload)
  const truncatedNote =
    truncated.length > 0
      ? ` [truncated: ${truncated.slice(0, 3).join(', ')}]`
      : ''

  switch (event.type) {
    case 'agent.message':
      return messageContent(payload) + truncatedNote
    case 'agent.tool_call': {
      const tool = stringValue(payload.tool) ?? 'unknown tool'
      const args = summarizeArgs(payload.args)
      const base = args ? `${tool} ${args}` : tool
      return base + truncatedNote
    }
    case 'agent.tool_result': {
      let base: string
      if (payload.ok === true) {
        base =
          payload.decision === 'auto_approved'
            ? 'ok (auto-approved)'
            : (stringValue(payload.content) ?? 'ok')
      } else {
        const decision = stringValue(payload.decision)
        base = decision ? `decision: ${decision}` : 'not ok'
      }
      return base + truncatedNote
    }
    case 'agent.usage': {
      const parts: string[] = []
      const model = stringValue(payload.model)
      const provider = stringValue(payload.provider)
      if (model) {
        parts.push(provider ? `${provider}/${model}` : model)
      }
      const input = numberValue(payload.input_tokens)
      const output = numberValue(payload.output_tokens)
      if (input !== null || output !== null) {
        parts.push(`${input ?? 0} in · ${output ?? 0} out`)
      }
      const cache = numberValue(payload.cache_tokens)
      if (cache !== null && cache > 0) {
        parts.push(`${cache} cache`)
      }
      const cost = stringValue(payload.cost_usd)
      if (cost) {
        parts.push(`$${cost}`)
      }
      const turn = numberValue(payload.turn)
      if (turn !== null) {
        parts.push(`turn ${turn}`)
      }
      return (parts.length ? parts.join(' · ') : '') + truncatedNote
    }
    case 'run.status_changed':
      return `status: ${stringValue(payload.status) ?? 'unknown'}`
    case 'run.node_started':
    case 'run.node_finished':
      return stringValue(payload.node) ?? ''
    case 'run.failed': {
      const node = stringValue(payload.node) ?? ''
      const error = stringValue(payload.error)
      return error
        ? `${node}: ${error.trim().replace(/\s*\n\s*/g, ' · ')}`
        : node
    }
    case 'approval.requested':
      return stringValue(payload.kind) ?? ''
    case 'approval.decided':
      return stringValue(payload.action) ?? ''
    default:
      return truncatedNote.trimStart()
  }
}

export type DiffFileSection = {
  id: string
  title: string
  lines: string[]
}

export function parseUnifiedDiff(diff: string): DiffFileSection[] {
  const sections: DiffFileSection[] = []
  let current: DiffFileSection | null = null
  let index = 0

  for (const line of diff.split('\n')) {
    if (line === '[diff truncated]') {
      continue
    }
    if (line.startsWith('diff --git ')) {
      if (current) {
        sections.push(current)
      }
      const match = /^diff --git a\/(.+?) b\/(.+)$/.exec(line)
      const title = match ? (match[2] ?? match[1] ?? line) : line
      current = { id: `diff-${index++}`, title, lines: [line] }
      continue
    }
    if (current) {
      current.lines.push(line)
    } else if (line.trim() !== '') {
      current = { id: `diff-${index++}`, title: 'Changes', lines: [line] }
    }
  }
  if (current) {
    sections.push(current)
  }
  return sections
}
