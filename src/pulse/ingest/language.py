"""Language detection tuned for short Indian app-store reviews (edge case NRM-09).

langdetect alone is unreliable on short text. On real Groww reviews it labelled
"Best app ever for trading" as Norwegian and "Very user friendly" as Danish, both
with >99% confidence, and Latin-script Hinglish as Indonesian. Only English text is
eligible for quotes, so those errors would silently discard good quotes.

Order of checks:
1. Hinglish marker words (Latin-script Hindi) → "hi-Latn"
2. A high share of frequent English words → "en"
3. Otherwise langdetect, accepted only above a confidence threshold, else "und".
"""

from __future__ import annotations

import re

from langdetect import DetectorFactory, LangDetectException, detect_langs

from pulse.textutil import word_count

DetectorFactory.seed = 0  # langdetect is random unless seeded

UNDETERMINED = "und"
HINGLISH = "hi-Latn"
MIN_LANG_WORDS = 3
MIN_LANG_PROB = 0.80
HINGLISH_MIN_SHARE = 0.20
ENGLISH_MIN_SHARE = 0.40

_TOKEN = re.compile(r"[a-z']+")

# Words that are common in romanized Hindi and rare in English. Ambiguous ones such
# as "to", "me", "do", "hi", "main", "par" are left out on purpose.
def _vocab(block: str) -> frozenset[str]:
    return frozenset(block.split())


_HINGLISH = _vocab("""
hai hain nahi nahin nhi kya kyu kyun kyon mera mere meri mujhe aap ap apka apko tum
kar karo karna karte kiya raha rahi rahe gaya gayi kuch bahut bhut bohot bhi kab kaise
paisa paise ek ki ke ka se ko wala wale wali accha acha achha bekar bakwas yaar sab jab
tab abhi hota hoti hua liye lekin aur toh mat diya dikkat koi kaam ye yeh wo woh hum
humara hamara sahi galat bilkul jaldi zyada jyada pehle baad shuru band chalu
""")

# Frequent English function words plus everyday app-review vocabulary.
_ENGLISH = _vocab("""
a an the and or but if of to in on at for from with by as is are was were be been being
am it its this that these those i me my we our you your he she they them their not no
yes very so too just only also all any some more most much many can could will would
should do does did done have has had get got make made use used using than then there
here what when where why how which who after before again still even ever never always
please good great best bad worst nice easy simple fast slow better worse love like hate
app apps application update updates updated user users friendly experience service
support customer team money fund funds account bank payment payments withdraw withdrawal
order orders trade trading trader traders stock stocks market investment invest
investing broker broking platform interface issue issues problem problems working work
works time day days crash crashes login screen option options feature features charges
""")


def _share(tokens: list[str], vocab: frozenset[str]) -> float:
    return sum(t in vocab for t in tokens) / len(tokens) if tokens else 0.0


def detect_language(text: str) -> str:
    """ISO 639-1 code, 'hi-Latn' for romanized Hindi, or 'und' when unsure."""
    if word_count(text) < MIN_LANG_WORDS:
        return UNDETERMINED
    tokens = _TOKEN.findall(text.lower())
    latin_share = len("".join(tokens)) / max(1, sum(ch.isalpha() for ch in text))
    if latin_share >= 0.9:  # mostly Latin script, so the word lists apply
        if _share(tokens, _HINGLISH) >= HINGLISH_MIN_SHARE:
            return HINGLISH
        if _share(tokens, _ENGLISH) >= ENGLISH_MIN_SHARE:
            return "en"
    letters = [ch for ch in text if ch.isalpha()]
    if letters and sum(ch.isupper() for ch in letters) / len(letters) > 0.7:
        text = text.lower()  # langdetect misreads ALL-CAPS English (seen: labelled German)
    try:
        best = detect_langs(text)[0]
    except LangDetectException:
        return UNDETERMINED
    return best.lang if best.prob >= MIN_LANG_PROB else UNDETERMINED
