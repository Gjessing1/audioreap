import re
import unicodedata

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
