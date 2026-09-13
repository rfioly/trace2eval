"""Alternative similarity matchers.

The default matcher lives in :mod:`trace2eval.select` and works on character
n-grams, because that is the only thing that handles Chinese and English through
one code path without a dependency. Its blind spot is fixed and known: a
paraphrase that shares no characters with the original scores zero.

Anything with the signature ``(str, str) -> float`` returning a value in
``[0, 1]`` can be swapped in:

    trace2eval build traces.jsonl --similarity my_embeddings:cosine

That is the intended answer to the paraphrase problem -- bring whatever model you
already have rather than making this package depend on one. This module ships two
dependency-free matchers: useful in their own right, and working examples of the
hook if you want to write your own.

Be aware of the cost when you swap: a custom matcher disables the n-gram inverted
index used to shortlist clusters, because a lexical shortlist would cap exactly
the recall you brought the custom matcher in for. Clustering then compares against
every cluster, so it gets noticeably slower on a large log.
"""

from __future__ import annotations

import re

#: Latin/digit words. Split on everything else.
_WORD_PATTERN = re.compile(r"[a-z0-9]+")

#: CJK ideographs, treated as individual tokens because there are no word
#: boundaries to split on.
_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]")

#: Below this many shared tokens the two inputs are unrelated as far as these
#: matchers are concerned. Guards the same failure mode ``MIN_SHARED_SHINGLES``
#: guards: two short inputs sharing one token scoring an overlap of 1.0.
MIN_SHARED_TOKENS = 2


def tokenise(text: str) -> set[str]:
    """Split into a token set: words for Latin script, characters for CJK.

    Mixed text gets the union, so an English error message embedded in a Chinese
    question still contributes its words.
    """
    lowered = text.lower()
    tokens = set(_WORD_PATTERN.findall(lowered))
    tokens |= set(_CJK_PATTERN.findall(lowered))
    return tokens


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    shared = len(left & right)
    if shared < MIN_SHARED_TOKENS:
        return 0.0
    return shared / min(len(left), len(right))


def word_jaccard(left: str, right: str) -> float:
    """Token-level overlap coefficient.

    Trades the default matcher's precision for word-order robustness. Prefer it
    when inputs are whole sentences in a whitespace-delimited language.

    Two honest caveats:

    - For Chinese it is *coarser* than the default. Character bigrams keep some
      ordering information; single characters do not, so genuinely different
      questions that share most of their characters score higher here. Measured
      on the sample log: ``支持哪些登录方式`` vs ``支持哪些支付方式`` scores 0.571
      under the default matcher and 0.857 under this one. If you use this on
      Chinese traffic, raise ``--dedup-threshold``.
    - It is a matcher for experimenting with the hook, not a recommendation. The
      real upgrade for paraphrase recall is embeddings, which this package does
      not bundle on purpose.
    """
    return _overlap(tokenise(left), tokenise(right))


def char_bigram_jaccard(left: str, right: str) -> float:
    """Plain Jaccard over character bigrams.

    Here to be measured against, not used. ``tests/test_dedup.py`` asserts that it
    ranks two different questions above two phrasings of the same question on the
    sample log, which is the observation that ruled Jaccard out for the built-in
    matcher. Useful if you want to reproduce that measurement on your own data.
    """
    from .select import SHINGLE_SIZE, jaccard, normalise

    def grams(text: str) -> set[str]:
        folded = normalise(text)
        if not folded:
            return set()
        if len(folded) <= SHINGLE_SIZE:
            return {folded}
        return {folded[i : i + SHINGLE_SIZE] for i in range(len(folded) - SHINGLE_SIZE + 1)}

    return jaccard(grams(left), grams(right))
