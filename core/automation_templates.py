from __future__ import annotations

import re
from collections.abc import Iterable


AUTOMATION_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_ -]+)\}")


def automation_placeholder_names(value: str) -> tuple[str, ...]:
    """Return recognized placeholder names without interpreting other braces."""

    return tuple(
        match.group(1)
        for match in AUTOMATION_PLACEHOLDER.finditer(str(value or ""))
    )


def has_automation_placeholder(value: str) -> bool:
    """Whether a value contains recognized unresolved automation syntax."""

    return AUTOMATION_PLACEHOLDER.search(str(value or "")) is not None


def concrete_sdxl_tags(values: Iterable[str]) -> list[str]:
    """Exclude unresolved templates from consumers without mutating authority."""

    return [
        str(value)
        for value in values
        if str(value).strip() and not has_automation_placeholder(str(value))
    ]
