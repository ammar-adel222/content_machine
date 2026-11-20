"""Tests for idea backlog management and topic deduplication."""

from __future__ import annotations

from pathlib import Path

import pytest

from cm.catalog import (
    add_idea,
    find_idea,
    load_catalog,
    set_idea_status,
    slugify,
)
from cm.models import IdeaStatus, TopicDomain


def test_slugify_generates_clean_kebab_case() -> None:
    assert slugify("Building a Cache From Scratch!") == "building-a-cache-from-scratch"
    assert slugify("  HTTP/2 vs HTTP/3  ") == "http-2-vs-http-3"


def test_add_idea_collision_and_near_duplicate_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    first, warnings1 = add_idea(
        title="Building a Password System From Scratch",
        topic_domain=TopicDomain.SECURITY,
        hook="How do apps protect your password from hackers?",
        failure_chain=["Plaintext leak", "Rainbow tables", "GPU brute force"],
        slug="password-system",
        score=9,
    )
    assert first.id == "idea-001"
    assert warnings1 == []

    with pytest.raises(ValueError, match="already exists"):
        add_idea(
            title="Duplicate Slug",
            topic_domain=TopicDomain.SECURITY,
            hook="Hook",
            failure_chain=["A", "B", "C"],
            slug="password-system",
        )

    second, warnings2 = add_idea(
        title="Password System From Scratch Explained",
        topic_domain=TopicDomain.SECURITY,
        hook="Why plaintext fails",
        failure_chain=["Leak", "Hash table", "GPU cluster"],
        slug="password-system-explained",
    )
    assert second.id == "idea-002"
    assert len(warnings2) == 1
    assert "overlap" in warnings2[0]


def test_find_and_set_idea_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    entry, _ = add_idea(
        title="Building DNS From Scratch",
        topic_domain=TopicDomain.NETWORKING,
        hook="Why typing google.com finds a server in milliseconds",
        failure_chain=["Hosts file breaks", "Single server crashes", "Cache poisoning"],
    )

    catalog = load_catalog()
    assert find_idea(catalog, "idea-001") is not None
    assert find_idea(catalog, entry.slug) is not None

    updated = set_idea_status("idea-001", IdeaStatus.IN_PROGRESS)
    assert updated.status == IdeaStatus.IN_PROGRESS
