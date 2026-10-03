"""Drafts a resolution from the retrieved sources, then checks it before returning it.

Pipeline for one request:
  1. Group sources that say the same thing, keep the best few, shorten them.
  2. Ask the language model for a JSON answer in which every step cites a source ID.
  3. Check the answer: citations must be real, each step must be supported by its source,
     and steps the customer already tried are flagged.
  4. If the model is unavailable or its answer is unusable, fall back to quoting the
     resolution steps of the best source directly. The service always returns something useful.
"""

from __future__ import annotations

import time

from prometheus_client import Counter, Histogram

from libs.common.llm_client import LLMOutputError, LLMUnavailableError
from libs.common.pii import mask_pii
from services.generation.config import Settings
from services.generation.prompt import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    answer_schema,
    build_user_prompt,
    clean_step_text,
)
from services.generation.sources import SourceGroup, group_sources, resolution_steps, shorten, statements

ANSWERS = Counter("generation_answers_total", "Answers produced, by how they were made", ["mode"])
FALLBACKS = Counter("generation_fallbacks_total", "Times the model's answer could not be used", ["reason"])
DROPPED_STEPS = Counter("generation_dropped_steps_total", "Steps removed because they cited no real source")
UNSUPPORTED_STEPS = Counter("generation_unsupported_steps_total", "Steps not backed by their cited source")
LLM_SECONDS = Histogram(
    "generation_llm_seconds",
    "Time the language model took to answer",
    buckets=(1, 2, 5, 10, 20, 30, 45, 60, 90, 120, 180),
)
TOKENS = Counter("generation_tokens_total", "Tokens read and written by the model", ["kind"])
# Start every known label at 0, so dashboards show a zero instead of nothing and the
# first event is counted. (Prometheus cannot see a rise from "does not exist" to 1.)
for _mode in ("llm", "extractive"):
    ANSWERS.labels(_mode)
for _reason in ("llm_disabled", "llm_unavailable", "invalid_output"):
    FALLBACKS.labels(_reason)


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class Generator:
    def __init__(self, llm, embedder, settings: Settings) -> None:
        self._llm = llm  # may be None when the model is switched off
        self._embedder = embedder
        self._settings = settings

    def llm_ready(self) -> bool:
        return self._llm is not None and self._llm.ready()

    def embedder_ready(self) -> bool:
        return self._embedder.ready()

    # ---- step 2: ask the model -------------------------------------------------------------

    def _ask_model(self, complaint: str, triage: dict | None, groups: list[SourceGroup]) -> tuple[dict, dict]:
        settings = self._settings
        shown = [
            {**group.primary, "content": shorten(group.primary["content"], settings.max_source_chars)}
            for group in groups
        ]
        return self._llm.chat_json(
            system=SYSTEM_PROMPT.format(max_steps=settings.max_steps),
            user=build_user_prompt(complaint, triage, shown),
            schema=answer_schema([group.primary["id"] for group in groups]),
            max_tokens=settings.max_output_tokens,
            temperature=settings.temperature,
        )

    # ---- step 3: check the answer ----------------------------------------------------------

    def _check(self, answer: dict, groups: list[SourceGroup]) -> tuple[list[dict], int]:
        """Keep only steps with real citations, and score how well each is supported."""
        settings = self._settings
        by_id = {group.primary["id"]: group for group in groups}
        kept, dropped = [], 0
        for step in answer.get("steps", [])[: settings.max_steps]:
            text = clean_step_text(str(step.get("text", "")))
            cited = [by_id[c] for c in dict.fromkeys(step.get("citations", [])) if c in by_id]
            if not text or not cited:
                dropped += 1  # no real source behind it: do not show it to the agent
                continue
            kept.append({"text": text, "groups": cited})
        if not kept:
            return [], dropped

        tried = [str(item).strip() for item in answer.get("already_tried", []) if str(item).strip()]
        source_lines = {gid: statements(group.primary["content"]) for gid, group in by_id.items()}
        flat_lines = [line for lines in source_lines.values() for line in lines]
        vectors = iter(self._embedder.embed([s["text"] for s in kept] + tried + flat_lines)["dense"])
        step_vectors = [next(vectors) for _ in kept]
        tried_vectors = [next(vectors) for _ in tried]
        line_vectors = {gid: [next(vectors) for _ in lines] for gid, lines in source_lines.items()}

        steps = []
        for number, (step, vector) in enumerate(zip(kept, step_vectors, strict=True), start=1):
            support = max(
                (
                    _dot(vector, line)
                    for group in step["groups"]
                    for line in line_vectors[group.primary["id"]]
                ),
                default=0.0,
            )
            repeats = any(_dot(vector, t) >= settings.repeat_threshold for t in tried_vectors)
            steps.append(
                {
                    "n": number,
                    "text": step["text"],
                    # Credit every source that says the same thing, not only the one the model saw.
                    "citations": [source_id for group in step["groups"] for source_id in group.ids],
                    "support": round(support, 3),
                    "verified": support >= settings.support_threshold,
                    "repeats_already_tried": repeats,
                }
            )
        return steps, dropped

    # ---- step 4: fallback --------------------------------------------------------------------

    @staticmethod
    def _extractive(groups: list[SourceGroup]) -> dict | None:
        """Quote the resolution steps of the best-ranked source that has any.

        Inside a group of matching sources, the knowledge-base article is preferred,
        because it holds the clean procedure without one ticket's closing note.
        """
        for group in groups:
            preferred = sorted(group.members, key=lambda member: member["source_type"] != "kb")
            for source in preferred:
                steps = resolution_steps(source["content"])
                if steps:
                    return {
                        "summary": f"Closest match: {source['title']}. Steps are quoted from the source.",
                        "already_tried": [],
                        "steps": [
                            {
                                "n": number,
                                "text": text,
                                "citations": group.ids,
                                "support": 1.0,
                                "verified": True,
                                "repeats_already_tried": False,
                            }
                            for number, text in enumerate(steps, start=1)
                        ],
                        "escalate": False,
                        "escalation_reason": "",
                    }
        return None

    # ---- the whole pipeline ------------------------------------------------------------------

    def generate(self, complaint: str, sources: list[dict], triage: dict | None = None) -> dict:
        settings = self._settings
        started = time.perf_counter()
        timings: dict[str, float] = {}
        complaint = mask_pii(complaint)
        groups = group_sources(sources, settings.duplicate_overlap)[: settings.max_prompt_sources]

        answer, usage, fallback_reason, dropped = None, {}, None, 0
        if not settings.llm_enabled or self._llm is None:
            fallback_reason = "llm_disabled"
        else:
            for _attempt in range(2):  # one retry if the answer is unusable
                stage = time.perf_counter()
                try:
                    raw, usage = self._ask_model(complaint, triage, groups)
                except LLMUnavailableError:
                    fallback_reason = "llm_unavailable"
                    timings["llm"] = timings.get("llm", 0.0) + time.perf_counter() - stage
                    break  # do not wait for a second timeout
                except LLMOutputError:
                    fallback_reason = "invalid_output"
                    timings["llm"] = timings.get("llm", 0.0) + time.perf_counter() - stage
                    continue
                timings["llm"] = timings.get("llm", 0.0) + time.perf_counter() - stage
                LLM_SECONDS.observe(time.perf_counter() - stage)
                TOKENS.labels("prompt").inc(usage.get("prompt_tokens", 0))
                TOKENS.labels("completion").inc(usage.get("completion_tokens", 0))

                stage = time.perf_counter()
                steps, dropped = self._check(raw, groups)
                timings["check"] = timings.get("check", 0.0) + time.perf_counter() - stage
                if steps or raw.get("escalate") is True:
                    answer = {
                        "summary": str(raw.get("summary", "")).strip(),
                        "already_tried": [str(item) for item in raw.get("already_tried", [])],
                        "steps": steps,
                        "escalate": bool(raw.get("escalate")) or not steps,
                        "escalation_reason": str(raw.get("escalation_reason", "")).strip(),
                    }
                    fallback_reason = None
                    break
                fallback_reason = "invalid_output"  # no usable steps and no escalation: try again

        mode = "llm"
        if answer is None:
            FALLBACKS.labels(fallback_reason).inc()
            mode = "extractive"
            answer = self._extractive(groups) or {
                "summary": "No usable resolution steps were found in the sources.",
                "already_tried": [],
                "steps": [],
                "escalate": True,
                "escalation_reason": "The retrieved sources contain no resolution steps.",
            }

        unsupported = sum(1 for step in answer["steps"] if not step["verified"])
        DROPPED_STEPS.inc(dropped)
        UNSUPPORTED_STEPS.inc(unsupported)
        ANSWERS.labels(mode).inc()
        timings["total"] = time.perf_counter() - started
        return {
            **answer,
            "mode": mode,
            "grounded": unsupported == 0 and (bool(answer["steps"]) or answer["escalate"]),
            "dropped_steps": dropped,
            "fallback_reason": fallback_reason,
            "sources_used": [source_id for group in groups for source_id in group.ids],
            "model": self._llm.model if mode == "llm" else None,
            "prompt_version": PROMPT_VERSION,
            "usage": {key: usage.get(key, 0) for key in ("prompt_tokens", "completion_tokens")},
            "timings_ms": {name: round(seconds * 1000, 1) for name, seconds in timings.items()},
        }
