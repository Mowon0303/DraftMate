"""skills:人设(personas)+ 联系人记忆(memory)的加载/存储。

都读写 skills/ 下的子目录,目录位置由 config.base_dir() 决定(开发态=项目目录,
打包态=~/Library/Application Support/DraftMate)。
"""
from __future__ import annotations

import re
from pathlib import Path

import config
import memory_store


# ════════════════════ 人设(personas)════════════════════
PERSONA_DIR = config.base_dir() / "skills" / "personas"
LOVEHELPER_DIR = config.resource_dir() / "skills" / "lovehelper"
LOVEHELPER_ADAPTER = LOVEHELPER_DIR / "draftmate-adapter.md"
LOVEHELPER_PLAYBOOK = LOVEHELPER_DIR / "relationship-copilot" / "references" / "human-progression-playbook.md"


def load_persona(name: str) -> str:
    # .local.md = 本地私有人设(不入 git、不入分发包),同名时优先公开版
    for suffix in (".md", ".local.md"):
        p = PERSONA_DIR / f"{name}{suffix}"
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def lovehelper_context() -> str:
    """读取 LoveHelper.skill 的 DraftMate 精简适配规则。缺失时静默降级。"""
    return LOVEHELPER_ADAPTER.read_text(encoding="utf-8") if LOVEHELPER_ADAPTER.exists() else ""


def lovehelper_playbook() -> str:
    """读取 LoveHelper 的推进 playbook。缺失时静默降级。"""
    return LOVEHELPER_PLAYBOOK.read_text(encoding="utf-8") if LOVEHELPER_PLAYBOOK.exists() else ""


# ════════════════════ 联系人记忆(人工档案 + 手动上下文)════════════════════
MEM_DIR = config.base_dir() / "skills" / "memory"
MEMORY_DB = config.base_dir() / "memory.sqlite3"
MANUAL_START = "<!-- autotalk:manual-context:start -->"
MANUAL_END = "<!-- autotalk:manual-context:end -->"
MANUAL_SECTIONS = {
    "person_info": "对方信息",
    "goal": "目标(阶段性)",
    "avoid": "不要提/边界",
    "notes": "备注",
}


def _safe(title: str) -> str:
    return re.sub(r"[^\w一-鿿\-]", "_", title or "unknown")[:60]


def _profile_path(title: str) -> Path:
    return MEM_DIR / f"{_safe(title)}.md"


def _summary_path(title: str) -> Path:
    return MEM_DIR / f"{_safe(title)}.summary.md"


def _template(title: str) -> str:
    return (
        f"# {title}\n"
        "- 关系: (待填,例如:同事 / 朋友 / 对象)\n"
        "- 称呼: \n"
        "- 偏好: (例如:说话简短、别发表情)\n"
        "- 不要提: \n"
        "- 人设: \n"  # 留空则用 config 映射或默认人设
        "- 备注: \n"
    )


def _blank_manual_context() -> dict:
    return {key: "" for key in MANUAL_SECTIONS}


def _profile_text(title: str | None) -> str:
    if not title:
        return ""
    p = _profile_path(title)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _manual_block(values: dict) -> str:
    rows = [MANUAL_START, "## 手动上下文"]
    for key, heading in MANUAL_SECTIONS.items():
        rows.append(f"### {heading}")
        rows.append((values.get(key) or "").strip())
        rows.append("")
    rows.append(MANUAL_END)
    return "\n".join(rows).rstrip() + "\n"


def _strip_manual_block(text: str) -> str:
    pattern = re.compile(
        rf"\n?{re.escape(MANUAL_START)}.*?{re.escape(MANUAL_END)}\n?",
        re.DOTALL,
    )
    return pattern.sub("\n", text).rstrip() + "\n"


def profile_exists(title: str | None) -> bool:
    return bool(title) and _profile_path(title).exists()


def manual_context(title: str | None) -> dict:
    """读取 UI 可编辑的手动上下文。没有填写时返回空字段。"""
    text = _profile_text(title)
    if not text or MANUAL_START not in text or MANUAL_END not in text:
        return _blank_manual_context()
    block = text.split(MANUAL_START, 1)[1].split(MANUAL_END, 1)[0]
    out = _blank_manual_context()
    heading_to_key = {v: k for k, v in MANUAL_SECTIONS.items()}
    current = None
    bucket: list[str] = []
    for line in block.splitlines():
        if line.startswith("### "):
            if current:
                out[current] = "\n".join(bucket).strip()
            current = heading_to_key.get(line[4:].strip())
            bucket = []
        elif current:
            bucket.append(line)
    if current:
        out[current] = "\n".join(bucket).strip()
    return out


def has_manual_context(values: dict) -> bool:
    return any((values.get(k) or "").strip() for k in ("person_info", "goal", "avoid"))


def has_distilled_memory(title: str | None) -> bool:
    """该联系人是否已有「自动记忆」的实质内容(历史导入 / 对方 post 蒸馏出的档案)。
    只有空模板、或各栏全是『(无)』的档案不算——避免对一无所知的人硬挤开场白。"""
    if not title:
        return False
    p = _summary_path(title)
    if not p.exists():
        return False
    placeholders = {"(无)", "（无）", "(无足够线索)", "（无足够线索）", "无"}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        body = re.sub(r"^[-*•·]\s*", "", line)
        body = body.replace("一句话:", "").replace("一句话：", "").strip()
        if body and body not in placeholders:
            return True
    return False


def save_manual_context(title: str | None, values: dict) -> dict:
    """保存 UI 手动输入。写入 profile 文件中的专用块,不改自动摘要。"""
    if not title:
        raise ValueError("缺少联系人标题,请先读取一次对话。")
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    memory_store.ensure_contact(title, safe_name=_safe(title), db_path=MEMORY_DB)
    p = _profile_path(title)
    if not p.exists():
        p.write_text(_template(title), encoding="utf-8")
    clean = {key: (values.get(key) or "").strip() for key in MANUAL_SECTIONS}
    base = _strip_manual_block(p.read_text(encoding="utf-8"))
    p.write_text(base.rstrip() + "\n\n" + _manual_block(clean), encoding="utf-8")
    return clean


def load_memory(title: str | None, current_text: str = "") -> str:
    """返回供 prompt 使用的记忆全文。首次见到某人会自动创建可编辑档案模板。"""
    if not title:
        return ""
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    memory_store.ensure_contact(title, safe_name=_safe(title), db_path=MEMORY_DB)
    profile = _profile_path(title)
    if not profile.exists():
        profile.write_text(_template(title), encoding="utf-8")
    parts = [_strip_manual_block(profile.read_text(encoding="utf-8"))]  # 手动上下文已单独高优注入,去重
    summ = _summary_path(title)
    if summ.exists():
        summary_text = summ.read_text(encoding="utf-8")
        memory_store.replace_summary_facts(title, summary_text, safe_name=_safe(title), db_path=MEMORY_DB)
    compacted = memory_store.render_compacts(title, db_path=MEMORY_DB, query_text=current_text)
    structured = memory_store.render_facts(title, db_path=MEMORY_DB, limit=4, query_text=current_text)
    if compacted:
        parts.append(compacted)
    if structured:
        parts.append(structured)
    elif not compacted and summ.exists():
        parts.append(summ.read_text(encoding="utf-8"))
    return "\n\n".join(parts).strip()


def save_summary(title: str | None, text: str) -> Path:
    """写入「自动记忆」档案(<联系人>.summary.md),与用户手填的 profile 分开。
    历史导入蒸馏的结果存这里;load_memory 会自动把它并入 prompt。"""
    if not title:
        raise ValueError("缺少联系人标题,无法保存记忆。")
    MEM_DIR.mkdir(parents=True, exist_ok=True)
    p = _summary_path(title)
    p.write_text((text or "").strip() + "\n", encoding="utf-8")
    memory_store.replace_summary_facts(title, text or "", safe_name=_safe(title), db_path=MEMORY_DB)
    return p


def compact_source(title: str | None) -> str:
    if not title:
        return ""
    return memory_store.render_compact_source(title, db_path=MEMORY_DB)


def save_compacts(title: str | None, compact_json: str) -> int:
    if not title:
        return 0
    return memory_store.replace_compacts_from_json(
        title, compact_json or "[]", safe_name=_safe(title), db_path=MEMORY_DB
    )
