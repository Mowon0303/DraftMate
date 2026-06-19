# DraftMate

桌面回复副驾（macOS）—— 读取屏幕上的对话，套「人设 + 记忆」生成**可复制的回复草稿**，由你过目后自己发送。可全程本地（Ollama），数据不出本机。

> **只读屏 + 只复制，永不替你发送**：DraftMate 读出对话、在右侧给几条候选回复，你点「复制」再自己粘贴到聊天框——把账号风险降到最低，也让你对每一句话有最终决定权。唯一会模拟键鼠的地方是「导入历史」时的**自动滚动**（只读浏览、由你点按钮触发），绝不模拟输入文字或点发送。

```
截取目标 App 窗口
  → 读出最近对话(谁说了什么)                       [OCR 几何判定 / 视觉模型直读]
  → 套「人设 + 联系人记忆 + 阶段性目标」生成候选回复   [本地 Ollama 或 云端 Claude]
  → 你在右侧过目/编辑 → 点「复制」 → 自己粘贴发送
```

---

## 一键安装

```bash
cd DraftMate
bash setup.sh                        # 建 .venv、装依赖(含 pywebview)、自检
cp config.example.yaml config.yaml   # 复制配置模板,然后填好 app_name
```

`setup.sh` 在 macOS 上会装最轻量的本地 OCR 后端(系统原生 Vision,免下模型)。

**授权**:日常用(读屏+复制)只需在 系统设置 → 隐私与安全性 → **屏幕录制** 里勾上你运行它的**终端**(或打包后的 `DraftMate.app`),重启生效。**只有用「导入历史」的自动滚动**才另需在 **辅助功能** 里也勾上它(模拟滚轮事件需要)。

## 配置目标应用(必填)

在 `config.yaml` 里填目标 App:
```yaml
app_name: "你的App应用名"      # 系统里显示的应用名/进程名
app_aliases: []               # 若该 App 窗口属主名是本地化名(和应用名不同),在此补上
```
不确定窗口属主名时,列出当前所有窗口的 owner 名照着填:
```bash
.venv/bin/python -c "import capture; print(capture.list_window_owners())"
```

## 后端(`provider` / `reply_model`)

| | 本地 Ollama(默认) | 云端 Claude | 云端 DeepSeek |
|---|---|---|---|
| 配置 | `provider: ollama` / `model: qwen2.5vl:7b` | `reply_model: claude-sonnet-4-6` | `reply_model: deepseek-v4-flash` |
| 需要 | `ollama pull qwen2.5vl:7b` | `.env` 里填 `ANTHROPIC_API_KEY=...` + `pip install anthropic` | `.env` 里填 `DEEPSEEK_API_KEY=...` |
| 隐私 | **截图不出本机** | 只把回复生成的对话文字发往 Anthropic | 只把回复生成的对话文字发往 DeepSeek |
| 质量 | 够用 | 更强 | 更适合长 playbook 中文回复 |

本地密钥写到项目根目录 `.env` 即可,也可以继续用 shell `export`。`.env` 已被 `.gitignore` 排除,不要提交真实 key:

```bash
cp .env.example .env
# 然后填入:
# DEEPSEEK_API_KEY=sk-...
```

## 两种读取模式(`read_mode`)

- **`ocr`(推荐)**:本地 OCR 拿到每个文字的精确坐标 + 检测头像,用**几何规则**判定发言人——贴右=我、贴左=对方、居中=系统。比让模型"猜"更稳。需 OCR 后端(macOS 原生 `vision` 最轻量,`setup.sh` 已装)。复用自带的 `chat_ocr.py`(自包含)。
- **`vlm`**:直接让视觉模型读整张图。最简单、不用装 OCR 后端,但小模型对左右气泡的发言人判定容易出错。OCR 读不出时会自动回退到 `vlm`。

## 能力

- **1v1 发言人判定**:我 / 对方 / 系统(时间戳),几何判定。
- **群聊多人区分**:识别气泡上方的昵称小字,把每条消息绑到对应的人。
- **图片/表情**:标成 `〔图片〕`/`〔表情〕`;表情包上的字会和图合并成一条。
- **会话名**:从标题栏提取,用于按联系人绑人设/记忆。
- **人设 + 记忆 + 阶段性目标**:每个联系人一份可编辑档案 + 一个长期「目标」,生成的回复朝目标循序渐进。
- **军师判定(阶段感知)**:每次读取先对关系阶段做一次证据化估计(L0–L5 升温 / D1 降温 / D2 风险;非恋爱语境自动标"不适用"),显示在「AI 分析」卡,并作为火候校准喂给草稿生成——避免在试探期输出确定关系的话术。
- **一键导入历史记忆**:设置面板选「最近 N 天」(读微信时间戳决定范围,默认 7 天)点「导入历史」,自动滚读当前对话、OCR 去重拼接、蒸馏成结构化记忆(关系背景/画像/雷区/共同经历/承诺),存入该联系人档案,之后回复就带着背景。长历史用 map-reduce 分段蒸馏抗 7B 归类错位。自动滚动是**只读导航**(需「辅助功能」权限),仍不替你发送。

## 运行

```bash
source .venv/bin/activate
python copilot.py            # 浏览器副驾:左看截图、右看候选回复,点「复制」
python copilot.py --window   # 原生窗口(需 pywebview)
python -m unittest test_draftmate -v   # 跑回归测试(纯逻辑,不需 ollama/截图)
```

打开后点「读取」(快捷键 ⌘R / Ctrl+R)即可。仅监听本机 `127.0.0.1`,不对外暴露。

**只测试 API 模型对话**:

```bash
python scripts/api_chat.py "用一句话解释什么是 API"   # 单轮
python scripts/api_chat.py                            # 交互聊天,输入 q 退出
```

默认读取 `.env` 里的 `DEEPSEEK_API_KEY`,使用 `config.yaml` 里的 `reply_model` 和 `deepseek_base_url`。也可以临时覆盖:

```bash
python scripts/api_chat.py --model deepseek-chat --base-url https://api.deepseek.com
```

**打包成 App**(可双击、可拷走):
```bash
pip install py2app
python setup_app.py py2app   # 产物 dist/DraftMate.app
```
首次启动右键 → 打开(未签名),并在「屏幕录制」里勾上 DraftMate。用户数据在 `~/Library/Application Support/DraftMate`。

## 目录结构

```
DraftMate/
  copilot.py           副驾主程序:本地网页 UI + 截图预览 + 候选回复 + 复制
  vision.py            读屏全链路:截图与窗口定位 + OCR/几何发言人判定 + 结构化对话
  llm.py               模型调用层(ollama / anthropic 双后端)
  agent.py             拼 prompt(人设 + 记忆 + 目标 + 对话)-> 候选回复
  skills.py            人设(personas)+ 联系人记忆(人工档案 + 手动上下文)
  config.py            配置加载 + 数据目录解析(开发态 / 打包态)
  config.example.yaml  配置模板(复制为 config.yaml)
  setup.sh             一键建环境 + 自检
  make_app.sh          生成可双击的启动器(开发态用,产物不入库)
  setup_app.py         py2app 打包配置(自包含 .app)
  skills/
    personas/          人设:serious / flirty / casual / shenqing(深情流)
    memory/            每个联系人一个可手改 Markdown 档案(运行时生成,不入库)
    lovehelper/        内置 LoveHelper.skill 资源 + DraftMate 精简适配规则
  memory.sqlite3       结构化联系人事实库(运行时生成,不入库)
```

## 加 skill

- **加人设**:在 `skills/personas/` 丢个 `xxx.md`(写清"什么语气")。生效方式:全局 `default_persona: xxx`,或按联系人 `contacts: {会话名: xxx}`,或在 `skills/memory/<名字>.md` 写 `人设: xxx`。文件名以 `.local.md` 结尾的人设只在本机生效(不进 git、不进打包),适合放私人版本。
- **调记忆**:联系人首次出现会自动生成 `skills/memory/<名字>.md`,手填关系/偏好/阶段性目标,代码不覆盖。历史导入仍会保存 `<名字>.summary.md`,同时拆成结构化 facts 写入本地 `memory.sqlite3`,再自动 compact 成长期记忆。运行时注入顺序是 Markdown 手动档案 → SQLite compact → 当前话题相关 facts;没有结构化记忆时才回退到原始 summary。
- **LoveHelper**:`skills/lovehelper/` 是从 LoveHelper.skill 拷入的恋爱副驾资源。阶段判定加载 `draftmate-adapter.md` 作为精简规则;生成回复时会同时加载 `relationship-copilot/references/human-progression-playbook.md`,让 playbook 参与决策层和口吻层。完整 `SKILL.md`/`references/` 保留给后续维护和溯源。

## 已知限制 & 风险

- 读取靠 OCR/视觉,复杂排版/异形表情/深色截图可能识别不全。
- 截图按窗口 ID 抓取,请保持目标窗口未最小化;`crop_left / crop_bottom` 按你的窗口尺寸微调。
- 对没有官方开放接口的 App 做自动化可能违反其用户协议、并有账号风险。DraftMate 因此**只读屏 + 只复制**、不自动发送:逐条人工确认、保持人类节奏。
- 隐私:`vlm` + 云端会把截图发往云端;`ocr` + 本地全程不出本机。本地生成的截图 / 联系人记忆 / 配置都不会进仓库(见 `.gitignore`)。
