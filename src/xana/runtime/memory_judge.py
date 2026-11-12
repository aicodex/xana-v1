"""JEV Noul transport; instructions are an explicit, host-approved dependency.

Protocol/batching: SAG-Benchmark bb7f8275 sag2.py::JevClient.score (MIT).
Bounded retry/cancellation: SAG-Wiki 61f63c66 models.rs::request_with_runtime.
No scoring instruction or endpoint is supplied implicitly.
"""

from __future__ import annotations

import asyncio
import json
import math

import httpx
import tiktoken

from xana.memory.contracts import (
    EvidenceCandidate,
    EvidenceCriterion,
    EvidenceJudgment,
    EvidenceScore,
)


class JevEvidenceJudge:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        endpoint: str,
        model: str,
        api_key: str,
        criterion: EvidenceCriterion,
        timeout_s: float,
        concurrency: int,
        max_attempts: int,
        context_tokens: int = 32000,
        batch_tokens: int = 64000,
    ) -> None:
        url = httpx.URL(endpoint)
        if url.scheme not in {"https", "http"} or not url.host or not model.strip():
            raise ValueError("valid JEV endpoint and model are required")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("JEV timeout must be positive")
        if any(
            type(value) is not int or value <= 0
            for value in (concurrency, max_attempts, context_tokens, batch_tokens)
        ):
            raise ValueError("JEV limits must be positive integers")
        path = url.path.rstrip("/")
        self._endpoint = url.copy_with(
            path=path
            if path.endswith(("/systemone", "/decisions"))
            else path + "/systemone"
        )
        self._client, self._model, self._key = client, model, api_key
        self._criterion, self._timeout, self._attempts = (
            criterion,
            timeout_s,
            max_attempts,
        )
        self._context_tokens, self._batch_tokens = context_tokens, batch_tokens
        self._semaphore = asyncio.Semaphore(concurrency)
        self._encoding = tiktoken.get_encoding("o200k_base")

    async def __call__(
        self,
        query: str,
        candidates: tuple[EvidenceCandidate, ...],
        evidence: tuple[EvidenceCandidate, ...],
    ) -> EvidenceJudgment:
        if len({candidate.id for candidate in candidates}) != len(candidates):
            raise ValueError("duplicate JEV candidate IDs")
        state_data = {
            "original_question": query,
            "previously_retrieved_evidence": [
                {
                    "title": item.title,
                    "passage": item.content,
                    **(
                        {"previous_jev_score": item.previous_score}
                        if item.previous_score is not None
                        else {}
                    ),
                }
                for item in evidence
            ],
        }
        if self._criterion.score_note and any(
            item.previous_score is not None for item in evidence
        ):
            state_data["evidence_score_note"] = self._criterion.score_note
        state = _json(state_data)
        state_size = self._tokens(state) + 1024
        batches, questions, size = [], {}, state_size
        for item in candidates:
            key = f"candidate_{item.id}"
            question = {
                "type": "noul",
                "instructions": {
                    "question": self._criterion.question,
                    "candidate_title": item.title,
                    "candidate_passage": item.content,
                },
                "criteria": {
                    "true": self._criterion.when_true,
                    "false": self._criterion.when_false,
                },
            }
            count = self._tokens(_json(question)) + 64
            if state_size + count > min(self._context_tokens, self._batch_tokens):
                raise ValueError(
                    "JEV input exceeds configured token limit; source text was not truncated"
                )
            if questions and size + count > self._batch_tokens:
                batches.append(questions)
                questions, size = {}, state_size
            questions[key] = question
            size += count
        if questions:
            batches.append(questions)
        tasks = [asyncio.create_task(self._batch(state, batch)) for batch in batches]
        try:
            results = await asyncio.wait_for(asyncio.gather(*tasks), self._timeout)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return EvidenceJudgment(
            tuple(score for scores, _, _, _ in results for score in scores),
            self._model,
            sum(row[1] for row in results),
            sum(row[2] for row in results),
            sum(row[3] for row in results),
        )

    async def _batch(
        self, state: str, questions: dict
    ) -> tuple[tuple[EvidenceScore, ...], int, int, int]:
        async with self._semaphore:
            for attempt in range(1, self._attempts + 1):
                try:
                    response = await self._client.post(
                        self._endpoint,
                        json={
                            "model": self._model,
                            "state": state,
                            "questions": questions,
                        },
                        headers={"Authorization": f"Bearer {self._key}"}
                        if self._key
                        else {},
                        timeout=self._timeout,
                    )
                except (httpx.TimeoutException, httpx.ConnectError):
                    if attempt == self._attempts:
                        raise RuntimeError(
                            "JEV request failed after bounded retries"
                        ) from None
                else:
                    if response.is_success:
                        data = response.json()
                        if not isinstance(data, dict):
                            raise ValueError("JEV response must be an object")
                        answers = data.get("answers")
                        if not isinstance(answers, dict) or set(answers) != set(
                            questions
                        ):
                            raise ValueError(
                                "JEV response must align exactly with requested candidate IDs"
                            )
                        scores = []
                        for key, answer in answers.items():
                            value = (
                                answer.get("noul") if isinstance(answer, dict) else None
                            )
                            if (
                                not isinstance(answer, dict)
                                or answer.get("type") != "noul"
                                or type(value) not in (float, int)
                                or not math.isfinite(value)
                                or not 0 <= value <= 1
                            ):
                                raise ValueError("invalid JEV Noul decision")
                            scores.append(
                                EvidenceScore(key[len("candidate_") :], value)
                            )
                        usage = data.get("usage", {})
                        if not isinstance(usage, dict):
                            raise ValueError("invalid JEV usage")
                        tokens = tuple(
                            usage.get(key, 0)
                            for key in ("input_tokens", "output_tokens")
                        )
                        if any(type(value) is not int or value < 0 for value in tokens):
                            raise ValueError("invalid JEV usage")
                        return tuple(scores), *tokens, attempt
                    if (
                        response.status_code
                        not in {408, 429, 499, 500, 502, 503, 504, 529}
                        or attempt == self._attempts
                    ):
                        raise RuntimeError(f"JEV returned HTTP {response.status_code}")
                await asyncio.sleep(min(2 ** (attempt - 1), 30))
        raise RuntimeError("JEV request did not complete")

    def _tokens(self, value: str) -> int:
        return len(self._encoding.encode(value, disallowed_special=()))


def _json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
