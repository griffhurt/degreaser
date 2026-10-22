"""UI-agnostic completion for the category input (docs/product-spec.md §4.4).

The Qt widget forwards text edits and Tab presses here and renders what comes back:

    completer.ghost(text)            -> low-opacity suffix to draw after the text
    completer.tab(text, backwards)   -> new text after Tab / Shift+Tab, or None for no-op

Tab accepts the best completion of the tag being typed and appends ", ". Pressing Tab again
with nothing typed in between swaps that completion for the next candidate (Shift+Tab goes
back). Any other edit resets the cycle, because the next call sees text it didn't produce.
"""

from dataclasses import dataclass

from degreaser.core.categories.index import CategoryIndex

SEPARATOR = ", "


@dataclass(frozen=True, slots=True)
class _Cycle:
    base: str  # text before the completed tag
    candidates: list[str]
    index: int
    produced: str  # the text this cycle last produced


def _split(text: str) -> tuple[str, list[str], str]:
    """-> (text before the current fragment, preceding tags, current fragment)."""
    head, sep, fragment = text.rpartition(",")
    base = head + sep
    if sep:
        # Keep the user's spacing after the comma as part of the base.
        stripped = fragment.lstrip()
        base += fragment[: len(fragment) - len(stripped)]
        fragment = stripped
    preceding = [t.strip() for t in head.split(",") if t.strip()] if sep else []
    return base, preceding, fragment


class Completer:
    def __init__(self, index: CategoryIndex) -> None:
        self.index = index
        self._cycle: _Cycle | None = None

    def candidates(self, text: str) -> list[str]:
        _, preceding, fragment = _split(text)
        return self.index.candidates(preceding, fragment)

    def ghost(self, text: str) -> str:
        """Suffix of the best completion for the fragment being typed ('' if none)."""
        if self._cycle is not None and text == self._cycle.produced:
            return ""
        _, preceding, fragment = _split(text)
        if not fragment:
            return ""
        best = next(iter(self.index.candidates(preceding, fragment)), None)
        if best is None or len(best) == len(fragment):
            return ""
        return best[len(fragment) :]

    def tab(self, text: str, backwards: bool = False) -> str | None:
        cycle = self._cycle
        if cycle is not None and text == cycle.produced and len(cycle.candidates) > 1:
            step = -1 if backwards else 1
            index = (cycle.index + step) % len(cycle.candidates)
            produced = cycle.base + cycle.candidates[index] + SEPARATOR
            self._cycle = _Cycle(cycle.base, cycle.candidates, index, produced)
            return produced
        if cycle is not None and text == cycle.produced:
            return None  # only one candidate: nothing to cycle to
        base, preceding, fragment = _split(text)
        candidates = self.index.candidates(preceding, fragment)
        if not candidates:
            self._cycle = None
            return None
        index = len(candidates) - 1 if backwards else 0
        produced = base + candidates[index] + SEPARATOR
        self._cycle = _Cycle(base, candidates, index, produced)
        return produced

    def reset(self) -> None:
        self._cycle = None
