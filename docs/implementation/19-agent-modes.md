# 19 — Agent modes and multi-provider models

**Asked for by the owner (2026-11-17):** three agentic modes the user can switch between, with
the specific model for every role picked from any provider. Grok (xAI) and OpenAI keys are coming
and should be planned for. "Start with the core functionality first"; a broader UI redesign
follows later.

Built by the lead directly, in three packages, each merged before the next is cut:

| Package | Branch | Scope |
|---|---|---|
| **19a** | `impl/19a-providers` | OpenAI and xAI as providers (keys, gateway routes, one OpenAI-compatible client), the model catalog in `prices.toml`, `GET /models`, auto-mode switches for the new families |
| **19b** | `impl/19b-agent-modes` | The three modes: a saved default plus a per-run snapshot, per-task model routing, straight shot, two tiered with a worker pool, wisdom of crowds |
| **19c** | `impl/19c-agent-modes-ui` | Settings panel for the mode and models, the run's mode on the run page, crowd candidates at the plan gate |

## The three modes

| Mode | Who does what |
|---|---|
| **Straight shot** (`single`) | One model does the whole ticket in **one agent session**. There is no plan and no plan gate: starting the run is the go-ahead. Sandboxed checks run, and the same model reviews its own diff (a "revise" verdict loops back to it, up to the retry limit). The PR and merge gates are unchanged. |
| **Two tiered** (`tiered`) | A frontier **lead** plans, assigns each step to a **worker** from a pool the owner configures, reviews every step, and takes a failing step over itself from attempt `lead_attempt_from` (3). Steps run **sequentially** in the run's one worktree. |
| **Wisdom of crowds** (`crowd`) | The lead sends the ticket to several mid-tier **panelists**, which write full plans in parallel. The lead sees them labelled Plan A, B, C and writes one merged plan that says what it took from each. The candidates are shown at the plan gate. From then on the run is two tiered. |

## Owner decisions (2026-11-17)

All four were the lead's recommendations, accepted as asked:

| # | Question | Decision |
|---|---|---|
| M1 | What is straight shot? | One model, one session for the whole ticket, no plan gate, self-review, PR and merge gates stay |
| M2 | How does the lead delegate? | Sequentially, assigning each step to a worker from a pool. Parallel subagents in separate worktrees are later work |
| M3 | Where is the mode chosen? | A default in Settings, overridable when a run starts. Each run saves the config it started with, so a resume after a restart uses the same models |
| M4 | How is the crowd used? | To merge plans only. Steps are not reviewed by the crowd |

## 19a — providers and the model catalog

### What it adds
- **Providers** are an enum, `LLMProvider`: `anthropic`, `deepseek`, `openai`, `xai`. The value
  is the gateway route (`/llm/<provider>/…`), the run token's provider and `llm_usage.provider`.
- **Keys:** `OPENAI_API_KEY` and `XAI_API_KEY` join the keychain names (`gaia secrets set …`).
  `PROVIDER_SECRETS` maps each provider to its entry; only the gateway reads them.
- **Gateway:** `/llm/openai/chat/completions` → `https://api.openai.com/v1/chat/completions`
  and `/llm/xai/chat/completions` → `https://api.x.ai/v1/chat/completions` (base URLs are the
  settings `openai_base_url` and `xai_base_url`). Only chat completions is forwarded; the
  Responses API is not. Streams get `stream_options.include_usage` like DeepSeek's.
- **One OpenAI-compatible client.** `gaia/llm/deepseek_client.py` became
  `gaia/llm/openai_compat_client.py`, still the only module that imports `openai` (ADR-001 P1).
  `OpenAICompatClient(provider=…)` serves DeepSeek, OpenAI and xAI; `DeepSeekClient` is its
  DeepSeek subclass. The dialects differ in three places only:

  | | DeepSeek | OpenAI | xAI |
  |---|---|---|---|
  | Output cap field | `max_tokens` | `max_completion_tokens` (reasoning models reject `max_tokens`) | `max_completion_tokens` |
  | `reasoning_content` echoed on assistant turns | yes (thinking-mode tool loops need it) | no | no |
  | Cached prompt tokens | `prompt_cache_hit_tokens` / `…_miss_tokens` | `prompt_tokens_details.cached_tokens` | `prompt_tokens_details.cached_tokens` |
  | Reasoning tokens | inside `completion_tokens` | inside `completion_tokens` | **outside**: `total_tokens` > prompt + completion |

  `openai_compat_buckets` reads all of them. Output is `max(completion_tokens, total_tokens −
  prompt_tokens)`, which is right for every convention and never under-counts.
- **The model catalog is `prices.toml`.** Each model now names its `provider`, a `label` and a
  `tier` (`frontier`, `mid`, `fast`: a hint for the UI, never a rule). A model can be used only
  when it is priced; the gateway now also refuses a model on another provider's route (an
  OpenAI model sent to `/llm/xai/…` is a 400), so a call is always metered at the right prices.
- **Long-context pricing:** `[models.<id>.long_context]` with `from_prompt_tokens` and four
  rates. Once a request's prompt (uncached input + cache reads + cache writes) reaches the
  threshold, the whole request is billed at those rates, with `price_tier = "long-context"`.
  OpenAI (over 272K), xAI (200K and up) and Claude Haiku 5.5 (over 100K) bill this way.
- **`GET /api/v1/models`:** every provider with whether its key is set (`configured`, plus the
  keychain entry name to set), and every catalog model with its label, tier, list prices and
  `available` (its provider's key is set; always true in demo mode). Key values never leave
  the keychain.
- **Auto mode** gains `openai` and `xai` switches. `family_for_provider` maps a provider to its
  family. The native runtime follows the switch of its model's provider, not always DeepSeek's.
- **The native runtime can run any OpenAI-compatible model:** `gateway_llm_factory(provider=…)`.
  Wiring takes the junior model's provider from the catalog, so `GAIA_JUNIOR_MODEL=gpt-6-luna`
  works once the key is set. Anthropic models stay on the Claude Agent SDK runtime: the native
  loop sends its system prompt as a chat message, which the Anthropic client does not take.

### Models shipped (official pages read 2026-11-17)
| Provider | Frontier | Mid | Fast |
|---|---|---|---|
| Anthropic | `claude-fable-5-2` ($8/$40), `claude-opus-5-5` ($4/$20) | `claude-sonnet-5-5` ($2/$10) | `claude-haiku-5-5` ($0.10/$0.50) |
| DeepSeek | — | `deepseek-v4-pro` ($1.32/$3.96 peak) | `deepseek-flash` ($0.30/$1.20 peak) |
| OpenAI | `gpt-6.2-astra` ($8/$40) | `gpt-6.2-sol` ($2/$10), `gpt-6.1-terra` ($1/$5) | `gpt-6-luna` ($0.10/$0.50) |
| xAI | `grok-4.7` ($2/$6) | `grok-4.3` ($1.25/$2.50) | `grok-build-0.1` ($1/$2) |

Prices are input/output USD per 1M tokens. Cache rates and long-context tiers are in the file.
Sources: [OpenAI pricing](https://developers.openai.com/api/docs/pricing),
[xAI models](https://docs.x.ai/docs/models), and Anthropic's model table (claude-api reference,
cached 2026-11-03).

### Decisions and deviations (as built)
| # | Decision | Why |
|---|---|---|
| A1 | One adapter module for all three OpenAI-compatible providers, renamed from `deepseek_client.py` | P1 stays one SDK, one module. The dialects are three small switches, not three clients. `DeepSeekClient` keeps its name and behaviour, so every DeepSeek test passed unchanged |
| A2 | Structured output uses JSON mode (`json_object`) plus the schema in the system prompt for every OpenAI-compatible provider | It is the one form all three accept. OpenAI and xAI also offer strict `json_schema`, but it needs every field required and no defaults, which `Plan` (`test_command: … = None`) does not meet. Validation and the one repair call are Gaia's either way |
| A3 | `prices.toml` is the catalog (provider, label, tier) rather than a second file | A model must be priced before the gateway forwards it, so a separate list could only drift. One section per model is one place to add one |
| A4 | The gateway refuses a model on another provider's route | Before 19a it only checked the model was priced. With four providers, a mis-routed call would be metered at the wrong prices |
| A5 | OpenAI's cache-write rate is recorded, not billed | Chat Completions usage does not report cache writes separately. Gaia bills every uncached prompt token at `input`. If OpenAI charges writes on automatic caching, Gaia under-counts by at most 25% of uncached input; **check against the first OpenAI bill** |
| A6 | Where Anthropic lists no cache-read rate (Haiku 5.5), Gaia assumes 0.1× input | Only over-counts |
| A7 | The native runtime keeps its registry name `native_deepseek` | The name is stored on `run.junior_runtime`; it is now provider-neutral in behaviour |
| A8 | Auto-mode details now read "Agents running an Anthropic/DeepSeek/OpenAI/xAI model" | With modes (19b) any model can play any role, so "the lead" and "the juniors" stop being accurate |

**Unverified until the keys exist** (live test in 19b): that xAI accepts
`max_completion_tokens`, xAI's usage field names (`prompt_tokens_details.cached_tokens`,
reasoning outside `completion_tokens` were taken from xAI's documented example), and OpenAI's
JSON mode on the GPT-6 family. Each is one line to change if wrong.

## 19b — the modes (as built)

### What it adds
- **`AgentModeConfig`** (`domain/agent_modes.py`): `mode` (`single`, `tiered`, `crowd`),
  `lead_model`, `worker_models` (1 to 4, distinct; empty for `single`) and `panel_models` (2 to 5,
  distinct; `crowd` only). In `single` mode `lead_model` is the one model.
- **The default** for new runs: `AgentModeStore`, a 0600 file `agent-mode.json` in the data dir
  (like auto mode), read and written through `GET`/`PUT /api/v1/admin/agent-mode` (audited as
  `admin.agent_mode`). Without a saved default, `legacy_agent_config` applies: two tiered,
  `planner_model` leads, `junior_model` works.
- **Per run:** `POST /runs` takes an optional `agent_config`. The resolved config is stored on
  `run.agent_config` (migration `0007`) and returned on `RunRead`. Order: the request's config,
  then the old `junior_runtime` override (translated to the pre-19b roles), then the saved default,
  then the settings default. Every model must be in the catalog (422 `unknown_model`) and every
  provider's key must be set (422 `provider_not_configured`, naming each `gaia secrets set …`).
- **Model → runtime:** `services/agent_modes.runtime_for_model`: Anthropic → Claude Agent SDK,
  everything else → the native loop; `AgentTask.model` carries the model to either runtime. In
  demo mode an Anthropic agent session runs natively on `junior_model`.
- **The planner returns a `PlanOutcome`** (`plan`, `skip_gate`, `candidates`, `provenance`,
  `notes`). `lead_plan` keeps its extras in `RunState.plan_meta`, and `approve_plan` either skips
  the gate (straight shot) or puts the extras on the approval payload (crowd).
- **Straight shot:** `straight_shot_plan` turns the ticket into one step (no model call, no
  `test_command`, so the repository's registered command runs). The gate is skipped, the one model
  implements every attempt, reviews its own step, and the final review is skipped.
- **Two tiered:** the lead's planning message ends with a `## Workers` list (id, label, tier,
  list prices); `PlanStep.worker` records the choice and `assign_workers` pins it into the pool.
- **Wisdom of crowds:** the panel plans in parallel (`asyncio.gather`, one run token per call,
  each on its own provider); the lead merges with `lead_plan.md` + `lead_synthesize.md` into a
  `CrowdPlan` (a `Plan` plus `provenance`). Candidates are labelled Plan A, B, C by panel order.
- **Demo mode** answers a `CrowdPlan` schema with a demo merge, so all three modes run without a
  key; `test_demo_mode_e2e.py` drives straight shot to the PR gate and the crowd to the plan gate.

### Decisions and deviations (as built)
| # | Decision | Why |
|---|---|---|
| B1 | The modes live in the services and in `approve_plan`, not in the graph's shape | No new nodes or edges, so checkpoints of in-flight runs stay valid and `docs/graph.md` is unchanged |
| B2 | A run stores its resolved config; a run without one gets the pre-19b behaviour (`legacy_agent_config`) | M3: a resume after a restart, or after the owner changes the default, uses the same models. Runs started before 19b keep working unchanged |
| B3 | Provider keys are checked when a run starts, not when the default is saved | A default can name OpenAI or xAI before the key is added; a run still never fails at its first model call |
| B4 | The runtime follows the model's provider; the settings `lead_runtime`/`junior_runtime` no longer choose it | With any model in any role, the provider is the only thing that decides which harness can run it. The two settings stay (demo mode and the dev CLI read them) and can be removed later |
| B5 | Straight shot's model plays the lead role (`agent:lead`, `lead_max_turns`) | It does the whole ticket, which is the lead's turn budget, not a step's |
| B6 | Straight shot skips the final review | Its plan has one step, so the step review already covered the whole diff; a second call would review the same bytes |
| B7 | The lead sees the panel's plans as Plan A, B, C, never the model names | Judging plans on content, not brand. The owner sees the names at the gate |
| B8 | A failing panelist is skipped and noted; if all fail, the lead plans alone. A budget stop or cancellation is never swallowed | A run should survive one provider being down; a budget stop must pause the run as for any call |
| B9 | The worker list shows list prices and tiers | The prompt asks the lead to give each step to the cheapest worker that can do it well |
| B10 | The old `junior_runtime` request field stays, and is refused together with `agent_config` | API clients (the current Tickets view) keep working; one request cannot say two things |
| B11 | Pools are bounded: 1 to 4 workers, 2 to 5 panelists | A run must not fan out into dozens of parallel calls against one budget (07b G7) |

### Verified
- `make check`: 2089 backend tests at 100% coverage, 140 frontend tests, gitleaks.
- Demo mode end to end: straight shot on `gpt-6.2-sol` reached the PR gate with no plan gate and
  only OpenAI usage; the crowd (lead Claude, panel OpenAI, xAI, DeepSeek) stopped at the plan
  gate with three candidates, provenance, and usage on all four providers.
- The swap drill still passes: a run whose roles are all DeepSeek never touches Claude.

## 19c — UI (as built)

### What it adds
- **Settings → Agent mode** (`views/settings/AgentModeSettings.tsx`): the saved or settings
  default in one line, the editor, **Save default** (enabled only for a changed, valid config)
  and **Discard changes**.
- **The editor** (`views/agent-modes/AgentModeEditor.tsx`), shared with the start-run dialog:
  three mode cards with what each does; a **Lead** select (**Model** in straight shot); **Panel**
  and **Workers** checklists. Models are grouped by provider and show tier and list prices; a model
  whose provider has no key says "key not set", and the editor lists the `gaia secrets set …`
  commands the config needs. The pool limits (1 to 4 workers, 2 to 5 panelists) disable further
  boxes, and an invalid config says why ("Pick 2 to 5 panel models.").
- **Start run** (Tickets): **Use my default** (with its one-line summary) or **Choose for this
  run**, which opens the editor and sends `agent_config`. The old **Junior runtime** select is gone.
- **Run page:** an **Agent mode** line, for example "Wisdom of crowds: panel GPT-6.2 Sol, Grok
  4.7; Claude Opus 5.5 → DeepSeek V4 Pro"; a run from before 19b says so.
- **Plan approval:** each step's **Worker**, kept through **Edit steps**; for a crowd plan,
  **How the panel was used** with the provenance, any notes (a failed panelist), and every
  candidate plan, collapsed.

### Decisions and deviations (as built)
| # | Decision | Why |
|---|---|---|
| C1 | One controlled editor for Settings and the start-run dialog | Same rules and wording in both places; the dialog starts from the default |
| C2 | Models without a key can be picked; they are marked and the commands are shown | The backend allows a default to name a provider before its key is added (B3); the run start refuses it with the same command |
| C3 | Plan edits keep each step's `worker`; the approval does not offer changing it | The approval payload does not carry the run's worker pool, and an out-of-pool worker would fall back silently. Changing the pool is a new run |
| C4 | Removed the **Junior runtime** select | With modes, the runtime follows the model; the field stays in the API for scripts (B10) |

### Verified
- `make check`: 156 frontend tests (settings panel, editor limits and errors, start-run default
  and override, crowd section and worker preservation, the run page's mode line, the helpers).
- A real browser (Playwright, Chromium, the production build served by `gaia serve` in demo mode
  on its own data dir and port): saved a crowd default in Settings, started a run with it from
  Tickets, saw the mode on the run page, and opened the plan approval with the panel's plans and
  the step's worker. No console errors.
