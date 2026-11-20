# SPDX-License-Identifier: MIT
"""Model menu for the Gemini web app.

The 2026-11-18 menu is 4 Hydrogen, 4 Neon, and 4 Carbon. Older Flash, Pro,
and Argon ids use those same slots. Mode integers and hashes are the contract
in docs/protocol.md. An unknown name falls back to the default model. A
`@think=N` suffix overrides the model's thinking level. 0 is the deepest pass
and 4 is the shallowest.
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
    "gemini-4-hydrogen": ModelSpec(1, 4, "Everyday help (4 Hydrogen)", "56fdd199312815e2"),
    "gemini-3.8-flash": ModelSpec(1, 4, "Alias of 4 Hydrogen", "56fdd199312815e2"),
    "gemini-3.7-flash": ModelSpec(1, 4, "Alias of 4 Hydrogen", "56fdd199312815e2"),
    "gemini-3.6-flash": ModelSpec(1, 4, "Alias of 4 Hydrogen", "56fdd199312815e2"),
    "gemini-3.5-flash": ModelSpec(1, 4, "Alias of 4 Hydrogen", "56fdd199312815e2"),
    "gemini-3.5-flash-thinking": ModelSpec(2, 0, "Deep thinking, longest output"),
    "gemini-4-neon": ModelSpec(
        3,
        4,
        "Deeper analysis (4 Neon). A free account can still be answered as Hydrogen or Flash-Lite.",
        "e6fa609c3fa255c0",
    ),
    "gemini-3.1-pro": ModelSpec(3, 4, "Alias of 4 Neon", "e6fa609c3fa255c0"),
    "gemini-auto": ModelSpec(4, 4, "Auto model selection"),
    "gemini-3.5-flash-thinking-lite": ModelSpec(5, 0, "Adaptive thinking"),
    "gemini-3.5-flash-lite": ModelSpec(6, 4, "Fastest chat (3.5 Flash-Lite)", "cf41b0e0dd7d53e5"),
    "gemini-flash-lite": ModelSpec(6, 4, "Alias of 3.5 Flash-Lite", "cf41b0e0dd7d53e5"),
    "gemini-4-carbon": ModelSpec(
        8,
        4,
        "Code and complex work (4 Carbon). Listed for Ultra; other accounts can be answered as another model.",
        "95a4c0abcba24364",
    ),
    "gemini-4-argon": ModelSpec(8, 4, "Alias of 4 Carbon", "95a4c0abcba24364"),
}


class InvalidThink(Exception):
    """`@think=` was not an integer."""


def resolve_model(model_name: str, default: str) -> tuple[str, ModelSpec, int, bool]:
    """Return `(name, spec, think_level, fallback)` for a request model id.
    
    fallback is True when the requested name was unknown and substituted.
    """
    think_override: int | None = None
    if "@think=" in model_name:
        model_name, think_str = model_name.rsplit("@think=", 1)
        try:
            think_override = int(think_str)
        except ValueError as exc:
            raise InvalidThink(think_str) from exc
    spec = MODELS.get(model_name)
    fallback = spec is None
    if spec is None:
        model_name = default if default in MODELS else "gemini-3.8-flash"
        spec = MODELS[model_name]
    think = think_override if think_override is not None else spec.think
    return model_name, spec, think, fallback
