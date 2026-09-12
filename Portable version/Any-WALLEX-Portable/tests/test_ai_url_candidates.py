"""AI base-URL candidate dictionary (user req): mangled URLs get auto-corrected
by trying plausible combinations. Offline tests — no network."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.ai_strategy import AIStrategyClient, AIProviderConfig


def _client():
    return AIStrategyClient(AIProviderConfig(provider="custom", base_url="x", model="m"))


def test_correct_url_first_candidate():
    cands = AIStrategyClient._base_candidates("https://api.b.ai/v1/")
    assert cands[0] == "https://api.b.ai/v1"      # exact entry wins first


def test_mangled_double_path_produces_clean_root():
    cands = AIStrategyClient._base_candidates("https://api.b.ai/v1/api/v1")
    assert "https://api.b.ai/v1" in cands         # the correct URL is among candidates
    assert cands[0] == "https://api.b.ai/v1/api/v1"  # user's input tried first
    assert "https://api.b.ai" in cands            # bare host fallback
    # no candidate doubles the version segment (mangles are never propagated)
    assert all(not c.endswith("/v1/v1") for c in cands)


def test_models_v1_typo_handled():
    cands = AIStrategyClient._base_candidates("https://api.b.ai/models/v1")
    assert "https://api.b.ai/v1" in cands
    # user's mangled input is tried first but never doubled (/models/v1/v1 is junk)
    assert cands[0] == "https://api.b.ai/models/v1"
    assert all(not c.endswith("/v1/v1") for c in cands)


def test_bare_host_gets_schemes():
    cands = AIStrategyClient._base_candidates("api.b.ai")
    # OpenAI-compatible /v1 root is the FIRST guess for bare hosts
    assert cands[0] == "https://api.b.ai/v1"
    assert "https://api.b.ai" in cands


def test_no_duplicate_candidates():
    cands = AIStrategyClient._base_candidates("https://api.b.ai/v1")
    assert len(cands) == len(set(cands))


def test_local_ollama_style():
    cands = AIStrategyClient._base_candidates("http://localhost:11434")
    assert cands[0] == "http://localhost:11434/v1"   # standard root first
    assert "http://localhost:11434" in cands          # bare host (ollama native) too


def test_looks_like_model_json():
    class _R:
        def __init__(self, body):
            self._b = body
        def json(self):
            return self._b
    assert AIStrategyClient._looks_like_model_json(_R({"data": []})) is True
    assert AIStrategyClient._looks_like_model_json(_R({"models": []})) is True
    assert AIStrategyClient._looks_like_model_json(_R("<html>404 page</html>")) is False
    assert AIStrategyClient._looks_like_model_json(_R({"error": "x"})) is False
