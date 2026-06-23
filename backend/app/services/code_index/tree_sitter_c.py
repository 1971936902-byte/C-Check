from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TreeSitterStatus:
    available: bool
    reason: str | None = None


def probe_tree_sitter_c() -> TreeSitterStatus:
    try:
        import tree_sitter  # noqa: F401
    except Exception as exc:
        return TreeSitterStatus(available=False, reason=f"tree-sitter unavailable: {exc}")
    try:
        import tree_sitter_c  # noqa: F401
    except Exception as exc:
        return TreeSitterStatus(available=False, reason=f"tree-sitter-c unavailable: {exc}")
    return TreeSitterStatus(available=True)
