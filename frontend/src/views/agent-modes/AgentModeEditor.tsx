import {
  MAX_PANEL,
  MAX_WORKERS,
  MODES,
  PROVIDER_LABELS,
  configProblem,
  missingKeys,
  type AgentMode,
  type AgentModeConfig,
  type CatalogModel,
  type ModelCatalog,
  type Provider,
} from './agentModes'

/** List prices, input / output in USD per 1M tokens ("$2 / $10"). */
function priceText(model: CatalogModel): string {
  const input = Number(model.input_usd_per_mtok)
  const output = Number(model.output_usd_per_mtok)
  return `$${input} / $${output}`
}

function optionText(model: CatalogModel): string {
  const parts = [model.label]
  if (model.tier) {
    parts.push(model.tier)
  }
  parts.push(priceText(model))
  const text = parts.join(' · ')
  return model.available ? text : `${text} (key not set)`
}

function byProvider(
  models: readonly CatalogModel[],
): [Provider, CatalogModel[]][] {
  const groups = new Map<Provider, CatalogModel[]>()
  for (const model of models) {
    groups.set(model.provider, [...(groups.get(model.provider) ?? []), model])
  }
  return [...groups.entries()]
}

function ModelSelect({
  id,
  label,
  value,
  models,
  onChange,
}: {
  id: string
  label: string
  value: string
  models: readonly CatalogModel[]
  onChange: (model: string) => void
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium">
        {label}
      </label>
      <select
        id={id}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="mt-1 w-full rounded border border-gray-300 px-2 py-1 dark:border-gray-700 dark:bg-gray-950"
      >
        <option value="">Choose a model…</option>
        {byProvider(models).map(([provider, group]) => (
          <optgroup key={provider} label={PROVIDER_LABELS[provider]}>
            {group.map((model) => (
              <option key={model.id} value={model.id}>
                {optionText(model)}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </div>
  )
}

function ModelChecklist({
  id,
  legend,
  hint,
  selected,
  max,
  models,
  onChange,
}: {
  id: string
  legend: string
  hint: string
  selected: readonly string[]
  max: number
  models: readonly CatalogModel[]
  onChange: (models: string[]) => void
}) {
  const full = selected.length >= max
  function toggle(model: string): void {
    onChange(
      selected.includes(model)
        ? selected.filter((entry) => entry !== model)
        : [...selected, model],
    )
  }
  return (
    <fieldset>
      <legend className="text-sm font-medium">{legend}</legend>
      <p className="text-xs text-gray-600 dark:text-gray-400">{hint}</p>
      <div className="mt-1 grid gap-2 sm:grid-cols-2">
        {byProvider(models).map(([provider, group]) => (
          <div key={provider}>
            <p className="text-xs font-semibold text-gray-500 dark:text-gray-400">
              {PROVIDER_LABELS[provider]}
            </p>
            {group.map((model) => {
              const checked = selected.includes(model.id)
              const boxId = `${id}-${model.id}`
              return (
                <div key={model.id} className="flex items-center gap-2 text-sm">
                  <input
                    id={boxId}
                    type="checkbox"
                    checked={checked}
                    disabled={!checked && full}
                    onChange={() => toggle(model.id)}
                  />
                  <label htmlFor={boxId}>{optionText(model)}</label>
                </div>
              )
            })}
          </div>
        ))}
      </div>
    </fieldset>
  )
}

/** Pick an agent mode and the models for its roles (doc 19). Controlled. */
export function AgentModeEditor({
  idPrefix,
  value,
  catalog,
  onChange,
}: {
  idPrefix: string
  value: AgentModeConfig
  catalog: ModelCatalog
  onChange: (config: AgentModeConfig) => void
}) {
  const models = catalog.models
  const workers = value.worker_models ?? []
  const panel = value.panel_models ?? []
  const problem = configProblem(value)
  const keys = missingKeys(value, catalog)

  function setMode(mode: AgentMode): void {
    onChange({ ...value, mode })
  }

  return (
    <div className="grid gap-4">
      <fieldset>
        <legend className="text-sm font-medium">Mode</legend>
        <div className="mt-1 grid gap-2 sm:grid-cols-3">
          {MODES.map(({ mode, label, detail }) => {
            const checked = value.mode === mode
            return (
              <label
                key={mode}
                className={`cursor-pointer rounded border p-3 text-sm ${
                  checked
                    ? 'border-blue-600 bg-blue-50 dark:bg-blue-950'
                    : 'border-gray-200 dark:border-gray-800'
                }`}
              >
                <input
                  type="radio"
                  name={`${idPrefix}-mode`}
                  value={mode}
                  checked={checked}
                  onChange={() => setMode(mode)}
                  className="mr-2"
                />
                <span className="font-medium">{label}</span>
                <span className="mt-1 block text-gray-600 dark:text-gray-400">
                  {detail}
                </span>
              </label>
            )
          })}
        </div>
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-400">
          Prices are list prices: input / output, in USD per 1M tokens.
        </p>
      </fieldset>

      <ModelSelect
        id={`${idPrefix}-lead`}
        label={value.mode === 'single' ? 'Model' : 'Lead (plans and reviews)'}
        value={value.lead_model}
        models={models}
        onChange={(lead_model) => onChange({ ...value, lead_model })}
      />

      {value.mode === 'crowd' ? (
        <ModelChecklist
          id={`${idPrefix}-panel`}
          legend="Panel"
          hint={`2 to ${MAX_PANEL} models that each write a plan for the lead to merge. Mid-tier models are a good fit.`}
          selected={panel}
          max={MAX_PANEL}
          models={models}
          onChange={(panel_models) => onChange({ ...value, panel_models })}
        />
      ) : null}

      {value.mode === 'single' ? null : (
        <ModelChecklist
          id={`${idPrefix}-workers`}
          legend="Workers"
          hint={`1 to ${MAX_WORKERS} models that implement the steps. The lead gives each step to the cheapest one that can do it well.`}
          selected={workers}
          max={MAX_WORKERS}
          models={models}
          onChange={(worker_models) => onChange({ ...value, worker_models })}
        />
      )}

      {problem ? (
        <p className="text-sm text-amber-800 dark:text-amber-200">{problem}</p>
      ) : null}
      {keys.length > 0 ? (
        <p className="rounded border border-amber-300 bg-amber-50 p-2 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950 dark:text-amber-100">
          Runs with these models need keys that are not set yet:{' '}
          {keys.map((name, index) => (
            <span key={name}>
              {index > 0 ? ', ' : ''}
              <code>gaia secrets set {name}</code>
            </span>
          ))}
        </p>
      ) : null}
    </div>
  )
}
