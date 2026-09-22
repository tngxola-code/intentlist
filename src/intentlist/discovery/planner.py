"""Query planning: turn a category into many narrow, high-intent search queries."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol

from ..config import CategorySpec, Taxonomy
from ..llm import ClaudeClient

log = logging.getLogger(__name__)

# English intents tried first; other languages are added for international coverage.
PRIMARY_INTENTS = ["WTB", "want to buy", "looking for", "ISO", "wanted"]


@dataclass(frozen=True)
class PlannedQuery:
    text: str
    template_key: str
    site: str | None


def _compose(intent: str, item: str, site: str | None) -> PlannedQuery:
    site_part = f" site:{site}" if site else ""
    return PlannedQuery(
        text=f'"{intent}" "{item}"{site_part}',
        template_key=f"{intent.lower()}|{item.lower()}|{site or 'web'}",
        site=site,
    )


class Planner(Protocol):
    def plan(self, spec: CategorySpec, sites: list[str], history_items: list[str]) -> list[PlannedQuery]: ...


class TemplatePlanner:
    """Deterministic planner: makers x intent phrases x allowed sites. Needs no API key."""

    def __init__(self, taxonomy: Taxonomy, include_international: bool = True):
        self.taxonomy = taxonomy
        self.include_international = include_international

    def intents(self) -> list[str]:
        intents = list(PRIMARY_INTENTS)
        if self.include_international:
            for lang, phrases in self.taxonomy.intent_phrases.items():
                if lang != "en" and phrases:
                    intents.append(phrases[0])
        return intents

    def items(self, spec: CategorySpec, history_items: list[str]) -> list[str]:
        return list(dict.fromkeys([*history_items, *spec.makers]))

    def plan(self, spec: CategorySpec, sites: list[str], history_items: list[str]) -> list[PlannedQuery]:
        targets = sites or [None]
        return [_compose(i, item, s) for item in self.items(spec, history_items) for i in self.intents()
                for s in targets]


_PLANNER_TOOL = {
    "name": "submit_items",
    "description": "Submit specific collectible items people commonly post wanted-to-buy requests for.",
    "input_schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "string", "description": "Maker + model or reference, 2-5 words, e.g. 'Rolex 116500LN'"},
                "minItems": 5,
                "maxItems": 60,
            }
        },
        "required": ["items"],
    },
}


class ClaudePlanner(TemplatePlanner):
    """Asks Claude for specific, frequently-sought items (models, references, years), then composes queries.

    Specific item names find far better pages than bare maker names: "WTB Patek 5711" surfaces actual
    requests, "WTB Patek" mostly surfaces dealer listings.
    """

    def __init__(self, taxonomy: Taxonomy, llm: ClaudeClient):
        super().__init__(taxonomy)
        self.llm = llm
        self.last_usage: dict = {}

    def items(self, spec: CategorySpec, history_items: list[str]) -> list[str]:
        prompt = (
            f"Category: {spec.name}.\nKnown makers: {', '.join(spec.makers)}.\nKnown models: {', '.join(spec.models)}.\n"
            f"Items that produced verified buyer requests before: {json.dumps(history_items[:30])}.\n\n"
            "List specific high-value items (maker + model, reference number or model year) that private "
            "collectors frequently post 'wanted to buy' requests for on forums and classifieds, internationally. "
            "Prefer items with active secondary markets. Include the previously productive items."
        )
        try:
            data, usage = self.llm.call_tool(prompt, _PLANNER_TOOL, max_tokens=2000)
            self.last_usage = usage
            items = [s.strip() for s in data.get("items", []) if isinstance(s, str) and s.strip()]
        except Exception as exc:  # planner failure must not stop a run
            log.warning("Claude planner failed, falling back to template items: %s", exc)
            items = []
        return list(dict.fromkeys([*history_items, *items, *spec.makers]))
