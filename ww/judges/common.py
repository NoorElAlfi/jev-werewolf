"""Helpers shared by the judges. Everything here works on public_view() output
only: {"players": [...], "rounds": [{"round", "public_events", "statements",
"defense"?}]}."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ww.config import JEV_PRICE_USD_PER_MTOK_INPUT, JEV_PRICE_USD_PER_MTOK_OUTPUT


def view_digest(view: dict[str, Any]) -> str:
    """Stable hash of a view (for deterministic per-view randomness)."""
    return hashlib.sha256(json.dumps(view, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def statements_of(view: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    """That player's statements in the view, oldest first: [{"round", "text"}]."""
    return [
        {"round": r["round"], "text": s["text"]}
        for r in view["rounds"]
        for s in r["statements"]
        if s["player_id"] == pid
    ]


def defenses_of(view: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    return [
        {"round": r["round"], "text": r["defense"]["text"]}
        for r in view["rounds"]
        if r.get("defense") and r["defense"]["player_id"] == pid
    ]


def strip_defense(view: dict[str, Any]) -> dict[str, Any]:
    """A copy of the view without any defenses."""
    return {
        "players": list(view["players"]),
        "rounds": [{k: v for k, v in r.items() if k != "defense"} for r in view["rounds"]],
    }


def view_as_text(view: dict[str, Any], include_defense: bool = True) -> str:
    """Human-readable transcript of the view, for LLM prompts."""
    lines = [f"Players: {', '.join(view['players'])}"]
    for r in view["rounds"]:
        lines.append(f"\n--- Round {r['round']} ---")
        for e in r["public_events"]:
            lines.append(f"[event] {e}")
        for s in r["statements"]:
            lines.append(f"{s['player_id']}: {s['text']}")
        if include_defense and r.get("defense"):
            d = r["defense"]
            lines.append(f"[defense] {d['player_id']}: {d['text']}")
    return "\n".join(lines)


def jev_cost_usd(input_tokens: int, output_tokens: int = 0) -> float:
    return input_tokens / 1e6 * JEV_PRICE_USD_PER_MTOK_INPUT + output_tokens / 1e6 * JEV_PRICE_USD_PER_MTOK_OUTPUT


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))
