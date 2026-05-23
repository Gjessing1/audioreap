import re
import unicodedata

# ── Search-query cleanup ──────────────────────────────────────────────────────
# Stripped from titles/artists before sending to MB text search.
# Does NOT strip remix/live/acoustic/radio-edit/feat — those help find the right recording.
_SEARCH_NOISE: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\s*[\[(]official\s*(music\s*)?video[\])]",
        r"\s*[\[(]official\s*(audio|lyric\s*video|clip)[\])]",
        r"\s*[\[(]official[\])]",
        r"\s*[\[(]music\s*video[\])]",
        r"\s*[\[(]mv[\])]",
        r"\s*[\[(]hd[\])]",
        r"\s*[\[(]hq[\])]",
        r"\s*[\[(]4k[\])]",
        r"\s*[\[(]\d{3,4}p[\])]",
        r"\s*[\[(]lyrics?[\])]",
        r"\s*[\[(]w[/\s]?\s*lyrics?[\])]",
        r"\s*[\[(]with\s+lyrics?[\])]",
        r"\s*[\[(]lyric\s*video[\])]",
        r"\s*[\[(]remastered.*?[\])]",
        r"\s*[\[(]audio[\])]",
        r"\s*[\[(]visualizer[\])]",
        r"\s*[\[(]video[\])]",
        r"\s*[\[(]explicit[\])]",
        r"\s*[\[(]clean\s*version[\])]",
        r"\s*[\[(]full\s*(album|version)[\])]",
        r"\s*[\[(]extended\s*version[\])]",
    ]
]

_EMOJI_RE = re.compile(
    r"[\U00010000-\U0010ffff\U0001f600-\U0001f64f\U0001f300-\U0001f5ff"
    r"\U0001f680-\U0001f6ff\U0001f1e0-\U0001f1ff☀-⛿✀-➿]+",
    re.UNICODE,
)


def clean_for_search(text: str) -> str:
    """Strip YouTube noise from a title/artist before MB text search.

    Removes official-video markers, format/quality tags, lyrics indicators.
    Preserves: remix, live, acoustic, radio edit, feat./ft.
    """
    result = text
    for pat in _SEARCH_NOISE:
        result = pat.sub("", result)
    result = _EMOJI_RE.sub("", result)
    result = _WHITESPACE.sub(" ", result).strip()
    return result or text  # never return empty string


# ── Identity hashing noise ────────────────────────────────────────────────────
# Noise patterns stripped from titles/artists before identity hashing.
# Order matters: strip outer wrappers before inner content.
_NOISE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\s*\[official\s*(music\s*)?video\]",
        r"\s*\(official\s*(music\s*)?video\)",
        r"\s*\[official\s*(audio|lyric\s*video)\]",
        r"\s*\(official\s*(audio|lyric\s*video)\)",
        r"\s*\[hd\]",
        r"\s*\(hd\)",
        r"\s*\[hq\]",
        r"\s*\(hq\)",
        r"\s*\[4k\]",
        r"\s*\(4k\)",
        r"\s*\[remastered.*?\]",
        r"\s*\(remastered.*?\)",
        r"\s*\[explicit\]",
        r"\s*\(explicit\)",
        r"\s*\[clean\]",
        r"\s*\(clean\)",
        r"\s*\(feat\..*?\)",
        r"\s*\[feat\..*?\]",
        r"\s*\(ft\..*?\)",
        r"\s*\[ft\..*?\]",
        r"\s*\(featuring.*?\)",
        r"\s*\[featuring.*?\]",
        r"\s*\(with.*?\)",
        r"\s*\[with.*?\]",
        r"\s*\(prod\..*?\)",
        r"\s*\[prod\..*?\]",
        r"\s*\(lyrics?\)",
        r"\s*\[lyrics?\]",
        r"\s*\(audio\)",
        r"\s*\[audio\]",
        r"\s*\(visualizer\)",
        r"\s*\[visualizer\]",
    ]
]

_WHITESPACE = re.compile(r"\s+")


def strip_diacritics(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def normalize(text: str) -> str:
    """Canonical form used for identity hashing and fuzzy matching."""
    result = text
    for pattern in _NOISE_PATTERNS:
        result = pattern.sub("", result)
    result = strip_diacritics(result)
    result = result.lower()
    result = _WHITESPACE.sub(" ", result).strip()
    return result
