"""SQLite-backed structured memory facts for DraftMate.

Markdown profiles remain the user-editable surface. This module stores the
machine-readable layer: contacts, imported summaries, and extracted facts.
"""
from __future__ import annotations

import datetime
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import config

DB_PATH = config.base_dir() / "memory.sqlite3"

SECTION_KINDS = {
    "关系背景": "relationship",
    "对方画像": "profile",
    "雷区/边界": "boundary",
    "一起经历/聊过的大事": "event",
    "承诺与待办": "commitment",
    "最近氛围": "mood",
}

KIND_LABELS = {
    "relationship": "关系背景",
    "profile": "对方画像",
    "boundary": "雷区/边界",
    "event": "一起经历/聊过的大事",
    "commitment": "承诺与待办",
    "mood": "最近氛围",
}

KIND_ORDER = {
    "boundary": 0,
    "commitment": 1,
    "relationship": 2,
    "profile": 3,
    "event": 4,
    "mood": 5,
}

COMPACT_LABELS = {
    "boundary": "稳定边界",
    "open_loop": "未完成事项",
    "relationship": "关系状态",
    "preference": "偏好",
    "profile": "稳定画像",
    "rhythm": "互动节奏",
}

COMPACT_ORDER = {
    "boundary": 0,
    "open_loop": 1,
    "relationship": 2,
    "preference": 3,
    "profile": 4,
    "rhythm": 5,
}


def init_db(db_path: Path | None = None) -> Path:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("PRAGMA foreign_keys = ON")
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL UNIQUE,
                safe_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS summaries (
                contact_id INTEGER PRIMARY KEY,
                body TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS memory_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contact_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'summary_import',
                source_ref TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.7,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(contact_id, kind, content),
                FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_memory_facts_contact_status
            ON memory_facts(contact_id, status, kind, updated_at);

            CREATE TABLE IF NOT EXISTS memory_compacts (
                contact_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.7,
                evidence_count INTEGER NOT NULL DEFAULT 1,
                last_seen_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(contact_id, kind, content),
                FOREIGN KEY(contact_id) REFERENCES contacts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_memory_compacts_contact_kind
            ON memory_compacts(contact_id, kind, updated_at);
            """
        )
        con.execute(
            "UPDATE memory_facts SET status = 'active' WHERE status IN ('candidate', 'confirmed')"
        )
    return path


def ensure_contact(title: str, safe_name: str = "", db_path: Path | None = None) -> int:
    path = init_db(db_path)
    now = _now()
    safe = safe_name or _safe(title)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute(
            """
            INSERT INTO contacts(title, safe_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(title) DO UPDATE SET
                safe_name = excluded.safe_name,
                updated_at = excluded.updated_at
            """,
            (title or "unknown", safe, now, now),
        )
        row = con.execute("SELECT id FROM contacts WHERE title = ?", (title or "unknown",)).fetchone()
        return int(row[0])


def replace_summary_facts(
    title: str,
    summary: str,
    safe_name: str = "",
    db_path: Path | None = None,
) -> int:
    """Store the raw summary and replace facts imported from that summary."""
    path = init_db(db_path)
    contact_id = ensure_contact(title, safe_name=safe_name, db_path=path)
    facts = parse_summary(summary)
    now = _now()
    body = (summary or "").strip()
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("PRAGMA foreign_keys = ON")
        existing = con.execute(
            "SELECT body FROM summaries WHERE contact_id = ?",
            (contact_id,),
        ).fetchone()
        if existing and str(existing[0]) == body:
            row = con.execute(
                "SELECT COUNT(*) FROM memory_facts WHERE contact_id = ? AND source = 'summary_import'",
                (contact_id,),
            ).fetchone()
            if int(row[0]) > 0:
                return int(row[0])
        con.execute(
            """
            INSERT INTO summaries(contact_id, body, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(contact_id) DO UPDATE SET
                body = excluded.body,
                updated_at = excluded.updated_at
            """,
            (contact_id, body, now),
        )
        con.execute(
            "DELETE FROM memory_facts WHERE contact_id = ? AND source = 'summary_import'",
            (contact_id,),
        )
        for fact in facts:
            con.execute(
                """
                INSERT INTO memory_facts(
                    contact_id, kind, content, source, source_ref,
                    confidence, status, created_at, updated_at
                )
                VALUES (?, ?, ?, 'summary_import', ?, 0.7, 'active', ?, ?)
                ON CONFLICT(contact_id, kind, content) DO UPDATE SET
                    source_ref = excluded.source_ref,
                    updated_at = excluded.updated_at
                """,
                (contact_id, fact["kind"], fact["content"], fact.get("source_ref", ""), now, now),
            )
    return len(facts)


def render_compact_source(title: str, db_path: Path | None = None, limit: int = 60) -> str:
    """Render active facts as compact input. This is not used directly in reply prompts."""
    return render_facts(title, db_path=db_path, limit=limit, query_text="")


def replace_compacts_from_json(
    title: str,
    compact_json: str,
    safe_name: str = "",
    db_path: Path | None = None,
) -> int:
    items = parse_compact_json(compact_json)
    return replace_compacts(title, items, safe_name=safe_name, db_path=db_path)


def replace_compacts(
    title: str,
    items: list[dict],
    safe_name: str = "",
    db_path: Path | None = None,
) -> int:
    path = init_db(db_path)
    contact_id = ensure_contact(title, safe_name=safe_name, db_path=path)
    now = _now()
    clean = _dedupe_compacts(items)
    with closing(sqlite3.connect(path)) as con, con:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("DELETE FROM memory_compacts WHERE contact_id = ?", (contact_id,))
        for item in clean:
            con.execute(
                """
                INSERT INTO memory_compacts(
                    contact_id, kind, content, confidence,
                    evidence_count, last_seen_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(contact_id, kind, content) DO UPDATE SET
                    confidence = excluded.confidence,
                    evidence_count = excluded.evidence_count,
                    last_seen_at = excluded.last_seen_at,
                    updated_at = excluded.updated_at
                """,
                (
                    contact_id,
                    item["kind"],
                    item["content"],
                    float(item.get("confidence", 0.75)),
                    max(1, int(item.get("evidence_count", 1))),
                    item.get("last_seen_at") or now,
                    now,
                ),
            )
    return len(clean)


def parse_compact_json(text: str) -> list[dict]:
    raw = _strip_code_fence(text or "").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("compact JSON must be a list")
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = _normalize_compact_kind(str(item.get("kind") or "profile"))
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        out.append(
            {
                "kind": kind,
                "content": content[:120],
                "confidence": _clamp_float(item.get("confidence", 0.75), 0.1, 1.0),
                "evidence_count": max(1, int(item.get("evidence_count") or 1)),
                "last_seen_at": str(item.get("last_seen_at") or ""),
            }
        )
    return _dedupe_compacts(out)


def render_compacts(
    title: str,
    db_path: Path | None = None,
    limit: int = 8,
    query_text: str = "",
) -> str:
    path = init_db(db_path)
    with closing(sqlite3.connect(path)) as con, con:
        con.row_factory = sqlite3.Row
        contact = con.execute("SELECT id FROM contacts WHERE title = ?", (title or "unknown",)).fetchone()
        if not contact:
            return ""
        rows = con.execute(
            """
            SELECT kind, content, confidence, evidence_count, last_seen_at, updated_at
            FROM memory_compacts
            WHERE contact_id = ?
            ORDER BY updated_at DESC
            LIMIT 100
            """,
            (int(contact["id"]),),
        ).fetchall()
    if not rows:
        return ""
    selected = select_compacts(rows, query_text=query_text, limit=limit)
    grouped: dict[str, list[str]] = {}
    for row in selected:
        grouped.setdefault(str(row["kind"]), []).append(str(row["content"]))
    out = ["## 压缩记忆(SQLite compact)"]
    for kind in sorted(grouped, key=lambda k: COMPACT_ORDER.get(k, 99)):
        out.append(f"### {COMPACT_LABELS.get(kind, kind)}")
        out.extend(f"- {item}" for item in grouped[kind])
    return "\n".join(out).strip()


def parse_summary(summary: str) -> list[dict[str, str]]:
    current_kind = ""
    facts: list[dict[str, str]] = []
    for raw in (summary or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("## "):
            heading = line[3:].strip()
            current_kind = SECTION_KINDS.get(heading, "")
            continue
        if not current_kind:
            continue
        content = _clean_fact_line(line)
        if not content or _is_empty_fact(content):
            continue
        facts.append(
            {
                "kind": current_kind,
                "content": content,
                "source_ref": _source_ref(content),
            }
        )
    return _dedupe_facts(facts)


def render_facts(
    title: str,
    db_path: Path | None = None,
    limit: int = 10,
    query_text: str = "",
) -> str:
    path = init_db(db_path)
    with closing(sqlite3.connect(path)) as con, con:
        con.row_factory = sqlite3.Row
        contact = con.execute("SELECT id FROM contacts WHERE title = ?", (title or "unknown",)).fetchone()
        if not contact:
            return ""
        rows = con.execute(
            """
            SELECT kind, content, status, source_ref, updated_at
            FROM memory_facts
            WHERE contact_id = ? AND status = 'active'
            ORDER BY updated_at DESC, id DESC
            LIMIT 200
            """,
            (int(contact["id"]),),
        ).fetchall()
    if not rows:
        return ""
    selected = select_facts(rows, query_text=query_text, limit=limit)
    grouped: dict[str, list[str]] = {}
    for row in selected:
        grouped.setdefault(str(row["kind"]), []).append(str(row["content"]))
    out = ["## 结构化记忆(SQLite facts)"]
    for kind in sorted(grouped, key=lambda k: KIND_ORDER.get(k, 99)):
        out.append(f"### {KIND_LABELS.get(kind, kind)}")
        out.extend(f"- {item}" for item in grouped[kind])
    return "\n".join(out).strip()


def select_facts(rows, query_text: str = "", limit: int = 10) -> list:
    """Small deterministic memory selector, scoped to one contact."""
    query_tokens = _topic_tokens(query_text)
    ranked = []
    for idx, row in enumerate(rows):
        score = _fact_score(row, query_tokens)
        ranked.append((score, idx, row))
    ranked.sort(
        key=lambda item: (
            -item[0],
            KIND_ORDER.get(str(item[2]["kind"]), 99),
            item[1],
        )
    )
    return [item[2] for item in ranked[: max(1, limit)]]


def select_compacts(rows, query_text: str = "", limit: int = 8) -> list:
    query_tokens = _topic_tokens(query_text)
    ranked = []
    for idx, row in enumerate(rows):
        score = _compact_score(row, query_tokens)
        ranked.append((score, idx, row))
    ranked.sort(
        key=lambda item: (
            -item[0],
            COMPACT_ORDER.get(str(item[2]["kind"]), 99),
            item[1],
        )
    )
    return [item[2] for item in ranked[: max(1, limit)]]


def _compact_score(row, query_tokens: set[str]) -> float:
    kind = str(row["kind"])
    content = str(row["content"])
    score = {
        "boundary": 120.0,
        "open_loop": 105.0,
        "relationship": 86.0,
        "preference": 72.0,
        "profile": 62.0,
        "rhythm": 56.0,
    }.get(kind, 40.0)
    score += min(20.0, max(1, int(row["evidence_count"])) * 3.0)
    score += float(row["confidence"]) * 12.0
    score += _recency_score(str(row["last_seen_at"] or row["updated_at"]))
    if query_tokens:
        overlap = query_tokens & _topic_tokens(content)
        score += min(60.0, len(overlap) * 15.0)
    return score


def _fact_score(row, query_tokens: set[str]) -> float:
    kind = str(row["kind"])
    content = str(row["content"])
    score = {
        "boundary": 100.0,
        "commitment": 90.0,
        "relationship": 75.0,
        "mood": 58.0,
        "profile": 52.0,
        "event": 45.0,
    }.get(kind, 30.0)
    score += _recency_score(str(row["updated_at"]))
    if query_tokens:
        overlap = query_tokens & _topic_tokens(content)
        score += min(70.0, len(overlap) * 18.0)
    return score


def _normalize_compact_kind(kind: str) -> str:
    aliases = {
        "边界": "boundary",
        "雷区": "boundary",
        "待办": "open_loop",
        "承诺": "open_loop",
        "关系": "relationship",
        "偏好": "preference",
        "画像": "profile",
        "节奏": "rhythm",
        "mood": "rhythm",
        "commitment": "open_loop",
    }
    normalized = aliases.get(kind.strip().lower(), kind.strip().lower())
    return normalized if normalized in COMPACT_LABELS else "profile"


def _recency_score(updated_at: str) -> float:
    try:
        dt = datetime.datetime.fromisoformat(updated_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        age_days = max(0, (datetime.datetime.now(datetime.timezone.utc) - dt).days)
    except ValueError:
        return 0.0
    if age_days <= 7:
        return 12.0
    if age_days <= 30:
        return 8.0
    if age_days <= 90:
        return 4.0
    return 0.0


def _topic_tokens(text: str) -> set[str]:
    text = (text or "").lower()
    tokens = set(re.findall(r"[a-z0-9_]{2,}", text))
    for seq in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        tokens.add(seq)
        tokens.update(seq[i:i + 2] for i in range(max(0, len(seq) - 1)))
    return tokens


def _strip_code_fence(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    return match.group(1) if match else text


def _clamp_float(value, low: float, high: float) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        f = low
    return max(low, min(high, f))


def _clean_fact_line(line: str) -> str:
    content = re.sub(r"^[-*]\s*", "", line).strip()
    content = re.sub(r"^(一句话|总结)[:：]\s*", "", content).strip()
    return content


def _is_empty_fact(content: str) -> bool:
    plain = content.strip()
    return plain in {"(无)", "无", "(暂无)", "暂无", "(无足够线索)", "无足够线索"}


def _source_ref(content: str) -> str:
    match = re.search(r"\[据[:：]\"([^\"]+)\"\]", content)
    return match.group(1) if match else ""


def _dedupe_facts(facts: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    out = []
    for fact in facts:
        key = (fact["kind"], fact["content"])
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
    return out


def _dedupe_compacts(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for item in items:
        key = (item["kind"], item["content"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _safe(title: str) -> str:
    return re.sub(r"[^\w一-鿿\-]", "_", title or "unknown")[:60]


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
