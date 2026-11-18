import type { AgentModeDefault, ModelCatalog } from './agentModes'

/** A small catalog for tests: one provider configured per kind of case. */
export const TEST_CATALOG: ModelCatalog = {
  providers: [
    { name: 'anthropic', configured: true, secret_name: 'ANTHROPIC_API_KEY' },
    { name: 'deepseek', configured: true, secret_name: 'DEEPSEEK_API_KEY' },
    { name: 'openai', configured: false, secret_name: 'OPENAI_API_KEY' },
    { name: 'xai', configured: false, secret_name: 'XAI_API_KEY' },
  ],
  models: [
    {
      id: 'claude-opus-5-5',
      provider: 'anthropic',
      label: 'Claude Opus 5.5',
      tier: 'frontier',
      available: true,
      input_usd_per_mtok: '4.0',
      output_usd_per_mtok: '20.0',
      cache_read_usd_per_mtok: '0.20',
    },
    {
      id: 'deepseek-v4-pro',
      provider: 'deepseek',
      label: 'DeepSeek V4 Pro',
      tier: 'mid',
      available: true,
      input_usd_per_mtok: '1.32',
      output_usd_per_mtok: '3.96',
      cache_read_usd_per_mtok: '0.044',
    },
    {
      id: 'deepseek-flash',
      provider: 'deepseek',
      label: 'DeepSeek Flash',
      tier: null,
      available: true,
      input_usd_per_mtok: '0.30',
      output_usd_per_mtok: '1.20',
      cache_read_usd_per_mtok: '0.006',
    },
    {
      id: 'gpt-6.2-sol',
      provider: 'openai',
      label: 'GPT-6.2 Sol',
      tier: 'mid',
      available: false,
      input_usd_per_mtok: '2.0',
      output_usd_per_mtok: '10.0',
      cache_read_usd_per_mtok: '0.10',
    },
    {
      id: 'grok-4.7',
      provider: 'xai',
      label: 'Grok 4.7',
      tier: 'frontier',
      available: false,
      input_usd_per_mtok: '2.0',
      output_usd_per_mtok: '6.0',
      cache_read_usd_per_mtok: '0.50',
    },
  ],
}

export const TEST_DEFAULT: AgentModeDefault = {
  config: {
    mode: 'tiered',
    lead_model: 'claude-opus-5-5',
    worker_models: ['deepseek-v4-pro'],
    panel_models: [],
  },
  saved: false,
}
