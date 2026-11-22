"""Text helpers shared by every stage, so word counts mean the same thing everywhere."""

from __future__ import annotations

import re
import unicodedata

# Emoji and pictographs, by Unicode block. No third-party emoji list is needed.
# Text emoticons such as ":)" are not emojis and are left alone, as are ₹ and other
# currency signs, letters in every script, and ordinary punctuation.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # mahjong/cards, emoticons, pictographs, transport, flags, skin tones
    "\U000E0020-\U000E007F"  # tag characters (subdivision flags)
    "☀-➿"          # miscellaneous symbols (★ ☺ ♥) and dingbats (✅ ✨ ❤)
    "⬀-⯿"          # arrows and symbols block (⭐ ⬆)
    "⌚⌛⏩-⏺"  # ⌚ ⌛ ⏩ … ⏺
    "〰〽㊗㊙"
    "︎️"           # variation selectors
    "‍"                 # zero-width joiner (joins emoji sequences)
    "⃣"                 # combining keycap
    "]+"
)
_SPACES = re.compile(r"[ \t ]{2,}")


def strip_emojis(text: str) -> str:
    """Remove emojis. Each emoji run becomes a space, so 'good👍app' stays two words."""
    if not text:
        return ""
    out = _EMOJI.sub(" ", text)
    out = _SPACES.sub(" ", out)
    return "\n".join(line.strip() for line in out.splitlines()).strip()


def has_emoji(text: str) -> bool:
    return bool(text) and _EMOJI.search(text) is not None


def words(text: str) -> list[str]:
    """Whitespace-separated tokens that contain at least one letter or digit.

    Punctuation-only tokens ("!!", "-") are not words. Devanagari vowel signs are
    combining marks, so a token counts as long as it has one base letter.
    """
    return [t for t in text.split() if any(ch.isalnum() for ch in t)]


def word_count(text: str) -> int:
    return len(words(text))


def normalize_for_hash(text: str) -> str:
    """Case-, width- and whitespace-insensitive form used only for dedupe hashing."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())
