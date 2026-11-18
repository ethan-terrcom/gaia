"""The three agent modes and the models that play each role (doc 19b).

``AgentModeConfig`` is plain data: it names models, and :mod:`gaia.services.agent_modes`
checks them against the catalog (``prices.toml``) and the keychain. A run stores the
config it started with (``run.agent_config``), so a resume after a restart uses the
same models even if the owner's default has changed since.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Limits on the pools. Bounded so one run cannot fan out into dozens of calls.
MAX_WORKERS = 4
MIN_PANEL = 2
MAX_PANEL = 5
MAX_MODEL_ID_CHARS = 100


class AgentMode(StrEnum):
    """How a run's work is divided between models (owner decisions M1-M4)."""

    #: Straight shot: one model does the whole ticket in one agent session.
    SINGLE = "single"
    #: Two tiered: a lead plans, assigns steps to workers, reviews, escalates.
    TIERED = "tiered"
    #: Wisdom of crowds: a panel plans in parallel, the lead merges, then two tiered.
    CROWD = "crowd"


def _check_model_id(value: str) -> str:
    """A model id is a short non-blank string; the catalog check comes later."""
    if not value.strip() or len(value) > MAX_MODEL_ID_CHARS or value != value.strip():
        raise ValueError(f"invalid model id {value!r}")
    return value


class AgentModeConfig(BaseModel):
    """Which models play which roles in one run.

    - ``single``: ``lead_model`` is the one model. No workers, no panel.
    - ``tiered``: ``lead_model`` plans, reviews and takes over failing steps;
      ``worker_models`` (1 to 4) implement steps.
    - ``crowd``: as ``tiered``, plus ``panel_models`` (2 to 5) whose plans the
      lead merges.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: AgentMode
    lead_model: str
    worker_models: list[str] = Field(default_factory=list)
    panel_models: list[str] = Field(default_factory=list)

    @field_validator("lead_model")
    @classmethod
    def _lead_is_a_model_id(cls, value: str) -> str:
        return _check_model_id(value)

    @field_validator("worker_models", "panel_models")
    @classmethod
    def _pool_is_distinct_model_ids(cls, value: list[str]) -> list[str]:
        for model in value:
            _check_model_id(model)
        if len(set(value)) != len(value):
            raise ValueError("a model may appear only once in a pool")
        return value

    @model_validator(mode="after")
    def _roles_fit_the_mode(self) -> Self:
        if self.mode is AgentMode.SINGLE:
            if self.worker_models or self.panel_models:
                raise ValueError("straight shot uses one model: leave workers and panel empty")
            return self
        if not 1 <= len(self.worker_models) <= MAX_WORKERS:
            raise ValueError(f"{self.mode.value} mode needs 1 to {MAX_WORKERS} worker models")
        if self.mode is AgentMode.TIERED and self.panel_models:
            raise ValueError("two tiered mode has no panel: leave panel_models empty")
        if self.mode is AgentMode.CROWD and not MIN_PANEL <= len(self.panel_models) <= MAX_PANEL:
            raise ValueError(f"wisdom of crowds needs {MIN_PANEL} to {MAX_PANEL} panel models")
        return self

    def models(self) -> tuple[str, ...]:
        """Every model the config names, each once, lead first."""
        seen: dict[str, None] = {self.lead_model: None}
        for model in (*self.worker_models, *self.panel_models):
            seen.setdefault(model, None)
        return tuple(seen)

    def worker_for(self, requested: str | None) -> str:
        """The worker a step runs on: the plan's choice when it is in the pool.

        A missing or unknown choice (a plan edited by hand, or a model that
        invented an id) falls back to the first worker. Straight shot has no
        workers, so its one model does every step.
        """
        if self.mode is AgentMode.SINGLE:
            return self.lead_model
        if requested is not None and requested in self.worker_models:
            return requested
        return self.worker_models[0]


def default_agent_config(*, lead_model: str, junior_model: str) -> AgentModeConfig:
    """The config Gaia used before 19b: two tiered, one worker."""
    return AgentModeConfig(
        mode=AgentMode.TIERED, lead_model=lead_model, worker_models=[junior_model]
    )


def config_from_json(raw: Any) -> AgentModeConfig | None:
    """Parse a stored config, or ``None`` when ``raw`` is empty."""
    if raw is None:
        return None
    return AgentModeConfig.model_validate(raw)
