"""Adapted from Zleap SAG2 bb7f8275 (MIT); see NOTICE.md for changes."""

from __future__ import annotations

import hashlib, json, math, re, sqlite3, struct, threading, unicodedata
from collections import Counter, OrderedDict, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import nullcontext
from itertools import islice
from multiprocessing import get_context
from pathlib import Path
import regex
from .chunking import (
    DEFAULT_CHUNK_CHARS,
    CHUNK_POLICY,
    chunk_document,
    source_content,
    utf16_length,
)
from .ranking import load_rank_extension
from .text import (
    INDEX_SCHEMA,
    QUERY_WORKERS,
    QUERY_CACHE_ENTRIES,
    TOKENIZER,
    COLLATOR,
    SENTENCES,
    NAMES,
    tokenize,
    compact_json,
    diversity_tokens,
    fold_latin_accents,
    quote_term,
    match_query,
)


def _initialize_index_worker() -> None:
    TOKENIZER.initialize()


def _prepare_index_document(item: tuple[int, dict, int]) -> tuple:
    position, doc, chunk_chars = item
    title, content = doc["title"].strip(), doc["text"]
    groups = chunk_document(content, chunk_chars, doc.get("path", "document.txt"))
    return (
        position + 1,
        title,
        f"{title}\n{content}",
        str(doc.get("idx", position)).rsplit("_", 1)[0],
        " ".join(tokenize(title)),
        [
            (
                chunk,
                " ".join(tokenize(chunk["heading"])),
                " ".join(tokenize(chunk["content"])),
            )
            for chunk in groups
        ],
    )


class CorpusIndex:
    """One cached SQLite FTS5 index per corpus."""

    def __init__(
        self,
        path: Path,
        corpus: list[dict],
        fingerprint: str,
        chunk_chars: int = DEFAULT_CHUNK_CHARS,
        *,
        index_workers: int = 1,
        source_dir: Path | None = None,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.source_dir = source_dir.resolve() if source_dir is not None else None
        signature = compact_json(
            [
                fingerprint,
                chunk_chars,
                INDEX_SCHEMA,
                str(self.source_dir) if self.source_dir else None,
                CHUNK_POLICY,
                *([] if self.source_dir else ["plain-text-default"]),
            ]
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (signature TEXT NOT NULL)")
        cached = self.db.execute("SELECT signature FROM metadata").fetchone()
        if cached is None or cached[0] != signature:
            pool_context = (
                ProcessPoolExecutor(
                    index_workers,
                    mp_context=get_context("spawn"),
                    initializer=_initialize_index_worker,
                )
                if index_workers > 1
                else nullcontext(None)
            )
            with pool_context as pool, self.db:
                self.db.executescript(
                    "DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS chunk_fts; DROP TABLE IF EXISTS source_files;"
                    "CREATE TABLE chunks (id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, "
                    "title TEXT NOT NULL, heading TEXT NOT NULL, "
                    + (
                        "start_char INTEGER NOT NULL, end_char INTEGER NOT NULL, "
                        if self.source_dir
                        else "content TEXT NOT NULL, full_doc TEXT NOT NULL, "
                    )
                    + "spans TEXT NOT NULL, characters INTEGER NOT NULL, source_id TEXT NOT NULL);"
                    "CREATE INDEX chunks_document ON chunks(document_id);"
                    "CREATE VIRTUAL TABLE chunk_fts USING fts5(title,heading,body"
                    + (",content=''" if self.source_dir else "")
                    + ");"
                    + (
                        "CREATE TABLE source_files (document_id INTEGER PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL);"
                        if self.source_dir
                        else ""
                    )
                )
                seen = set()

                def documents():
                    for position, doc in enumerate(corpus):
                        if self.source_dir:
                            raw = (self.source_dir / doc["path"]).read_bytes()
                            if hashlib.sha256(raw).hexdigest() != doc["sha256"]:
                                raise ValueError(
                                    f"Source changed since indexing: {doc['path']}"
                                )
                            doc = {**doc, "text": raw.decode("utf-8")}
                        title, content = doc["title"].strip(), doc["text"]
                        if not title or not content.strip():
                            raise ValueError(
                                f"Corpus row {position} needs non-empty title and text"
                            )
                        full_doc = f"{title}\n{content}"
                        if full_doc in seen:
                            continue
                        seen.add(full_doc)
                        yield position, doc, chunk_chars

                inputs = documents()
                chunk_id = 0
                while batch := list(islice(inputs, 256)):
                    prepared = (
                        pool.map(_prepare_index_document, batch, chunksize=16)
                        if pool
                        else map(_prepare_index_document, batch)
                    )
                    chunks, tokens = [], []
                    if self.source_dir:
                        self.db.executemany(
                            "INSERT INTO source_files VALUES(?,?,?)",
                            (
                                (position + 1, doc["path"], doc["sha256"])
                                for position, doc, _ in batch
                            ),
                        )
                    for (
                        document_id,
                        title,
                        full_doc,
                        source_id,
                        indexed_title,
                        groups,
                    ) in prepared:
                        for chunk, indexed_heading, indexed_body in groups:
                            body, spans = chunk["content"], chunk["spans"]
                            chunk_id += 1
                            chunks.append(
                                (chunk_id, document_id, title, chunk["heading"])
                                + (
                                    (spans[0][0], spans[-1][1])
                                    if self.source_dir
                                    else (body, full_doc)
                                )
                                + (
                                    compact_json(spans),
                                    utf16_length(body),
                                    source_id,
                                )
                            )
                            tokens.append(
                                (chunk_id, indexed_title, indexed_heading, indexed_body)
                            )
                    self.db.executemany(
                        "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?)", chunks
                    )
                    self.db.executemany(
                        "INSERT INTO chunk_fts(rowid,title,heading,body) VALUES(?,?,?,?)",
                        tokens,
                    )
                self.db.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('optimize')")
                self.db.execute("DELETE FROM metadata")
                self.db.execute("INSERT INTO metadata VALUES(?)", (signature,))
        self._load_statistics(corpus, chunk_chars, signature)
        self._initialize_queries()
        self.name_pattern = (
            regex.compile(
                r"(?<![\p{L}\p{N}])(?:"
                + "|".join(
                    regex.escape(key)
                    for key in sorted(
                        self.aliases, key=lambda value: (-len(value), value)
                    )
                )
                + r")(?![\p{L}\p{N}])",
                regex.IGNORECASE,
            )
            if self.aliases
            else None
        )

    def _load_statistics(self, corpus, chunk_chars, signature):
        # Keep corpus-wide work in the index, rather than repeating it per process.
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS index_statistics "
            "(signature TEXT PRIMARY KEY, data TEXT NOT NULL)"
        )
        cached = self.db.execute(
            "SELECT data FROM index_statistics WHERE signature=?", (signature,)
        ).fetchone()
        data = json.loads(cached[0]) if cached is not None else None
        if data is not None and (
            self.source_dir is None or data.get("source_statistics_complete")
        ):
            self.frequencies = data["frequencies"]
            self.total = data["total"]
            self.document_count = data["document_count"]
            self.title_phrases = data["title_phrases"]
            self.aliases = data["aliases"]
            self.diversity_idf = data["diversity_idf"]
            self.title_phrases = {tuple(words) for words in self.title_phrases}
            return
        self.db.execute(
            "CREATE VIRTUAL TABLE temp.vocab USING fts5vocab(main,chunk_fts,'col')"
        )
        self.frequencies = {
            row["term"]: row["doc"]
            for row in self.db.execute(
                "SELECT term,doc FROM temp.vocab WHERE col='body'"
            )
        }
        self.total = self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        self.document_count = self.db.execute(
            "SELECT COUNT(DISTINCT document_id) FROM chunks"
        ).fetchone()[0]
        if self.source_dir is not None:
            corpus = (
                {
                    "title": row["title"],
                    "text": (self.source_dir / row["path"])
                    .read_bytes()
                    .decode("utf-8"),
                    "path": row["path"],
                }
                for row in self.db.execute(
                    "SELECT path, (SELECT title FROM chunks WHERE document_id = "
                    "source_files.document_id LIMIT 1) AS title FROM source_files ORDER BY document_id"
                )
            )
        self.title_phrases = set()
        names = defaultdict(set)
        frequencies = Counter()
        count = 0
        for row in corpus:
            words = tuple(tokenize(row["title"]))
            if len(words) >= 2:
                self.title_phrases.add(words)
            name = unicodedata.normalize("NFKC", row["title"]).casefold()
            names[fold_latin_accents(name)].add(name)
            terms = set(diversity_tokens(row["text"]))
            for chunk in chunk_document(row["text"], chunk_chars, row.get("path")):
                terms.update(diversity_tokens(chunk["content"]))
            frequencies.update(terms)
            count += 1
        self.aliases = {
            alias: next(iter(values))
            for alias, values in names.items()
            if len(values) == 1 and alias not in values
        }
        self.diversity_idf = {
            term: math.log((1 + count) / (1 + frequency)) + 1
            for term, frequency in frequencies.items()
        }

        data = {
            "frequencies": self.frequencies,
            "total": self.total,
            "document_count": self.document_count,
            "title_phrases": sorted(self.title_phrases),
            "aliases": self.aliases,
            "diversity_idf": self.diversity_idf,
            "source_statistics_complete": self.source_dir is not None,
        }
        with self.db:
            self.db.execute("DELETE FROM index_statistics")
            self.db.execute(
                "INSERT INTO index_statistics VALUES(?,?)",
                (signature, compact_json(data)),
            )

    def repair_names(self, text, excluded):
        if self.name_pattern is None:
            return text

        def replace(match):
            source = match.group()
            if self.has_match(quote_term(" ".join(tokenize(source))), excluded):
                return source
            target = self.aliases[source.casefold()]
            return (
                target
                if self.has_match(quote_term(" ".join(tokenize(target))), excluded)
                else source
            )

        return self.name_pattern.sub(replace, text)

    def match_query(self, query):
        return match_query(self.repair_names(query, set()))

    def _initialize_queries(self) -> None:
        self.db.execute("PRAGMA mmap_size=2147483648")
        load_rank_extension(self.db)
        self._rank_stats = {}
        self._query_path = Path(self.db.execute("PRAGMA database_list").fetchone()[2])
        self._query_cache = OrderedDict()
        self._query_epoch = None
        self._query_cache_hits = 0
        self._query_cache_misses = 0
        self._query_pools = None
        self._query_local = threading.local()
        self._query_readers = []

    def close(self) -> None:
        if self._query_pools is not None:
            for pool in self._query_pools:
                pool.shutdown(wait=True)
            for reader in self._query_readers:
                reader.db.close()
        self.db.close()

    def has_match(self, expression, excluded):
        return (
            self.db.execute(
                "SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ? "
                "AND rowid NOT IN (SELECT value FROM json_each(?)) LIMIT 1",
                (expression, compact_json(sorted(excluded))),
            ).fetchone()
            is not None
        )

    def retrieve(
        self, expression: str, excluded: set[int], limit: int, term: str | None = None
    ) -> list[dict]:
        own_transaction = not self.db.in_transaction
        if own_transaction:
            self.db.execute("BEGIN")
        try:
            return self._retrieve_snapshot(expression, excluded, limit, term)
        finally:
            if own_transaction:
                self.db.rollback()

    def _retrieve_snapshot(
        self, expression: str, excluded: set[int], limit: int, term: str | None
    ) -> list[dict]:
        # data_version pins the read snapshot and detects writes by other connections.
        epoch = (
            self.db.execute("PRAGMA data_version").fetchone()[0],
            self.db.total_changes,
        )
        if epoch != self._query_epoch:
            self._query_cache.clear()
            if self._query_epoch is not None:
                self._rank_stats.clear()
            self._query_epoch = epoch
        key = (expression, tuple(sorted(excluded)), limit, term)
        if expression not in self._rank_stats:
            row = self.db.execute(
                "SELECT sag_stats(chunk_fts) FROM chunk_fts WHERE chunk_fts MATCH ? LIMIT 1",
                (expression,),
            ).fetchone()
            self._rank_stats[expression] = row[0] if row else b""
        query = (
            f"({expression}) AND {quote_term(term)}" if term is not None else expression
        )
        sql = (
            "WITH top_hits AS MATERIALIZED ("
            "SELECT rowid,sag_bm25(chunk_fts,?,2,2,1) AS score FROM chunk_fts "
            "WHERE chunk_fts MATCH ? "
            "AND rowid NOT IN (SELECT value FROM json_each(?)) "
            "ORDER BY score,rowid LIMIT ?) SELECT rowid,score FROM top_hits ORDER BY score,rowid"
        )
        params = [self._rank_stats[expression], query, compact_json(sorted(excluded))]
        if key in self._query_cache:
            self._query_cache_hits += 1
            ranked = self._query_cache[key]
            self._query_cache.move_to_end(key)
        else:
            self._query_cache_misses += 1
            phrases = re.findall(r'"((?:""|[^"])*)"', expression)
            packed = self._rank_stats[expression]
            idfs = struct.unpack(f"{len(packed) // 8}d", packed)[1:]
            pruned = None
            if (
                term is None
                and limit >= 100
                and len(phrases) >= 8
                and len(idfs) == len(phrases)
            ):
                selected = sorted(range(len(idfs)), key=lambda i: -idfs[i])[
                    : math.ceil(len(idfs) * 0.55)
                ]
                subset = " OR ".join('"' + phrases[i] + '"' for i in selected)
                restricted = sql.replace(
                    "ORDER BY score,rowid LIMIT ?)",
                    "AND +rowid IN (SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ?) ORDER BY score,rowid LIMIT ?)",
                )
                possible = tuple(
                    tuple(row)
                    for row in self.db.execute(restricted, [*params, subset, limit])
                )
                bound = 2.2 * sum(
                    idf for i, idf in enumerate(idfs) if i not in selected
                )
                if len(possible) == limit and -possible[-1][1] > bound:
                    pruned = possible
            ranked = (
                pruned
                if pruned is not None
                else tuple(tuple(row) for row in self.db.execute(sql, [*params, limit]))
            )
            self._query_cache[key] = ranked
            if len(self._query_cache) > QUERY_CACHE_ENTRIES:
                self._query_cache.popitem(last=False)
        records = {
            row["id"]: dict(row)
            for row in self.db.execute(
                "SELECT * FROM chunks WHERE id IN (SELECT value FROM json_each(?))",
                (compact_json([identifier for identifier, _ in ranked]),),
            )
        }
        hits = [{**records[identifier], "score": score} for identifier, score in ranked]
        if self.source_dir:
            documents = {}
            for hit in hits:
                document_id = hit["document_id"]
                if document_id not in documents:
                    source = self.db.execute(
                        "SELECT path,sha256 FROM source_files WHERE document_id=?",
                        (document_id,),
                    ).fetchone()
                    path = self.source_dir / source["path"]
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != source["sha256"]:
                        raise ValueError(
                            f"Source changed since indexing; rebuild the index: {path}"
                        )
                    documents[document_id] = raw.decode("utf-8")
                hit["content"] = source_content(
                    documents[document_id], json.loads(hit["spans"])
                )
        return hits

    def _retrieve_batch(self, requests: list[tuple]) -> list[tuple[int, list[dict]]]:
        if not hasattr(self._query_local, "reader"):
            reader = CorpusIndex.__new__(CorpusIndex)
            reader.db = sqlite3.connect(
                self._query_path.as_uri() + "?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            reader.db.row_factory = sqlite3.Row
            reader.source_dir = self.source_dir
            reader._initialize_queries()
            reader._rank_stats = self._rank_stats
            self._query_readers.append(reader)
            self._query_local.reader = reader
        reader = self._query_local.reader
        return [(position, reader.retrieve(*request)) for position, request in requests]

    def _frequency_batch(self, terms):
        self._retrieve_batch([])
        db = self._query_local.reader.db
        return {
            term: db.execute(
                "SELECT COUNT(*) FROM chunk_fts WHERE chunk_fts MATCH ?",
                (f"body : {quote_term(term)}",),
            ).fetchone()[0]
            for term in terms
        }

    def frequency(self, term: str) -> int:
        if term not in self.frequencies:
            self.frequencies[term] = self.db.execute(
                "SELECT COUNT(*) FROM chunk_fts WHERE chunk_fts MATCH ?",
                (f"body : {quote_term(term)}",),
            ).fetchone()[0]
        return self.frequencies[term]

    def expansion_terms(self, query: str, seeds: list[dict], limit: int) -> list[dict]:
        unknown = set()
        for seed in seeds:
            for text in SENTENCES.split(seed["content"]):
                unknown.update(tokenize(text))
                unknown.update(" ".join(tokenize(m[0])) for m in NAMES.finditer(text))
        unknown.difference_update(self.frequencies)
        if len(unknown) > 1:
            if self._query_pools is None:
                self._query_pools = [
                    ThreadPoolExecutor(1, thread_name_prefix=f"sag-bm25-{i}")
                    for i in range(QUERY_WORKERS)
                ]
            terms = sorted(unknown)
            n = min(len(terms), QUERY_WORKERS)
            futures = [
                self._query_pools[i].submit(self._frequency_batch, terms[i::n])
                for i in range(n)
            ]
            for future in futures:
                self.frequencies.update(future.result())
        original = set(tokenize(query))
        max_score = max([0, *(seed["jev_score"] for seed in seeds)])
        ranked: dict[str, dict] = {}
        for seed in seeds:
            sentences = []
            names = {}
            for text in SENTENCES.split(seed["content"]):
                matches = list(NAMES.finditer(text))
                ungrouped, end = "", 0
                sentence_names = []
                for match in matches:
                    term = " ".join(tokenize(match[0]))
                    names.setdefault(term, match[0])
                    sentence_names.append(term)
                    ungrouped += text[end : match.start()] + " "
                    end = match.end()
                words = tokenize(ungrouped + text[end:]) + sentence_names
                sentences.append((text, words))
            counts = Counter(word for _, words in sentences for word in words)
            context_scores: dict[str, float] = {}
            for text, words in sentences:
                # Match JavaScript reduce's left-to-right addition. Python 3.12+
                # sum uses compensated addition, which can change near-tied terms.
                context = 0.0
                for term in dict.fromkeys(tokenize(text)):
                    if term in original:
                        context += math.log(
                            1
                            + (self.total - self.frequency(term) + 0.5)
                            / (self.frequency(term) + 0.5)
                        )
                for word in words:
                    context_scores[word] = max(context_scores.get(word, 0), context)
            weight = 0.5 + 0.5 * (seed["jev_score"] / max_score if max_score > 0 else 0)
            for term, tf in counts.items():
                frequency = self.frequency(term)
                if all(word in original for word in tokenize(term)) or frequency == 0:
                    continue
                context = context_scores[term]
                idf = math.log(1 + (self.total - frequency + 0.5) / (frequency + 0.5))
                unweighted = (1 + math.log(tf)) * idf * (1 + context)
                score = unweighted * weight
                if term not in ranked or score > ranked[term]["score"]:
                    ranked[term] = {
                        "term": term,
                        "score": score,
                        "unweighted_score": unweighted,
                        "seed_weight": weight,
                        "seed_jev_score": seed["jev_score"],
                        "term_frequency": tf,
                        "document_frequency": frequency,
                        "query_context_score": context,
                        "seed_document_id": seed["document_id"],
                        "kind": "name_phrase" if term in names else "word",
                        **({"name": names[term]} if term in names else {}),
                    }
        return sorted(
            ranked.values(),
            key=lambda item: (-item["score"], COLLATOR.sort_key(item["term"])),
        )[:limit]

    def retrieve_expansion(
        self, expression: str, terms: list[dict], excluded: set[int], limit: int
    ) -> tuple[list[dict], list[dict]]:
        resolved = []
        for item in terms:
            term = dict(item)
            if not self.has_match(quote_term(term["term"]), excluded):
                repaired = self.repair_names(item.get("name", item["term"]), excluded)
                if repaired != item.get("name", item["term"]):
                    term["term"] = " ".join(tokenize(repaired))
            if term["kind"] == "name_phrase" and not self.has_match(
                quote_term(term["term"]), excluded
            ):
                words = term["term"].split()
                matches = [
                    tuple(words[a:b])
                    for a in range(len(words))
                    for b in range(a + 2, len(words) + 1)
                    if tuple(words[a:b]) in self.title_phrases
                ]
                if matches:
                    grounded = " ".join(
                        min(matches, key=lambda words: (-len(words), words))
                    )
                    if self.has_match(quote_term(grounded), excluded):
                        term["term"] = grounded
            resolved.append(term)
        terms = resolved
        ranking = (
            " OR ".join(quote_term(term["term"]) for term in terms)
            + f" OR ({expression})"
        )
        if ranking not in self._rank_stats:
            row = self.db.execute(
                "SELECT sag_stats(chunk_fts) FROM chunk_fts WHERE chunk_fts MATCH ? LIMIT 1",
                (ranking,),
            ).fetchone()
            self._rank_stats[ranking] = row[0] if row else b""
        if len(terms) < 2 or QUERY_WORKERS == 1:
            lists = [
                self.retrieve(ranking, excluded, limit, term["term"]) for term in terms
            ]
        else:
            if self._query_pools is None:
                # Fixed worker assignment keeps repeated queries in the same bounded cache.
                self._query_pools = [
                    ThreadPoolExecutor(1, thread_name_prefix=f"sag-bm25-{i}")
                    for i in range(QUERY_WORKERS)
                ]
            requests = [
                (i, (ranking, excluded, limit, term["term"]))
                for i, term in enumerate(terms)
            ]
            count = min(len(self._query_pools), len(requests))
            futures = [
                self._query_pools[i].submit(self._retrieve_batch, requests[i::count])
                for i in range(count)
            ]
            lists = [None] * len(terms)
            for future in futures:
                for position, rows in future.result():
                    lists[position] = rows
        hits, seen = [], set()
        queries = [
            {
                "term": term["term"],
                "term_rank": i + 1,
                "kind": term["kind"],
                "match_expression": quote_term(term["term"]),
                "ranking_expression": ranking,
                "retrieved_count": len(lists[i]),
                "selected": [],
            }
            for i, term in enumerate(terms)
        ]
        for rank in range(limit):
            for i, rows in enumerate(lists):
                if rank >= len(rows) or rows[rank]["id"] in seen:
                    continue
                hit = {
                    **rows[rank],
                    "expansion_term": terms[i]["term"],
                    "expansion_term_rank": i + 1,
                    "expansion_bm25_rank": rank + 1,
                }
                seen.add(hit["id"])
                hits.append(hit)
                queries[i]["selected"].append(
                    {
                        "document_id": hit["document_id"],
                        "bm25_rank": rank + 1,
                        "bm25_score": hit["score"],
                    }
                )
                if len(hits) == limit:
                    return hits, queries
        return hits, queries
