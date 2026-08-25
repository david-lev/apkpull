"""Pure parsing of ``uiautomator dump`` XML.

Kept free of any adb/subprocess dependency so button-finding logic can be
unit tested against static XML fixtures instead of a real device.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NODE_RE = re.compile(r"<node\b[^>]*?/?>")
_ATTR_RE = re.compile(r'([\w-]+)="((?:[^"\\]|\\.)*)"')
_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


@dataclass(frozen=True, slots=True)
class UiNode:
    text: str
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


def iter_nodes(dump_xml: str):
    """Yield every ``<node>`` in the dump that carries non-empty text and bounds."""
    for match in _NODE_RE.finditer(dump_xml):
        attrs = dict(_ATTR_RE.findall(match.group(0)))
        text = attrs.get("text", "")
        bounds_raw = attrs.get("bounds", "")
        if not text:
            continue
        bounds_match = _BOUNDS_RE.match(bounds_raw)
        if not bounds_match:
            continue
        x1, y1, x2, y2 = (int(g) for g in bounds_match.groups())
        yield UiNode(text=_unescape(text), bounds=(x1, y1, x2, y2))


def find_button(dump_xml: str, text: str) -> tuple[int, int] | None:
    """Return the tap coordinates of the first node whose text exactly matches ``text``."""
    for node in iter_nodes(dump_xml):
        if node.text == text:
            return node.center
    return None


def contains_text(dump_xml: str, text: str) -> bool:
    return any(node.text == text for node in iter_nodes(dump_xml))


def find_text_near_content_desc(dump_xml: str, content_desc: str) -> str | None:
    """Find a node whose ``content-desc`` exactly matches ``content_desc`` — e.g.
    Play Store's warning-triangle icon, which carries no text of its own — and
    return the text of the nearest text-bearing node (by vertical distance
    between bounds centers), on the assumption that an icon and its
    accompanying message are one visual row. ``None`` if no such icon is
    present, or no text node exists to pair it with.
    """
    icon: UiNode | None = None
    texts: list[UiNode] = []
    for match in _NODE_RE.finditer(dump_xml):
        attrs = dict(_ATTR_RE.findall(match.group(0)))
        bounds_match = _BOUNDS_RE.match(attrs.get("bounds", ""))
        if not bounds_match:
            continue
        x1, y1, x2, y2 = (int(g) for g in bounds_match.groups())
        bounds = (x1, y1, x2, y2)
        if icon is None and _unescape(attrs.get("content-desc", "")) == content_desc:
            icon = UiNode(text="", bounds=bounds)
        text = attrs.get("text", "")
        if text:
            texts.append(UiNode(text=_unescape(text), bounds=bounds))

    if icon is None or not texts:
        return None
    _, icon_y = icon.center
    nearest = min(texts, key=lambda n: abs(n.center[1] - icon_y))
    return nearest.text


def contains_any_currency_amount(dump_xml: str, symbols: tuple[str, ...]) -> bool:
    """Detect a price tag like ``$4.99`` or ``₪17.90`` anywhere in the dump."""
    pattern = re.compile(
        r"\d+[.,]\d+\s*(?:" + "|".join(re.escape(s) for s in symbols) + ")"
    )
    alt_pattern = re.compile(
        r"(?:" + "|".join(re.escape(s) for s in symbols) + r")\s*\d+[.,]\d+"
    )
    for node in iter_nodes(dump_xml):
        if pattern.search(node.text) or alt_pattern.search(node.text):
            return True
    return False
