---
title: Replacement Rule
type: doc
status: active
---

# Replacement Rule

A module implementation can be replaced when:

1. It accepts the same input contract.
2. It emits the same output contract.
3. It preserves lifecycle semantics.
4. It preserves error semantics.
5. It does not read or write state outside its ownership boundary.
6. It passes contract tests for that module.

If a replacement needs new data, update the contract first. Do not smuggle data through untyped dictionaries unless the contract explicitly marks it as extension metadata.
