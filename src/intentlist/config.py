"""Loads the taxonomy and source registry, and syncs both into the database."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Category, Source
from .settings import get_settings


@dataclass(frozen=True)
class CategorySpec:
    slug: str
    name: str
    active: bool
    makers: tuple[str, ...]
    models: tuple[str, ...]
    reference_patterns: tuple[re.Pattern, ...] = field(default_factory=tuple)
    year_pattern: re.Pattern | None = None

    def _find_terms(self, text: str, terms: tuple[str, ...]) -> list[str]:
        found = []
        for term in terms:
            if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, flags=re.IGNORECASE):
                found.append(term)
        return found

    def makers_in(self, text: str) -> list[str]:
        return self._find_terms(text, self.makers)

    def models_in(self, text: str) -> list[str]:
        return self._find_terms(text, self.models)

    def specificity(self, text: str) -> dict:
        """Evidence that a request names a specific item, not just a category."""
        makers = self.makers_in(text)
        models = self.models_in(text)
        refs = [m.group(0) for p in self.reference_patterns for m in p.finditer(text)]
        years = [m.group(0) for m in self.year_pattern.finditer(text)] if self.year_pattern else []
        specific = bool(makers) and bool(models or refs or years)
        return {"specific": specific, "makers": makers, "models": models, "refs": refs[:5], "years": years[:3]}


@dataclass(frozen=True)
class Taxonomy:
    categories: dict[str, CategorySpec]
    intent_phrases: dict[str, tuple[str, ...]]

    def all_intent_phrases(self) -> list[str]:
        return [p for group in self.intent_phrases.values() for p in group]

    def intent_phrase_in(self, text: str) -> str | None:
        for phrase in sorted(self.all_intent_phrases(), key=len, reverse=True):
            pattern = r"(?<!\w)" + re.escape(phrase) + (r"(?!\w)" if phrase[-1].isalnum() else "")
            if re.search(pattern, text, flags=re.IGNORECASE):
                return phrase
        return None


def _compile_category(raw: dict) -> CategorySpec:
    return CategorySpec(
        slug=raw["slug"],
        name=raw["name"],
        active=bool(raw.get("active", False)),
        makers=tuple(str(x) for x in raw.get("makers", [])),
        models=tuple(str(x) for x in raw.get("models", [])),
        reference_patterns=tuple(re.compile(p) for p in raw.get("reference_patterns", [])),
        year_pattern=re.compile(raw["year_pattern"]) if raw.get("year_pattern") else None,
    )


def load_taxonomy(path: Path | None = None) -> Taxonomy:
    data = yaml.safe_load(Path(path or get_settings().taxonomy_file).read_text(encoding="utf-8"))
    cats = {c["slug"]: _compile_category(c) for c in data["categories"]}
    phrases = {lang: tuple(v) for lang, v in (data.get("intent_phrases") or {}).items()}
    return Taxonomy(categories=cats, intent_phrases=phrases)


@lru_cache
def taxonomy() -> Taxonomy:
    return load_taxonomy()


def load_sources(path: Path | None = None) -> list[dict]:
    data = yaml.safe_load(Path(path or get_settings().sources_file).read_text(encoding="utf-8"))
    return data.get("sources", [])


def sync_config(session: Session, tax: Taxonomy | None = None, sources: list[dict] | None = None) -> None:
    """Idempotently upsert categories and sources from YAML into the DB."""
    tax = tax or taxonomy()
    for spec in tax.categories.values():
        row = session.scalar(select(Category).where(Category.slug == spec.slug))
        if row is None:
            session.add(Category(slug=spec.slug, name=spec.name, active=spec.active))
        else:
            row.name, row.active = spec.name, spec.active

    for raw in sources if sources is not None else load_sources():
        domain = raw["domain"].lower().removeprefix("www.")
        row = session.scalar(select(Source).where(Source.domain == domain))
        if row is None:
            row = Source(domain=domain)
            session.add(row)
        row.name = raw.get("name", domain)
        row.kind = raw.get("kind", "forum")
        row.allowed = bool(raw.get("allowed", False))
        row.respect_robots = bool(raw.get("respect_robots", True))
        row.render_js = bool(raw.get("render_js", False))
        row.rate_limit_rps = raw.get("rate_limit_rps")
        row.categories = list(raw.get("categories", []))
        row.tos_notes = raw.get("tos_notes")
        reviewed = raw.get("tos_reviewed_on")
        row.tos_reviewed_on = reviewed if isinstance(reviewed, date) else (
            date.fromisoformat(reviewed) if reviewed else None
        )
    session.flush()
