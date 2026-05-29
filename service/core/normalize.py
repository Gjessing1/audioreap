import re
import unicodedata
from dataclasses import dataclass

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


# ── Semantic modifier extraction ──────────────────────────────────────────────
# Phase 2: detect what KIND of recording a title/query describes BEFORE
# clean_for_search strips the surface noise. The flags flow into Phase 3's
# incompatibility gates (e.g. a `live` source must not silently match a studio
# MB recording). This is pure detection — it never mutates the text.
#
# Patterns are intentionally conservative (word-boundaried) to avoid false hits
# such as "alive" → live or "discover" → cover.
_MODIFIER_PATTERNS: dict[str, re.Pattern[str]] = {
    "is_live": re.compile(
        r"\b(?:live|unplugged|in\s+concert|concert)\b", re.IGNORECASE),
    "is_remix": re.compile(r"\b(?:remix|remixed|re-?edit)\b", re.IGNORECASE),
    "is_acoustic": re.compile(r"\bacoustic\b", re.IGNORECASE),
    "is_cover": re.compile(r"\b(?:cover|tribute)\b", re.IGNORECASE),
    "is_explicit": re.compile(r"\bexplicit\b", re.IGNORECASE),
    "is_karaoke": re.compile(r"\bkaraoke\b", re.IGNORECASE),
    "is_instrumental": re.compile(r"\binstrumental\b", re.IGNORECASE),
}


@dataclass(frozen=True)
class ModifierFlags:
    """Semantic flags describing what kind of recording a title/query refers to."""

    is_live: bool = False
    is_remix: bool = False
    is_acoustic: bool = False
    is_cover: bool = False
    is_explicit: bool = False
    is_karaoke: bool = False
    is_instrumental: bool = False

    @property
    def any(self) -> bool:
        """True if any modifier is set."""
        return any((
            self.is_live, self.is_remix, self.is_acoustic, self.is_cover,
            self.is_explicit, self.is_karaoke, self.is_instrumental,
        ))


def extract_modifiers(text: str) -> ModifierFlags:
    """Detect semantic modifiers in a raw title/query before search cleanup.

    Runs ahead of :func:`clean_for_search` so signals like ``(Live)`` or
    ``[Explicit]`` — which cleanup would otherwise discard — are preserved as
    structured flags for the identification gates.
    """
    if not text:
        return ModifierFlags()
    return ModifierFlags(**{
        name: bool(pat.search(text)) for name, pat in _MODIFIER_PATTERNS.items()
    })


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


# ── Album grouping normalization ──────────────────────────────────────────────
# Strips edition/remaster markers from album titles so "Abbey Road (Remastered)"
# and "Abbey Road" compare equal for library cohesion purposes. Never stored —
# used only for grouping comparison in find_canonical_album().
_EDITION_PAREN = re.compile(
    r"\s*[\[(][^\])[]*"
    r"(?:remaster(?:ed)?|\d+(?:th|st|nd|rd)\s+anniversary|deluxe|expanded|"
    r"special|limited|collector|legacy|super\s+deluxe|bonus\s+track)"
    r"[^\])]*[\])]",
    re.IGNORECASE,
)
_EDITION_DASH = re.compile(
    r"\s*[-–]\s*(?:remaster(?:ed)?|deluxe(?:\s+edition)?|"
    r"anniversary(?:\s+edition)?|expanded(?:\s+edition)?|"
    r"special(?:\s+edition)?|limited(?:\s+edition)?|"
    r"collector[\'s]*(?:\s+edition)?|legacy(?:\s+edition)?)$",
    re.IGNORECASE,
)


def normalize_album_for_grouping(title: str) -> str:
    """Strip edition/remaster suffixes for album cohesion comparison (never stored)."""
    result = _EDITION_PAREN.sub("", title)
    result = _EDITION_DASH.sub("", result)
    result = _WHITESPACE.sub(" ", result).strip()
    return normalize(result) or normalize(title)
