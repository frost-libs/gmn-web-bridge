# SPDX-License-Identifier: MIT
"""Model menu for the Gemini web app.

Mode integers and hashes are the contract in docs/protocol.md. An unknown
name falls back to the default model. A `@think=N` suffix overrides the
model's thinking level. 0 is the deepest pass and 4 is the shallowest.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    mode: int
    think: int
    description: str
    token: str | None = None


MODELS: dict[str, ModelSpec] = {
    "gemini-3.8-flash": ModelSpec(1, 4, "All-around help (3.8 Flash)", "56fdd199312815e2"),
    "gemini-3.7-flash": ModelSpec(1, 4, "Alias of 3.8 Flash", "56fdd199312815e2"),
    "gemini-3.6-flash": ModelSpec(1, 4, "Alias of 3.8 Flash", "56fdd199312815e2"),
    "gemini-3.5-flash": ModelSpec(1, 4, "Alias of 3.8 Flash", "56fdd199312815e2"),
    "gemini-3.5-flash-thinking": ModelSpec(2, 0, "Deep thinking, longest output"),
    "gemini-3.1-pro": ModelSpec(3, 4, "Pro model. A free account can still be answered as Flash.", "e6fa609c3fa255c0"),
    "gemini-auto": ModelSpec(4, 4, "Auto model selection"),
    "gemini-3.5-flash-thinking-lite": ModelSpec(5, 0, "Adaptive thinking"),
    "gemini-3.5-flash-lite": ModelSpec(6, 4, "Fastest chat (3.5 Flash-Lite)", "cf41b0e0dd7d53e5"),
    "gemini-flash-lite": ModelSpec(6, 4, "Alias of 3.5 Flash-Lite", "cf41b0e0dd7d53e5"),
    "gemini-4-argon": ModelSpec(8, 4, "Complex tasks (4 Argon)", "95a4c0abcba24364"),
}


class InvalidThink(Exception):
    """`@think=` was not an integer."""


def resolve_model(model_name: str, default: str) -> tuple[str, ModelSpec, int]:
    """Return `(name, spec, think_level)` for a request model id."""
    think_override: int | None = None
    if "@think=" in model_name:
        model_name, think_str = model_name.rsplit("@think=", 1)
        try:
            think_override = int(think_str)
        except ValueError as exc:
            raise InvalidThink(think_str) from exc
    spec = MODELS.get(model_name)
    if spec is None:
        model_name = default if default in MODELS else "gemini-3.8-flash"
        spec = MODELS[model_name]
    think = think_override if think_override is not None else spec.think
    return model_name, spec, think
