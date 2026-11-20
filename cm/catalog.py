"""Idea backlog management and channel-level topic deduplication."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from cm.models import Catalog, FormatPreset, IdeaEntry, IdeaStatus, TopicDomain
from cm.paths import atomic_write_text, catalog_path

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "to",
        "of",
        "in",
        "from",
        "how",
        "why",
        "and",
        "for",
        "with",
        "your",
        "its",
        "is",
        "are",
        "on",
        "at",
        "by",
    }
)


def slugify(text: str) -> str:
    """Convert arbitrary title text into a lowercase ASCII kebab-case slug."""
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    if not slug:
        raise ValueError(f"Cannot derive a valid slug from '{text}'")
    return slug


def tokenize_topic(text: str) -> set[str]:
    """Extract lowercase alphanumeric content tokens excluding common stop words."""
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return tokens - STOP_WORDS


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Compute Jaccard similarity between two token sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def load_catalog(root: Path | None = None) -> Catalog:
    """Load channels/catalog.json, creating an initialized catalog if missing."""
    path = catalog_path(root)
    if not path.is_file():
        catalog = Catalog()
        save_catalog(catalog, root)
        return catalog
    try:
        return Catalog.model_validate_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise ValueError(f"Corrupt catalog file at {path}: {exc}") from exc


def save_catalog(catalog: Catalog, root: Path | None = None) -> Path:
    """Persist the catalog atomically to channels/catalog.json."""
    return atomic_write_text(catalog_path(root), catalog.model_dump_json(indent=2) + "\n")


def find_duplicates(
    title: str,
    slug: str,
    catalog: Catalog,
    threshold: float = 0.6,
) -> list[str]:
    """Return warning messages for exact slug matches or similar topic titles."""
    warnings: list[str] = []
    query_tokens = tokenize_topic(title)

    for idea in catalog.ideas:
        if idea.slug == slug:
            warnings.append(f"Exact slug match with existing idea '{idea.id}' ({idea.title})")
            continue
        sim = jaccard_similarity(query_tokens, tokenize_topic(idea.title))
        if sim >= threshold:
            warnings.append(
                f"Similar title ({sim:.0%} overlap) to idea '{idea.id}' ({idea.title})"
            )

    for pub in catalog.published:
        if pub.slug == slug:
            warnings.append(f"Exact slug match with published video '{pub.slug}' ({pub.title})")
            continue
        pub_tokens = tokenize_topic(pub.title)
        for kw in pub.keywords:
            pub_tokens |= tokenize_topic(kw)
        sim = jaccard_similarity(query_tokens, pub_tokens)
        if sim >= threshold:
            warnings.append(
                f"Similar topic ({sim:.0%} overlap) to published video '{pub.slug}' ({pub.title})"
            )

    return warnings


def _next_idea_id(catalog: Catalog) -> str:
    max_num = 0
    for idea in catalog.ideas:
        match = re.match(r"^idea-(\d+)$", idea.id)
        if match:
            max_num = max(max_num, int(match.group(1)))
    return f"idea-{max_num + 1:03d}"


def add_idea(
    title: str,
    topic_domain: TopicDomain,
    hook: str,
    failure_chain: list[str],
    slug: str | None = None,
    fmt: FormatPreset = FormatPreset.SHORT_9X16,
    score: int = 7,
    root: Path | None = None,
) -> tuple[IdeaEntry, list[str]]:
    """Add a new idea to channels/catalog.json after checking slug collisions."""
    catalog = load_catalog(root)
    final_slug = (slug or slugify(title)).strip()
    if not SLUG_PATTERN.match(final_slug):
        raise ValueError(
            f"Invalid slug '{final_slug}': must be lowercase alphanumeric kebab-case"
        )

    if any(i.slug == final_slug for i in catalog.ideas):
        raise ValueError(f"Slug '{final_slug}' already exists in catalog ideas")
    if any(p.slug == final_slug for p in catalog.published):
        raise ValueError(f"Slug '{final_slug}' already exists in published catalog")

    cleaned_chain = [step.strip() for step in failure_chain]
    if len(cleaned_chain) < 3 or any(not step for step in cleaned_chain):
        raise ValueError("failure_chain requires at least 3 non-empty steps")

    warnings = find_duplicates(title, final_slug, catalog)

    entry = IdeaEntry(
        id=_next_idea_id(catalog),
        slug=final_slug,
        title=title.strip(),
        topic_domain=topic_domain,
        format=fmt,
        hook=hook.strip(),
        failure_chain=cleaned_chain,
        score=score,
        status=IdeaStatus.BACKLOG,
        created_at=datetime.now(UTC),
    )
    catalog.ideas.append(entry)
    save_catalog(catalog, root)
    return entry, warnings


def find_idea(catalog: Catalog, ref: str) -> IdeaEntry | None:
    """Find an idea entry by exact ID (e.g. idea-001) or slug."""
    for idea in catalog.ideas:
        if idea.id == ref or idea.slug == ref:
            return idea
    return None


def set_idea_status(ref: str, status: IdeaStatus, root: Path | None = None) -> IdeaEntry:
    """Update an idea's lifecycle status and persist the catalog."""
    catalog = load_catalog(root)
    idea = find_idea(catalog, ref)
    if idea is None:
        raise KeyError(f"Idea not found: '{ref}'")
    idea.status = status
    save_catalog(catalog, root)
    return idea
