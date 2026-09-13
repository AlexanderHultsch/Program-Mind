"""Provider abstraction for the AI Board's model calls (AP-7).

Provider and model are configuration, never code (AP-7): the task type
resolves to a ``provider/model`` string read from ``provider.models.board``
in the runtime config, exactly the form OpenCode's own ``--model`` flag
expects.

``provider.endpoint`` is deliberately not read anywhere in this module.
OpenCode is invoked as a subprocess with a fixed runtime configuration of
its own; the endpoint is not passed per call. The field stays in the config
purely as the record of which endpoint was approved under AI-3 - it is not
dead configuration, do not remove it on that account.

Nothing here calls a model. This module only resolves configuration and
declares the interface a real client (the OpenCode client) implements.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any

TASK_BOARD = "ai_board"  # AI Board

TASK_MODEL_KEYS = {
    TASK_BOARD: "board",
}


class AiNotConfiguredError(RuntimeError):
    """Raised when a task type has no model configured to run it with (K-7)."""


def _get(config: dict, dotted: str) -> Any:
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if node not in ("", None) else None


def _model_key(task: str) -> str:
    try:
        return TASK_MODEL_KEYS[task]
    except KeyError:
        raise ValueError(f"unknown task type {task!r}") from None


def resolve_model(config: dict, task: str) -> str | None:
    """The ``provider/model`` string configured for ``task``, or ``None`` if
    that task type's config key has not been set yet."""
    key = _model_key(task)
    return _get(config, f"provider.models.{key}")


def token_limit(config: dict, task: str) -> int | None:
    """The configured token limit for ``task`` (AI-1), or ``None`` if unset."""
    key = _model_key(task)
    return _get(config, f"provider.token_limits.{key}")


def is_configured(config: dict, task: str) -> bool:
    """Whether ``task`` has a model configured to run it with."""
    return resolve_model(config, task) is not None


@dataclass(frozen=True)
class AiResult:
    text: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    duration_seconds: float
    over_token_limit: bool = False

    @property
    def total_tokens(self) -> int | None:
        """The value ``audit.log_run`` takes as ``tokens`` - the sum of
        whichever of the two token counts are known, or ``None`` if neither is."""
        if self.input_tokens is None and self.output_tokens is None:
            return None
        return (self.input_tokens or 0) + (self.output_tokens or 0)


class AiProvider(ABC):
    """One call to a configured model for one task type.

    Every implementation must return an ``AiResult`` with provider, model,
    token counts and duration filled in - AI-2 requires every run to log
    all four, and this is the one place that guarantees it.
    """

    @abstractmethod
    def complete(self, task: str, prompt: str, on_text: Callable[[str], None] | None = None) -> AiResult:
        """``on_text`` is called with the answer as far as the model has
        written it, whenever that changes (spec 4.1, 13 September 2026).
        Only a provider that streams calls it; what it passes is for the
        page's eye alone, and the answer is the ``AiResult``."""
        raise NotImplementedError


def build_provider(config: dict, *, cwd: Path | str | None = None) -> AiProvider | None:
    """The configured provider, or ``None`` if no task type has a model set.

    as "AI not available" and report it rather than failing (NFR-8) - this
    keeps that contract rather than raising.

    ``provider.opencode.mode`` (spec 4.1, decision 6) picks the way in:
    ``auto`` (the default: the server, falling back to the run when it will
    not start), ``serve``, or ``run``. Only the server streams the answer as
    it is written.
    """
    if not any(resolve_model(config, task) is not None for task in TASK_MODEL_KEYS):
        return None

    # Imported here, not at module top level, so this module keeps calling no
    # model itself (see the module docstring) - only the OpenCode client does.
    from .opencode_client import OpenCodeProvider

    run = OpenCodeProvider(config, cwd=cwd)
    mode = str(_get(config, "provider.opencode.mode") or "auto").strip().lower()
    if mode == "run":
        return run
    from .opencode_server import OpenCodeServerProvider

    return OpenCodeServerProvider(config, fallback=None if mode == "serve" else run)


def streams(config: dict) -> bool:
    """Whether answers can be shown as they are written (spec 4.1)."""
    return str(_get(config, "provider.opencode.mode") or "auto").strip().lower() != "run"
