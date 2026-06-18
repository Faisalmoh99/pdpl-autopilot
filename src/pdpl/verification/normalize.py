"""Arabic text normalization — shared by the denylist match and the
references-control check (ADR-0009 §3).

Why this exists: both the compliance-assertion denylist (check 1) and the
references-control matcher (check 2) compare against Arabic strings, and
Arabic has many orthographic variants of the same word — alef forms
(أ/إ/آ/ا), hamza carriers, taa marbuta vs haa (ة/ه), alef maqsura vs yaa
(ى/ي), the tatweel elongation (ـ), and optional diacritics. Without
normalization a denylisted phrase or a control title would false-miss on a
trivial orthographic difference. Normalizing BOTH sides to one canonical
form before comparing is what lets paraphrase and orthography vary without
corrupting the checks.

This is pure, deterministic, dependency-free string work — exactly what a
trusted verifier (`pdpl.verification`) is allowed to contain.
"""

from __future__ import annotations

# Alef variants (with hamza above/below, madda, wasla) -> bare alef.
_ALEF_VARIANTS = "أإآٱ"
# Diacritics to strip: the harakat (fathatan..sukun), superscript alef, and
# the Quranic marks just past them, plus the tatweel elongation character.
_DIACRITICS = (
    "ًٌٍَُِّْ"  # fathatan..sukun
    "ٕٖٓٔٗ٘"  # madda/hamza above-below, etc.
    "ٰ"  # superscript alef
    "ـ"  # tatweel
)

# A single translation table applied in one pass: variant letters folded to a
# canonical letter, diacritics/tatweel removed (mapped to None).
_TRANSLATION = {ord(ch): ord("ا") for ch in _ALEF_VARIANTS}
_TRANSLATION[ord("ة")] = ord("ه")  # taa marbuta -> haa
_TRANSLATION[ord("ى")] = ord("ي")  # alef maqsura -> yaa
_TRANSLATION[ord("ؤ")] = ord("و")  # hamza-on-waw -> waw
_TRANSLATION[ord("ئ")] = ord("ي")  # hamza-on-yaa -> yaa
_TRANSLATION[ord("ء")] = None  # standalone hamza -> removed
for _ch in _DIACRITICS:
    _TRANSLATION[ord(_ch)] = None


def normalize(text: str) -> str:
    """Fold Arabic orthographic variants and collapse whitespace.

    Lowercases too, so the English half of the denylist matches
    case-insensitively (a no-op on Arabic letters). Returns a canonical form
    suitable for substring/token comparison — NOT for display.
    """
    folded = text.translate(_TRANSLATION).lower()
    # Collapse any run of whitespace to a single space; trim the ends.
    return " ".join(folded.split())
