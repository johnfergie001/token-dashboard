"""Cost/token breakdown by real project, optionally split below the Claude
Code session slug.

Why this exists: a Claude Code project 'slug' is the folder a session was
launched in, not the files it touched. Anyone who reaches other projects by
absolute path, or manages a remote service over SSH, sees that work lumped
under one slug — `project_summary()` alone can't separate it back out.

If `~/.claude/token-dashboard-subprojects.json` (or `TOKEN_DASHBOARD_SUBPROJECTS`)
defines named regex groups, each session's cost is split across whichever
groups its real tool calls (Bash/Read/Edit/Write targets) match, weighted by
each group's share of that session's real tool calls. Sessions with no
subprojects file configured — or with no calls matching any group — fall back
to plain per-slug grouping, so this is a no-op for anyone who hasn't set one
up. See subprojects.example.json for the format.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from .db import connect, _range_clause, best_project_name
from .pricing import cost_for

REAL_TOOLS = ("Bash", "Read", "Edit", "Write")


def default_map_path() -> Path:
    return Path.home() / ".claude" / "token-dashboard-subprojects.json"


def load_map(path: Optional[str] = None) -> list:
    """Return [{"name": str, "patterns": [compiled re, ...]}, ...]; [] if unset/missing/invalid."""
    p = Path(path or os.environ.get("TOKEN_DASHBOARD_SUBPROJECTS") or default_map_path())
    if not p.is_file():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    groups = raw.get("groups") if isinstance(raw, dict) else None
    if not isinstance(groups, list):
        return []
    out = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        name, patterns = g.get("name"), g.get("patterns")
        if not name or not isinstance(patterns, list):
            continue
        compiled = []
        for pat in patterns:
            try:
                compiled.append(re.compile(pat, re.IGNORECASE))
            except re.error:
                continue
        if compiled:
            out.append({"name": name, "patterns": compiled})
    return out


def _match_group(target: str, groups: list) -> Optional[str]:
    for g in groups:
        for rx in g["patterns"]:
            if rx.search(target):
                return g["name"]
    return None


def _new_bucket(name: str) -> dict:
    return {
        "project_name": name, "project_slug": name,
        "sessions": set(), "turns": 0.0,
        "input_tokens": 0.0, "output_tokens": 0.0,
        "cache_read_tokens": 0.0, "billable_tokens": 0.0,
        "cost_usd": 0.0,
    }


def _add(bucket: dict, sid: str, weight: float, turns: int, in_tok: int, out_tok: int,
         cr_tok: int, billable: int, cost: float) -> None:
    if weight <= 0:
        return
    bucket["sessions"].add(sid)
    bucket["turns"] += turns * weight
    bucket["input_tokens"] += in_tok * weight
    bucket["output_tokens"] += out_tok * weight
    bucket["cache_read_tokens"] += cr_tok * weight
    bucket["billable_tokens"] += billable * weight
    bucket["cost_usd"] += cost * weight


def project_cost_rows(db_path, pricing: dict, since=None, until=None, map_path: Optional[str] = None) -> list:
    """Per-project token + cost breakdown.

    Cost is computed per assistant message (never by summing tokens across a
    session and pricing the total at one tier-rate) so sessions that switch
    model mid-way are still priced correctly, then weighted-summed into
    whichever project bucket(s) that message's session belongs to.
    """
    groups = load_map(map_path)
    rng, args = _range_clause(since, until)

    with connect(db_path) as c:
        sessions = [r["session_id"] for r in c.execute(
            f"SELECT DISTINCT session_id FROM messages m WHERE 1=1 {rng}", args
        )]

        totals: dict = {}
        slug_names: dict = {}

        def slug_name(slug: str) -> str:
            if slug not in slug_names:
                cwds = [row["cwd"] for row in c.execute(
                    "SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND cwd IS NOT NULL", (slug,)
                )]
                slug_names[slug] = best_project_name(cwds, slug)
            return slug_names[slug]

        for sid in sessions:
            msgs = [dict(r) for r in c.execute(
                "SELECT type, project_slug, model, input_tokens, output_tokens, "
                "cache_read_tokens, cache_create_5m_tokens, cache_create_1h_tokens "
                "FROM messages WHERE session_id=?", (sid,)
            )]
            if not msgs:
                continue
            slug = msgs[0]["project_slug"]
            turns = sum(1 for m in msgs if m["type"] == "user")

            session_cost = 0.0
            in_tok = out_tok = cr_tok = cc_tok = 0
            for m in msgs:
                if m["type"] != "assistant":
                    continue
                in_tok += m["input_tokens"] or 0
                out_tok += m["output_tokens"] or 0
                cr_tok += m["cache_read_tokens"] or 0
                cc_tok += (m["cache_create_5m_tokens"] or 0) + (m["cache_create_1h_tokens"] or 0)
                usage = {
                    "input_tokens": m["input_tokens"] or 0,
                    "output_tokens": m["output_tokens"] or 0,
                    "cache_read_tokens": m["cache_read_tokens"] or 0,
                    "cache_create_5m_tokens": m["cache_create_5m_tokens"] or 0,
                    "cache_create_1h_tokens": m["cache_create_1h_tokens"] or 0,
                }
                cf = cost_for(m["model"] or "", usage, pricing)
                if cf["usd"] is not None:
                    session_cost += cf["usd"]
            billable = in_tok + out_tok + cc_tok

            fallback_name = slug_name(slug)

            if not groups:
                _add(totals.setdefault(fallback_name, _new_bucket(fallback_name)),
                     sid, 1.0, turns, in_tok, out_tok, cr_tok, billable, session_cost)
                continue

            calls = [r["target"] for r in c.execute(
                "SELECT target FROM tool_calls WHERE session_id=? AND tool_name IN "
                "('Bash','Read','Edit','Write') AND target IS NOT NULL", (sid,)
            )]
            if not calls:
                _add(totals.setdefault(fallback_name, _new_bucket(fallback_name)),
                     sid, 1.0, turns, in_tok, out_tok, cr_tok, billable, session_cost)
                continue

            match_counts: dict = {}
            for target in calls:
                name = _match_group(target, groups)
                if name:
                    match_counts[name] = match_counts.get(name, 0) + 1
            total_calls = len(calls)

            for name, n in match_counts.items():
                _add(totals.setdefault(name, _new_bucket(name)),
                     sid, n / total_calls, turns, in_tok, out_tok, cr_tok, billable, session_cost)

            leftover = (total_calls - sum(match_counts.values())) / total_calls
            _add(totals.setdefault(fallback_name, _new_bucket(fallback_name)),
                 sid, leftover, turns, in_tok, out_tok, cr_tok, billable, session_cost)

        rows = []
        for name, b in totals.items():
            if not b["sessions"]:
                continue
            rows.append({
                "project_slug": b["project_slug"],
                "project_name": b["project_name"],
                "sessions": len(b["sessions"]),
                "turns": round(b["turns"]),
                "input_tokens": round(b["input_tokens"]),
                "output_tokens": round(b["output_tokens"]),
                "cache_read_tokens": round(b["cache_read_tokens"]),
                "billable_tokens": round(b["billable_tokens"]),
                "cost_usd": round(b["cost_usd"], 4),
                "subproject": bool(groups),
            })
        rows.sort(key=lambda r: r["cost_usd"], reverse=True)
        return rows
