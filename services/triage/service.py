"""Gathers the evidence for a complaint (similar tickets, signal matches) and applies the rules."""

from __future__ import annotations

import threading
import time

from libs.common.pii import mask_pii
from services.triage.logic import Evidence, Params, Signal, decide, match_signals, split_segments


class Triage:
    def __init__(self, retrieval, embedder, signals: list[Signal], params: Params, max_neighbours: int = 25):
        self._retrieval = retrieval
        self._embedder = embedder
        self.signals = signals
        self.params = params
        self._max_neighbours = max_neighbours
        self._example_vectors: dict[str, list[list[float]]] | None = None
        self._lock = threading.Lock()

    def ready(self) -> bool:
        return self._retrieval.ready() and self._embedder.ready()

    def _signal_vectors(self) -> dict[str, list[list[float]]]:
        """Embed the signal examples once and keep them in memory."""
        with self._lock:
            if self._example_vectors is None:
                texts = [example for signal in self.signals for example in signal.examples]
                vectors = iter(self._embedder.embed(texts, kind="document")["dense"])
                self._example_vectors = {
                    signal.name: [next(vectors) for _ in signal.examples] for signal in self.signals
                }
            return self._example_vectors

    def collect(self, complaint: str) -> tuple[Evidence, dict[str, float]]:
        """Fetch similar tickets and score every signal. Returns the evidence and stage timings."""
        timings: dict[str, float] = {}
        text = mask_pii(complaint)

        stage = time.perf_counter()
        found = self._retrieval.search(text, top_k_tickets=self._max_neighbours, top_k_kb=0, rerank=False)
        timings["neighbours"] = time.perf_counter() - stage

        stage = time.perf_counter()
        segments = split_segments(text)
        segment_vectors = self._embedder.embed(segments, kind="query")["dense"]
        signals = match_signals(segments, segment_vectors, self.signals, self._signal_vectors())
        timings["signals"] = time.perf_counter() - stage

        return Evidence(neighbours=found["results"], signals=signals), timings

    def classify(self, complaint: str, params: Params | None = None) -> dict:
        started = time.perf_counter()
        evidence, timings = self.collect(complaint)
        result = decide(evidence, self.signals, params or self.params)
        timings["total"] = time.perf_counter() - started
        result["timings_ms"] = {name: round(seconds * 1000, 1) for name, seconds in timings.items()}
        return result
