# SAG2 source provenance

Derived from Zleap-AI/SAG-Benchmark, `sag2/scripts`, commit
`bb7f82751ba1576219f9d9ebd021128603d130c3` (MIT).
Copyright (c) 2026 Zleap Team; see LICENSE.
SQLite headers carry their original public-domain notices.

Adaptations: package-relative imports; separate text processing, corpus indexing,
and retrieval responsibilities; inject an async scoring callable rather than the
benchmark HTTP client; use a Xana-specific native compilation cache directory.
Restore only the explicitly named lexical statistics from the index cache.
No benchmark runner, API credential handling, scorer prompts, datasets, local
model weights or evaluation outputs are bundled. The lexical ranking,
normalization, term expansion, seed retention and diversity ordering are retained.

The index class is over the project file review threshold because it preserves
one upstream index/query responsibility; source-level regression fixtures are
required before changing its ranking or tokenizer behavior.
