from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def alias_pattern(alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.IGNORECASE)


def matches_alias(text: str | None, alias: str) -> bool:
    return bool(text and alias_pattern(alias).search(text))
