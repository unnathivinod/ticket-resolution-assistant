"""Drafts a resolution from the retrieved sources, then checks it before returning it.

Pipeline for one request:
  1. Group sources that say the same thing, keep the best few, shorten them.
  2. Ask the language model for a JSON answer in which every step cites a source ID.
  3. Check the answer: citations must be real, each step must be supported by its source,
     and steps the customer already tried are flagged.
  4. If the model is unavailable or its answer is unusable, ask the backup model when one is
     configured. If that fails too, fall back to quoting the resolution steps of the best
     source directly. The service always returns something useful.
"""

from __future__ import annotations

import logging
import time

from prometheus_client import Counter, Histogram

from libs.common.llm_client import LLMOutputError, LLMUnavailableError
from libs.common.pii import mask_pii
from services.generation.config import Settings
from services.generation.prompt import (
    answer_schema,
    build_user_prompt,
    clean_step_text,
    prompt_version,
    system_prompt,
)
from services.generation.sources import (
    SourceGroup,
    group_sources,
    resolution_steps,
    said_by_customer,
    shorten,
    statements,
)

log = logging.getLogger("generation")

ANSWERS = Counter("generation_answers_total", "Answers produced, by how they were made", ["mode"])
FALLBACKS = Counter("generation_fallbacks_total", "Times the model's answer could not be used", ["reason"])
REFUSALS = Counter(
    "generation_model_refusals_total", "Answers withheld because the model said the sources do not match"
)
INVENTED_TRIED = Counter(
    "generation_invented_already_tried_total",
    "'Already tried' items removed because the customer never said them",
)
FAILOVERS = Counter(
    "generation_model_failovers_total",
    "Times the backup model was asked because the first model failed",
    ["reason"],
)
MODEL_ANSWERS = Counter("generation_model_answers_total", "Answers written by each model", ["model"])
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
for _reason in ("llm_unavailable", "invalid_output"):
    FAILOVERS.labels(_reason)


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


class Generator:
    def __init__(self, llm, embedder, settings: Settings, fallback_llm=None) -> None:
        self._llm = llm  # may be None when the model is switched off
        self._fallback_llm = fallback_llm  # asked only when the first model fails; usually None
        self._embedder = embedder
        self._settings = settings

    def llm_ready(self) -> bool:
        return self._llm is not None and self._llm.ready()

    def fallback_llm_ready(self) -> bool:
        return self._fallback_llm is not None and self._fallback_llm.ready()

    @property
    def fallback_model(self) -> str | None:
        return self._fallback_llm.model if self._fallback_llm is not None else None

    def embedder_ready(self) -> bool:
        return self._embedder.ready()

    # ---- step 2: ask the model -------------------------------------------------------------

    def _ask_model(
        self, llm, complaint: str, triage: dict | None, groups: list[SourceGroup]
    ) -> tuple[dict, dict]:
        settings = self._settings
        shown = [
            {**group.primary, "content": shorten(group.primary["content"], settings.max_source_chars)}
            for group in groups
        ]
        return llm.chat_json(
            system=system_prompt(settings.max_steps, settings.match_check),
            user=build_user_prompt(complaint, triage, shown),
            schema=answer_schema([group.primary["id"] for group in groups], settings.match_check),
            # The match check adds three short fields to the reply, so it needs a little more room.
            max_tokens=settings.max_output_tokens + (50 if settings.match_check else 0),
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

    # ---- steps 2 and 3 for one model ----------------------------------------------------------

    def _draft(
        self, llm, complaint: str, triage: dict | None, groups: list[SourceGroup], timings: dict
    ) -> tuple[dict | None, dict, str | None, int]:
        """Ask one model and check its answer. Returns (answer, token usage, why it failed, dropped steps).

        answer is None when this model could not produce a usable one.
        """
        settings = self._settings
        usage, reason, dropped = {}, None, 0
        for _attempt in range(2):  # one retry if the answer is unusable
            stage = time.perf_counter()
            try:
                raw, usage = self._ask_model(llm, complaint, triage, groups)
            except LLMUnavailableError as error:
                log.warning("model unavailable", extra={"fields": {"model": llm.model, "error": str(error)}})
                timings["llm"] = timings.get("llm", 0.0) + time.perf_counter() - stage
                return None, usage, "llm_unavailable", dropped  # do not wait for a second timeout
            except LLMOutputError as error:
                log.warning("unusable reply", extra={"fields": {"model": llm.model, "error": str(error)}})
                reason = "invalid_output"
                timings["llm"] = timings.get("llm", 0.0) + time.perf_counter() - stage
                continue
            timings["llm"] = timings.get("llm", 0.0) + time.perf_counter() - stage
            LLM_SECONDS.observe(time.perf_counter() - stage)
            TOKENS.labels("prompt").inc(usage.get("prompt_tokens", 0))
            TOKENS.labels("completion").inc(usage.get("completion_tokens", 0))

            # Keep only "already tried" items the customer really wrote. The model sometimes
            # lifts one from a past ticket, which would also flag good steps as repeats.
            claimed = [str(item).strip() for item in raw.get("already_tried", []) if str(item).strip()]
            raw["already_tried"] = [item for item in claimed if said_by_customer(item, complaint)]
            INVENTED_TRIED.inc(len(claimed) - len(raw["already_tried"]))

            if settings.match_check and raw.get("same_problem") is False:
                # The model says the sources are about a different problem. Its word is
                # enforced here: no steps are shown, whatever else it wrote.
                REFUSALS.inc()
                about = str(raw.get("source_problem", "")).strip().rstrip(".") or "a different problem"
                wanted = str(raw.get("customer_problem", "")).strip().rstrip(".") or "this problem"
                answer = {
                    "summary": f"No matching fix was found for: {wanted}.",
                    "already_tried": [str(item) for item in raw.get("already_tried", [])],
                    "steps": [],
                    "escalate": True,
                    "escalation_reason": f"The closest sources are about something else ({about}).",
                }
                return answer, usage, None, dropped

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
                return answer, usage, None, dropped
            reason = "invalid_output"  # no usable steps and no escalation: try again
        return None, usage, reason, dropped

    # ---- the whole pipeline ------------------------------------------------------------------

    def generate(self, complaint: str, sources: list[dict], triage: dict | None = None) -> dict:
        settings = self._settings
        started = time.perf_counter()
        timings: dict[str, float] = {}
        complaint = mask_pii(complaint)
        groups = group_sources(sources, settings.duplicate_overlap)[: settings.max_prompt_sources]

        answer, usage, fallback_reason, dropped = None, {}, "llm_disabled", 0
        answered_by, failover_from = None, None
        models = [self._llm, self._fallback_llm] if settings.llm_enabled and self._llm is not None else []
        for position, llm in enumerate(model for model in models if model is not None):
            if position > 0:
                # The first model failed. Note it, then give the backup model the same task.
                FAILOVERS.labels(fallback_reason).inc()
                failover_from = self._llm.model
            answer, usage, fallback_reason, dropped = self._draft(llm, complaint, triage, groups, timings)
            if answer is not None:
                answered_by = llm.model
                break

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
        else:
            MODEL_ANSWERS.labels(answered_by).inc()

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
            "model": answered_by,
            # Set when the first-choice model failed and the backup model wrote the answer.
            "failover_from": failover_from if mode == "llm" else None,
            "prompt_version": prompt_version(settings.match_check),
            "usage": {key: usage.get(key, 0) for key in ("prompt_tokens", "completion_tokens")},
            "timings_ms": {name: round(seconds * 1000, 1) for name, seconds in timings.items()},
        }
