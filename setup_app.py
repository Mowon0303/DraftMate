"""py2app 打包配置 —— 把副驾打成自包含的 DraftMate.app。

构建:
    source .venv/bin/activate
    pip install py2app
    python setup_app.py py2app

产物:dist/DraftMate.app(可拷走)。首次启动:右键→打开(未签名);
系统设置→隐私与安全性→屏幕录制 勾上 DraftMate。
用户数据在 ~/Library/Application Support/DraftMate(config.yaml / skills/memory / 截图)。
本包面向本地 ollama;若要云端 Claude,需另把 anthropic 加入 includes。
"""
import glob

from setuptools import setup

APP = ["copilot.py"]


DATA_FILES = [
    ("", ["config.example.yaml"]),
    # *.local.md 为本地私有人设(如真名版),永不进分发包
    ("skills/personas", [p for p in glob.glob("skills/personas/*.md")
                         if not p.endswith(".local.md")]),
    # LoveHelper:运行时只读这两个(均不含真名);含真名的研究资料绝不入分发包 —— 去名化红线。
    ("skills/lovehelper", ["skills/lovehelper/draftmate-adapter.md"]),
    ("skills/lovehelper/relationship-copilot/references",
     ["skills/lovehelper/relationship-copilot/references/human-progression-playbook.md"]),
]

OPTIONS = {
    "argv_emulation": False,
    "packages": ["PIL", "webview"],
    "includes": [
        "agent", "config", "history", "llm", "skills", "vision",
        "memory_store",
        "yaml", "objc", "proxy_tools", "bottle", "typing_extensions",
        "Foundation", "AppKit", "WebKit", "Quartz", "Vision",
    ],
    "plist": {
        "CFBundleName": "DraftMate",
        "CFBundleDisplayName": "DraftMate",
        "CFBundleIdentifier": "local.draftmate.copilot",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1.0",
        "NSHighResolutionCapable": True,
        "LSUIElement": False,
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
