import { describe, expect, it } from 'vitest'
import {
  configModels,
  configProblem,
  describeConfig,
  missingKeys,
  modeLabel,
  modelLabel,
  normalizeConfig,
} from './agentModes'
import { TEST_CATALOG } from './testData'

describe('agent mode helpers', () => {
  it('describes each mode in one line', () => {
    expect(
      describeConfig(
        { mode: 'single', lead_model: 'gpt-6.2-sol' },
        TEST_CATALOG.models,
      ),
    ).toBe('Straight shot: GPT-6.2 Sol')
    expect(
      describeConfig(
        {
          mode: 'crowd',
          lead_model: 'claude-opus-5-5',
          worker_models: ['deepseek-v4-pro'],
          panel_models: ['grok-4.7', 'unknown-model'],
        },
        undefined,
      ),
    ).toBe(
      'Wisdom of crowds: panel grok-4.7, unknown-model; claude-opus-5-5 → deepseek-v4-pro',
    )
  })

  it('normalizes a config to the fields its mode uses', () => {
    expect(
      normalizeConfig({
        mode: 'single',
        lead_model: 'm',
        worker_models: ['w'],
      }),
    ).toEqual({ mode: 'single', lead_model: 'm' })
    expect(
      normalizeConfig({ mode: 'tiered', lead_model: 'm', panel_models: ['p'] }),
    ).toEqual({
      mode: 'tiered',
      lead_model: 'm',
      worker_models: [],
      panel_models: [],
    })
  })

  it('says why a config cannot be saved', () => {
    expect(configProblem({ mode: 'single', lead_model: '' })).toBe(
      'Pick a model.',
    )
    expect(configProblem({ mode: 'single', lead_model: 'm' })).toBeNull()
    expect(configProblem({ mode: 'tiered', lead_model: 'm' })).toBe(
      'Pick 1 to 4 workers.',
    )
    expect(
      configProblem({
        mode: 'crowd',
        lead_model: 'm',
        worker_models: ['w'],
        panel_models: ['a', 'b'],
      }),
    ).toBeNull()
  })

  it('lists the keys a config still needs, once each', () => {
    const config = {
      mode: 'crowd' as const,
      lead_model: 'gpt-6.2-sol',
      worker_models: ['deepseek-v4-pro'],
      panel_models: ['grok-4.7', 'gpt-6.2-sol', 'not-in-catalog'],
    }

    expect(configModels(config)).toEqual([
      'gpt-6.2-sol',
      'deepseek-v4-pro',
      'grok-4.7',
      'not-in-catalog',
    ])
    expect(missingKeys(config, TEST_CATALOG)).toEqual([
      'OPENAI_API_KEY',
      'XAI_API_KEY',
    ])
    expect(missingKeys(config, undefined)).toEqual([])
  })

  it('falls back to ids for unknown modes and models', () => {
    expect(modeLabel('tiered')).toBe('Two tiered')
    expect(modeLabel('other' as never)).toBe('other')
    expect(modelLabel('x', TEST_CATALOG.models)).toBe('x')
  })
})
