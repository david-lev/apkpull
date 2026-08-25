"""Pure parsing of ``uiautomator dump`` XML.

Kept free of any adb/subprocess dependency so button-finding logic can be
unit tested against static XML fixtures instead of a real device.

:func:`parse` does the one and only regex pass over a dump's raw XML;
every lookup below (:func:`find_button`, :func:`contains_text`, ...) takes
its ``list[UiNode]`` result rather than the XML string itself, so a single
poll tick's dump only ever gets scanned once no matter how many checks run
against it -- see ``automation.py``'s ``_check_error_screens``/
``_wait_for_button``, which parse once per tick and reuse the result across
what would otherwise be up to a dozen-plus independent re-scans.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache

_NODE_RE = re.compile(r"<node\b[^>]*?/?>")
_ATTR_RE = re.compile(r'([\w-]+)="((?:[^"\\]|\\.)*)"')
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass(frozen=True, slots=True)
class UiNode:
    text: str
    content_desc: str
    bounds: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2


def _unescape(value: str) -> str:
    return (
        value.replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


def parse(dump_xml: str) -> list[UiNode]:
    """Parse every ``<node>`` with valid bounds out of a dump, once.

    Nodes with no ``text`` (but a real ``content-desc``, e.g. Play Store's
    warning-triangle icon) are still included -- only a missing/invalid
    ``bounds`` excludes a node, since that's what makes it unusable either
    way (can't tap it, can't measure distance to it).
    """
    nodes: list[UiNode] = []
    for match in _NODE_RE.finditer(dump_xml):
        attrs = dict(_ATTR_RE.findall(match.group(0)))
        bounds_match = _BOUNDS_RE.match(attrs.get("bounds", ""))
        if not bounds_match:
            continue
        x1, y1, x2, y2 = (int(g) for g in bounds_match.groups())
        nodes.append(
            UiNode(
                text=_unescape(attrs.get("text", "")),
                content_desc=_unescape(attrs.get("content-desc", "")),
                bounds=(x1, y1, x2, y2),
            )
        )
    return nodes


def find_button(nodes: Sequence[UiNode], text: str) -> tuple[int, int] | None:
    """Return the tap coordinates of the first node whose text exactly matches ``text``."""
    for node in nodes:
        if node.text == text:
            return node.center
    return None


def contains_text(nodes: Sequence[UiNode], text: str) -> bool:
    return any(node.text == text for node in nodes)


def find_text_near_content_desc(
    nodes: Sequence[UiNode], content_desc: str
) -> str | None:
    """Find a node whose ``content-desc`` exactly matches ``content_desc`` — e.g.
    Play Store's warning-triangle icon, which carries no text of its own — and
    return the text of the nearest text-bearing node (by vertical distance
    between bounds centers), on the assumption that an icon and its
    accompanying message are one visual row. ``None`` if no such icon is
    present, or no text node exists to pair it with.
    """
    icon = next((n for n in nodes if n.content_desc == content_desc), None)
    texts = [n for n in nodes if n.text]
    if icon is None or not texts:
        return None
    _, icon_y = icon.center
    nearest = min(texts, key=lambda n: abs(n.center[1] - icon_y))
    return nearest.text


@cache
def _currency_patterns(
    symbols: tuple[str, ...],
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    escaped = "|".join(re.escape(s) for s in symbols)
    return (
        re.compile(rf"\d+[.,]\d+\s*(?:{escaped})"),
        re.compile(rf"(?:{escaped})\s*\d+[.,]\d+"),
    )


def contains_any_currency_amount(
    nodes: Sequence[UiNode], symbols: tuple[str, ...]
) -> bool:
    """Detect a price tag like ``$4.99`` or ``₪17.90`` anywhere in the dump."""
    pattern, alt_pattern = _currency_patterns(symbols)
    for node in nodes:
        if node.text and (pattern.search(node.text) or alt_pattern.search(node.text)):
            return True
    return False
