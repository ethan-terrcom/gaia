import { api } from '../../api/client'
import type { components } from '../../api/schema'

export type AgentMode = components['schemas']['AgentMode']
export type AgentModeConfig = components['schemas']['AgentModeConfig']
export type AgentModeDefault = components['schemas']['AgentModeDefault']
export type ModelCatalog = components['schemas']['ModelCatalog']
export type CatalogModel = components['schemas']['CatalogModelRead']
export type Provider = components['schemas']['LLMProvider']

export const MODELS_QUERY_KEY = ['models'] as const
export const AGENT_MODE_QUERY_KEY = ['admin', 'agent-mode'] as const

/** Pool limits, matching ``gaia.domain.agent_modes`` (doc 19b). */
export const MAX_WORKERS = 4
export const MIN_PANEL = 2
export const MAX_PANEL = 5

export const PROVIDER_LABELS: Record<Provider, string> = {
  anthropic: 'Anthropic',
  deepseek: 'DeepSeek',
  openai: 'OpenAI',
  xai: 'xAI',
}

export const MODES: readonly {
  mode: AgentMode
  label: string
  detail: string
}[] = [
  {
    mode: 'single',
    label: 'Straight shot',
    detail:
      'One model does the whole ticket in one session. No plan to approve; it reviews its own work.',
  },
  {
    mode: 'tiered',
    label: 'Two tiered',
    detail:
      'A lead plans, gives each step to one of your workers, reviews every step, and takes over a step that keeps failing.',
  },
  {
    mode: 'crowd',
    label: 'Wisdom of crowds',
    detail:
      'Several models each write a plan; the lead merges the best parts. You see every plan at the approval. Then it runs as two tiered.',
  },
]

export function modeLabel(mode: AgentMode): string {
  return MODES.find((entry) => entry.mode === mode)?.label ?? mode
}

export async function fetchModels(): Promise<ModelCatalog> {
  // Pass the current global `fetch` so tests can stub it after import.
  const response = await api.GET('/api/v1/models', { fetch })
  if (response.data === undefined) {
    throw new Error('empty models response')
  }
  return response.data
}

export async function fetchAgentMode(): Promise<AgentModeDefault> {
  const response = await api.GET('/api/v1/admin/agent-mode', { fetch })
  if (response.data === undefined) {
    throw new Error('empty agent-mode response')
  }
  return response.data
}

export async function saveAgentMode(
  config: AgentModeConfig,
): Promise<AgentModeDefault> {
  const response = await api.PUT('/api/v1/admin/agent-mode', {
    body: config,
    fetch,
  })
  if (response.error) {
    throw response.error
  }
  return response.data
}

/** The config with only the fields its mode uses, so the API accepts it. */
export function normalizeConfig(config: AgentModeConfig): AgentModeConfig {
  if (config.mode === 'single') {
    return { mode: 'single', lead_model: config.lead_model }
  }
  return {
    mode: config.mode,
    lead_model: config.lead_model,
    worker_models: config.worker_models ?? [],
    panel_models: config.mode === 'crowd' ? (config.panel_models ?? []) : [],
  }
}

/** Why ``config`` cannot be saved yet, or ``null`` when it can. */
export function configProblem(config: AgentModeConfig): string | null {
  if (!config.lead_model) {
    return config.mode === 'single' ? 'Pick a model.' : 'Pick a lead model.'
  }
  if (config.mode === 'single') {
    return null
  }
  const workers = config.worker_models ?? []
  if (workers.length < 1 || workers.length > MAX_WORKERS) {
    return `Pick 1 to ${MAX_WORKERS} workers.`
  }
  if (config.mode === 'crowd') {
    const panel = config.panel_models ?? []
    if (panel.length < MIN_PANEL || panel.length > MAX_PANEL) {
      return `Pick ${MIN_PANEL} to ${MAX_PANEL} panel models.`
    }
  }
  return null
}

/** Every model a config names, each once, lead first. */
export function configModels(config: AgentModeConfig): string[] {
  const seen = new Set<string>([config.lead_model])
  for (const model of [
    ...(config.worker_models ?? []),
    ...(config.panel_models ?? []),
  ]) {
    seen.add(model)
  }
  return [...seen].filter((model) => model !== '')
}

export function modelLabel(
  id: string,
  models: readonly CatalogModel[] | undefined,
): string {
  return models?.find((model) => model.id === id)?.label ?? id
}

/** One line for a config, such as "Two tiered: Claude Opus 5.5 → DeepSeek V4 Pro". */
export function describeConfig(
  config: AgentModeConfig,
  models: readonly CatalogModel[] | undefined,
): string {
  const label = (id: string) => modelLabel(id, models)
  const lead = label(config.lead_model)
  const workers = (config.worker_models ?? []).map(label).join(', ')
  if (config.mode === 'single') {
    return `${modeLabel('single')}: ${lead}`
  }
  if (config.mode === 'tiered') {
    return `${modeLabel('tiered')}: ${lead} → ${workers}`
  }
  const panel = (config.panel_models ?? []).map(label).join(', ')
  return `${modeLabel('crowd')}: panel ${panel}; ${lead} → ${workers}`
}

/** The ``gaia secrets set`` entries a config still needs. */
export function missingKeys(
  config: AgentModeConfig,
  catalog: ModelCatalog | undefined,
): string[] {
  if (!catalog) {
    return []
  }
  const providers = new Set(
    configModels(config)
      .map((id) => catalog.models.find((model) => model.id === id)?.provider)
      .filter((provider): provider is Provider => provider !== undefined),
  )
  return catalog.providers
    .filter((status) => providers.has(status.name) && !status.configured)
    .map((status) => status.secret_name)
}
