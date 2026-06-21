"""配置加载 + 运行时数据目录解析。

- 数据目录:开发态=项目目录(行为不变);打包态(py2app, sys.frozen)=
  ~/Library/Application Support/DraftMate(首次从 bundle 资源拷出 config.example→config.yaml、人设)。
- 配置:读 config.yaml,补默认值,处理模型回退。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# 数据目录创建/种子失败时记一条人类可读线索(copilot 启动时会打印出来),而不是静默崩在 import。
DATA_DIR_ERROR = ""

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


# ════════════════════ 数据目录(开发态 / 打包态:py2app / PyInstaller)════════════════════
_FROZEN = bool(getattr(sys, "frozen", False))
# 打包后只读资源目录:py2app 设环境变量 RESOURCEPATH;PyInstaller 设 sys._MEIPASS;
# 都没有(开发态)则取本文件所在目录。
_RES = Path(
    os.environ.get("RESOURCEPATH")
    or getattr(sys, "_MEIPASS", None)
    or Path(__file__).resolve().parent
)


def resource_dir() -> Path:
    """只读资源(默认人设 / 模板)所在目录。"""
    return _RES


def base_dir() -> Path:
    """可写用户数据目录(config.yaml / skills/memory / 截图 / 状态)。"""
    if not _FROZEN:
        return Path(__file__).resolve().parent          # 开发:项目目录
    # 打包态:按平台取标准的「每用户可写数据目录」
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        d = root / "DraftMate"
    elif sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "DraftMate"
    else:  # Linux 等
        root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
        d = root / "DraftMate"
    try:
        d.mkdir(parents=True, exist_ok=True)
        _seed(d)
        return d
    except OSError as e:
        # 受限/重定向 profile、满盘、无权限等 → 回退到临时目录,保证 app 仍能启动并留下线索
        global DATA_DIR_ERROR
        fallback = Path(tempfile.gettempdir()) / "DraftMate"
        try:
            fallback.mkdir(parents=True, exist_ok=True)
            _seed(fallback)
        except OSError:
            pass
        DATA_DIR_ERROR = f"无法写入数据目录 {d}({e});已回退到 {fallback}。配置/记忆将存这里。"
        return fallback


def load_env_file(path: str | Path | None = None) -> int:
    """Load simple KEY=VALUE pairs from .env without overriding shell env."""
    p = Path(path) if path else base_dir() / ".env"
    if not p.exists():
        return 0
    loaded = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or any(c.isspace() for c in key):
            continue
        if os.environ.get(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value:
            continue
        os.environ[key] = value
        loaded += 1
    return loaded


def _seed(d: Path) -> None:
    """首次运行:把只读默认拷进可写目录。单项拷贝失败(满盘/权限)不应整体崩溃,降级为缺该项。"""
    cfg = d / "config.yaml"
    if not cfg.exists():
        ex = _RES / "config.example.yaml"
        if ex.exists():
            try:
                shutil.copy(ex, cfg)
            except OSError:
                pass
    psrc, pdst = _RES / "skills" / "personas", d / "skills" / "personas"
    if psrc.exists() and not pdst.exists():
        try:
            shutil.copytree(psrc, pdst)
        except OSError:
            pass
    try:
        (d / "skills" / "memory").mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


# ════════════════════ 配置加载与默认值 ════════════════════
_DEFAULTS = {
    "provider": "anthropic",
    "ollama_host": "http://localhost:11434",
    "deepseek_base_url": "https://api.deepseek.com",
    "deepseek_thinking": "disabled",
    "deepseek_reasoning_effort": "high",
    "read_mode": "vlm",
    "ocr_backend": "auto",
    "me_side": "right",
    "crop_left": 0.0,
    "crop_bottom": 0.0,
    "model": "claude-sonnet-4-6",
    "vision_model": None,
    "reply_model": None,
    "app_name": "",
    "app_aliases": [],
    "read_last_n": 8,
    "poll_interval_seconds": 5,
    "history_days": 7,              # 导入历史默认采集「最近几天」(按系统时间戳停;UI 可选)
    "history_max_screens": 60,      # 硬上限兜底:最多自动滚多少屏(防解析失败时失控)
    "history_scroll_lines": 8,      # 每屏向上滚多少行(太大跳屏拼不上,太小慢)
    "default_persona": "serious",
    "contacts": {},
}


def load(path: str | None = None) -> dict:
    """读取 config.yaml,补齐默认值,处理模型回退。"""
    if yaml is None:
        raise SystemExit("缺少依赖 PyYAML,请先运行: pip install -r requirements.txt")
    load_env_file()
    p = Path(path) if path else base_dir() / "config.yaml"
    cfg = dict(_DEFAULTS)
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        cfg.update({k: v for k, v in data.items() if v is not None})
    # 单独指定的模型为空时回退到主模型
    cfg["vision_model"] = cfg.get("vision_model") or cfg["model"]
    cfg["reply_model"] = cfg.get("reply_model") or cfg["model"]
    cfg["contacts"] = cfg.get("contacts") or {}
    return cfg
