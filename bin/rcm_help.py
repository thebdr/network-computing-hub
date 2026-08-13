#!/usr/bin/env python3
"""Help registry: user-facing behaviour is documented once, in the source.

The design handoff's engineering mandate: the source code is the primary manual.
Every user-facing behaviour registers itself here — usually by decorating the
function that implements it with @help_topic — and the offline Help (F1) is
GENERATED from this registry. The guide may add framing text around topics, but
behaviour text lives in code only, so it cannot drift from what the code does.

Each topic records where it came from (source_file:line), which the Help window
renders as an "Open code" link: the skilled reader's path from any explanation
straight to the implementation.

This module is imported by both the `rcm` CLI and the GUI; it must import
nothing from either (no cycles), and stay dependency-free.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HelpTopic:
    topic_id: str            # stable slug, e.g. "connect-buttons"
    title: str               # human heading, e.g. "Connect buttons & ticking"
    body: str                # behaviour text, taken from the docstring
    keywords: tuple = ()     # extra search terms beyond title words
    section: str = ""        # grouping in the Help sidebar, e.g. "Connections"
    source_file: str = ""    # repo-relative path of the registering code
    source_line: int = 0     # 1-based line of the registering symbol
    symbol: str = ""         # function/class name, shown in the Open-code link


# The single registry. Import order decides insertion order, which the Help
# sidebar preserves within each section.
HELP_TOPICS: dict[str, HelpTopic] = {}

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _relative_source(obj) -> tuple[str, int]:
    """(repo-relative file, line) for a function or class, best effort."""
    try:
        src = inspect.getsourcefile(obj) or ""
        _, line = inspect.getsourcelines(obj)
        path = Path(src).resolve()
        try:
            return str(path.relative_to(_REPO_ROOT)), line
        except ValueError:
            return str(path), line
    except (OSError, TypeError):
        return "", 0


def help_topic(topic_id: str, title: str, keywords: tuple = (), section: str = ""):
    """Register the decorated function's docstring as a Help topic.

    The docstring must therefore be written for the *user of the behaviour*,
    not only the maintainer: what it does, how to drive it, what the edge rules
    are. The decorator changes nothing about the function itself.
    """
    def register(func):
        body = inspect.cleandoc(func.__doc__ or "")
        src, line = _relative_source(func)
        HELP_TOPICS[topic_id] = HelpTopic(
            topic_id=topic_id, title=title, body=body, keywords=tuple(keywords),
            section=section, source_file=src, source_line=line,
            symbol=getattr(func, "__qualname__", getattr(func, "__name__", "")))
        return func
    return register


def register_topic(topic_id: str, title: str, body: str, keywords: tuple = (),
                   section: str = "", source_file: str = "",
                   source_line: int = 0, symbol: str = "") -> None:
    """Register a topic that has no single function to hang on.

    Used for behaviours that live in config conventions or span several
    functions — the body is still authored next to the code that implements it.
    """
    HELP_TOPICS[topic_id] = HelpTopic(
        topic_id=topic_id, title=title, body=inspect.cleandoc(body),
        keywords=tuple(keywords), section=section, source_file=source_file,
        source_line=source_line, symbol=symbol)


def topics_by_section() -> dict[str, list[HelpTopic]]:
    """Topics grouped for the Help sidebar, insertion order preserved."""
    grouped: dict[str, list[HelpTopic]] = {}
    for t in HELP_TOPICS.values():
        grouped.setdefault(t.section or "General", []).append(t)
    return grouped


def search_topics(query: str) -> list[HelpTopic]:
    """Case-insensitive full-text match over title, keywords and body."""
    needle = query.strip().lower()
    if not needle:
        return list(HELP_TOPICS.values())
    hits = []
    for t in HELP_TOPICS.values():
        haystack = " ".join([t.title, " ".join(t.keywords), t.body]).lower()
        if all(word in haystack for word in needle.split()):
            hits.append(t)
    return hits
