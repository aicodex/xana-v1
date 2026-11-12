"""Adapted from Zleap SAG2 bb7f8275 (MIT); see NOTICE.md for changes."""

from __future__ import annotations

import json, logging, math, re, unicodedata
from collections import Counter
from functools import lru_cache
from typing import Any
import jieba
import jieba.finalseg
import regex
from pyuca import Collator
from .chunking import DEFAULT_CHUNK_CHARS, chunk_document

INDEX_SCHEMA = 5
QUERY_WORKERS = 8
QUERY_CACHE_ENTRIES = 256
EXPANSION_METHOD = "query_sentence_tfidf_capitalized_names_jev_weight"
RETRIEVAL_METHOD = "per_term_all_expansions_query_bm25_rank_interleave"
FINAL_METHOD = "history_top10_scores_local_dpp"
STOPWORDS = set(
    "的 了 是 在 和 与 或 哪 什么 哪个 哪些 如何 怎么 为什么 是否 这 那 一个 一种 请 我 你 他 它 吗 呢 吧 地 得 "
    "the a an of in on at to for and or is are was were be by with from which what who how when "
    "where does do did that this it as".split()
)
# Match jieba-rs' connected ASCII tokens, including its search-mode component words.
# Python jieba otherwise splits half-ounce and A.H.G. before FTS receives them.
jieba.setLogLevel(logging.WARNING)
jieba.finalseg.re_skip = re.compile(r"([a-zA-Z0-9]+(?:[._-][a-zA-Z0-9]+)*%?)")
TOKENIZER = jieba.Tokenizer()
COLLATOR = Collator()
NAME_WORD = r"(?:\p{Lu}\.|\p{Lu}[\p{L}\p{M}]*(?:[-’']\p{Lu}[\p{L}\p{M}]*)*)"
NAMES = regex.compile(
    rf"(?<![\p{{L}}\p{{N}}]){NAME_WORD}(?:[ \t]+{NAME_WORD})+(?![\p{{L}}\p{{N}}])"
)
SENTENCES = regex.compile(r"(?<!\b[A-Z])[.!?]\s+|[。！？]+\s*|\n+")


HAS_SEPARATOR = re.compile(r"[._-]")
SEPARATORS = re.compile(r"[._-]")
HAS_LATIN = re.compile(r"[a-z]")
HAS_CONTENT = regex.compile(r"[\p{L}\p{N}]")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@lru_cache(maxsize=8192)
def _cached_tokenize(text: str) -> tuple[str, ...]:
    words = []
    for word in TOKENIZER.cut(unicodedata.normalize("NFKC", text).lower(), HMM=True):
        if HAS_SEPARATOR.search(word):
            words.extend(
                part
                for part in SEPARATORS.split(word)
                if len(part) >= 2 and HAS_LATIN.search(part)
            )
        for length in (2, 3):
            if len(word) > length:
                words.extend(
                    word[i : i + length]
                    for i in range(len(word) - length + 1)
                    if TOKENIZER.FREQ.get(word[i : i + length], 0) > 0
                )
        words.append(word)
    return tuple(
        word for word in words if HAS_CONTENT.search(word) and word not in STOPWORDS
    )


def tokenize(text: str) -> list[str]:
    return list(_cached_tokenize(text))


def quote_term(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def match_query(query: str) -> str:
    return " OR ".join(
        quote_term(term) for term in list(dict.fromkeys(tokenize(query)))[:80]
    )


def fold_latin_accents(text):
    characters, latin_base = [], False
    for char in unicodedata.normalize("NFKD", text):
        if unicodedata.combining(char):
            if not latin_base:
                characters.append(char)
        else:
            characters.append(char)
            latin_base = "LATIN" in unicodedata.name(char, "")
    return unicodedata.normalize("NFKC", "".join(characters))


def diversity_tokens(text):
    return re.findall(r"[^\W_]+", text.casefold())


def rank_dpp(candidates, idf):
    vectors = []
    for candidate in candidates:
        vector = {
            term: (1 + math.log(count)) * idf[term]
            for term, count in Counter(diversity_tokens(candidate["content"])).items()
        }
        norm = math.sqrt(sum(value * value for value in vector.values()))
        vectors.append({term: value / norm for term, value in vector.items()})
    scores = [c["jev_score"] for c in candidates]
    if not scores:
        return candidates
    kernel = [
        [
            scores[i]
            * scores[j]
            * sum(value * vectors[j].get(term, 0) for term, value in vectors[i].items())
            for j in range(len(candidates))
        ]
        for i in range(len(candidates))
    ]
    residuals = [score * score for score in scores]
    for i, value in enumerate(residuals):
        kernel[i][i] = value
    tolerance = len(candidates) * math.ulp(max(residuals))
    coefficients, remaining, order = (
        [[] for _ in candidates],
        list(range(len(candidates))),
        [],
    )
    while remaining:
        winner = max(remaining, key=lambda i: residuals[i])
        if residuals[winner] <= tolerance:
            order.extend(remaining)
            break
        order.append(winner)
        remaining.remove(winner)
        denominator = math.sqrt(residuals[winner])
        for i in remaining:
            projection = (
                kernel[winner][i]
                - sum(
                    a * b
                    for a, b in zip(coefficients[winner], coefficients[i], strict=True)
                )
            ) / denominator
            coefficients[i].append(projection)
            residuals[i] = max(0.0, residuals[i] - projection * projection)
    return [candidates[i] for i in order]


def chunk_text(content: str, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> list[str]:
    """Return complete sentence chunks with heading-scoped context overlap."""
    return [chunk["content"] for chunk in chunk_document(content, chunk_chars)]
