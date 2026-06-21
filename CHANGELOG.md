# Changelog

## Unreleased - 2026-06-21 (OCR 跨设备健壮性 · 第一轮)

### Changed
- **几何判定自校准(尺度无关)**:`detect_avatars` / `_content_mask` / 图片消息检测里所有"固定窗口比例/像素"的尺度(纹理块 `H//160`、头像高度带 `0.03–0.16H`、图片高度阈值 `0.04/0.13H`)改为**锚定文字行中位高 `mh`**。原因:真实 UI 头像是固定像素、不随窗口高度成比例,旧的 `0.03H` 下限在高窗口/4K/高 DPI 上会把真实头像判得太小而漏检,连带图片/表情消息(锚在头像上)一起丢。`process_image` 统一算 `mh` 下传;`detect_avatars(path,W,H,mh=None)` 向后兼容(无 mh 时行为不变)。
- **内容掩码无条件计算**:`_content_mask` 不再被 `if avatars` 卡住,为后续"内容锚定"图片检测铺路。
- **系统消息加正信号门槛(M3)**:`process_image` 中"居中且窄→system"的几何判定后增加 `_looks_system_text` 检查——只有含日期/时间/撤回/拍了拍/红包等系统特征才保留 system,否则按位置低置信归属,避免把"好的/在吗"这类被居中的短真消息误删成系统提示。

### Added
- `tools/ocr_geometry_harness.py`:OCR 几何健壮性测试台。monkeypatch `run_ocr` 注入合成行,在 **3 种渲染尺度 × 明暗主题** 下端到端验证发言人判定/头像检测/图片消息识别不漂(不依赖真实 OCR、确定性)。本轮改动前 small/medium 尺度图片消息漏检,改动后全矩阵 PASS。

### Note
- 这是路径 B(死磕本地 OCR 健壮性)第一轮,主攻**尺度/DPI/分辨率无关性**。仍未做:`crop_left` 固定比例→动态会话栏边界检测(H4,需真机微信截图调参)、群聊按头像绑发言人(M2)、深色主题下纯色/字母头像的自适应阈值(M1 完整版)。**待办与状态跟踪见 `docs/ocr-robustness-plan.md`**,风险全清单见 `docs/windows-robustness-audit.md`。

## Unreleased - 2026-06-20 (Windows 跨平台支持)

### Added
- **Windows 读屏后端**(`vision.py`):新增 `_IS_MAC/_IS_WIN` 平台分流。Windows 走 Pillow `ImageGrab` 截目标窗口区域、`pygetwindow` 按窗口标题定位窗口、ctypes 合成滚轮(`history.scroll_up`)与置前(`activate`);macOS 路径完全不变。进程级设 DPI 感知,使窗口坐标与截图物理像素在缩放屏下对齐。`grab/window_box/list_window_owners` 按平台分流,`_osascript` 在非 mac 上返回空串绝不抛错。实测真机:窗口枚举/定位/区域截图全通(douyin 窗口 2304×1315 精确裁剪)。
- **CI 自动打包**(`.github/workflows/build.yml`):推 `v*` 标签 → windows + macos runner 各自 PyInstaller 打包 → 产物挂到同一 Release(`DraftMate-Windows.zip` + `DraftMate-macOS.zip`)。手动触发只产 artifact 不发布。
- `requirements.txt`:补 Windows 专属依赖(`pillow/numpy/pygetwindow`,带 `sys_platform == "win32"` 标记,mac 自动跳过)。README 增 Windows 安装/运行/平台差异说明。

### Fixed
- `memory_store.py`:`datetime.UTC`(Python 3.11+ 别名)→ `datetime.timezone.utc`,**3.10+ 通用**(此前在 3.10 上 8 个测试 AttributeError;mac 用 3.10 同样受益)。
- `test_draftmate.py`:修两处 Windows 文件句柄锁(`mkstemp` 未 `close(fd)`、`sqlite3.connect` 的 `with` 不关连接致 `TemporaryDirectory` 清理 WinError),改用 `os.close` + `contextlib.closing`。Windows + Python 3.10 现 **40 测试全过**。
- `copilot.py`:打包后(`sys.frozen`)默认开 pywebview 窗口,缺 WebView2 时**回退浏览器模式不再崩**;入口将 stdout/stderr 切 UTF-8,避免非 UTF-8 控制台打印中文 `UnicodeEncodeError`。同理修 `scripts/api_chat.py`。
- `config.py`:打包态数据目录按平台分(Windows=`%LOCALAPPDATA%\DraftMate`、macOS=`~/Library/...`、Linux=XDG);冻结资源目录兼容 PyInstaller 的 `sys._MEIPASS`(原仅 py2app `RESOURCEPATH`)。

### Note
- Windows 截图用 `ImageGrab` 截「屏幕可见区域」,目标窗口需在前台、未遮挡、未最小化(不像 macOS 能截被挡住的窗口)。`app_name` 在 Windows 按**窗口标题**匹配。OCR 原生 Vision 不可用,`read_mode: ocr` 需装 easyocr/paddleocr/tesseract;默认 `vlm` 模式无需 OCR。

## Unreleased - 2026-06-10 (回归测试套件)

### Added
- `test_draftmate.py`:23 个 unittest(stdlib,无新依赖),覆盖今天新增的核心纯逻辑——历史拼接去重(stitch/重叠/到顶/发言人指纹)、微信时间戳解析(纯时间/昨天/星期X/年月日/月日 + OCR 容错"昨大" + 乱码 + 单调取最早)、agent render 截断/分人设温度/手动上下文渲染、人设 `.local.md` 回退、记忆 save/load 往返(临时目录隔离)、用量手动/自动分计、云端无 key 必本地。0.01s 跑完,不依赖 ollama/网络/截图。
- 命令:`.venv/bin/python -m unittest test_draftmate -v`。补上此前裸奔的 ~1300 行新代码的回归保护。

## Unreleased - 2026-06-10 (可选云端模型开关)

### Added
- 模型下拉新增**云端 claude 选项**(claude-sonnet-4-6 / haiku-4-5 / opus-4-8):仅在检测到 `ANTHROPIC_API_KEY` + anthropic 包时出现,带 ☁ 标记;切换时弹确认"会把对话文字发往 Anthropic(读图/截图仍全本地、不上传)";没 key 时不打扰、改显示开启指引。**默认仍本地 Ollama、隐私红线不变**——云端只用于回复生成、只传文字。
- `_cloud_available()` 检测 key+包,缺任一即回落本地(容错);`list_models` 返回项带 `cloud` 标记 + `cloud_available`。llm 早已按模型名路由(claude-* 走云端),本次把入口接到 UI。

### Note
- 分发的 `.app` 默认**不打包 anthropic**(保持本地纯净、包小、契合隐私卖点);云端开关在源码/开发态(venv 装 anthropic)可用。要让分发版也支持云端,需把 anthropic 加进 py2app includes(有打包依赖风险:httpx/pydantic 等,Gate 1 后再评估)。Phase 0 验证主打本地版,分发版不带云端反而契合"截图不出本机"的演示。

## Unreleased - 2026-06-10 (历史导入:按天数决定采集范围)

### Added
- 导入历史从「按屏数」改为**「按天数」**:解析微信系统时间戳(相对戳"昨天"/"星期三"/纯时间 → 绝对日期,以**采集当下**为基准,容错 OCR 如"昨大"),滚到「最近 N 天」边界即停;`history_max_screens`(60) 降级为硬上限兜底。设置面板加天数下拉(最近 3/7/14/30 天 / 全部),默认 7 天;进度显示"已滚到 M-D"。
- `history.parse_wechat_date` + `_earliest_in_screen`:单调约束(只接受更早日期)吸收 OCR 把"星期二/三"读混的 ±1 天抖动。

### Why
- 解决之前记录的"采集范围不可预期"(每次打开对话停的位置不同→覆盖的消息段不一致):现在范围明确=最近 N 天,符合"导入最近一周"的心智。

### Verified
- 解析器对真实噪声戳 39/39=100%(含"昨大");8 个边界单测全过(纯时间/昨天/星期X/年月日/月.日/乱码);真机 days=1 实测 **2 屏精确停在 cutoff**(reached_target=True,未滚满上限)。

## Unreleased - 2026-06-10 (历史导入:自动滚动 + 蒸馏记忆)

> 痛点:逐张截图太累、爬数据库太重。解法:点一下,自动滚完当前对话,蒸馏成长期记忆。

### Added
- 新模块 `history.py`:自动滚动当前对话 + 多屏截图 OCR 去重拼接 → 一段连续历史。
  - **M3 自动滚动**:Quartz CGEvent 合成滚轮(**只读导航**);方向自适应(自动应对「自然滚动」开关,首次若反向会自纠);开始前激活目标窗口;`accessibility_ok()` 检测辅助功能权限并引导。
  - **M2 去重拼接**:按消息指纹(发言人+文本前 24 字)找相邻屏重叠,拼成连续历史;连续两屏无新增 = 到顶。
  - 滚动定位改用 Quartz `vision.window_box`(和截图同源),**不依赖 AppleScript/System Events**——后者需 Apple Events 自动化权限,本机三个进程名实测全 None。
- `agent.distill_memory`:整段历史蒸馏成结构化记忆(关系背景/画像/雷区/共同经历/承诺待办/氛围),M1 离线验证过,压缩到 ~50%。
- `skills.save_summary`:写入 `<联系人>.summary.md`(自动记忆,与手填 profile 分开;`load_memory` 自动并入 prompt)。
- copilot:设置面板「导入历史记忆」按钮 + 后台线程 + 进度轮询(POST `/api/import_history` / GET `/api/import_status`);完成后把记忆档案显示在 AI 分析卡。
- config:`history_max_screens`(25)、`history_scroll_lines`(8)。

### Product
- 「不模拟键鼠」对外表述收窄为「只读屏 + 只读滚动导入历史,永不替你发送」——自动滚动是只读导航,**红线 3(永不自动发送)继续死守**。用户在论文 review 等待窗口期临时放开红线 1 投入此功能。

### Fixed
- **自动滚动激活失败(实测根因)**:原 `activate()` 走 AppleScript/System Events,本机 Apple Events 权限不通(三个进程名全 None),微信没被切前台→滚轮落到别的窗口、采集 0 新增。改用 **Cocoa `NSRunningApplication`**(`vision.window_pid` 拿 PID)激活,绕开 AppleScript;CGEvent post 权限实测有效。修复后真实滚动正常,15 屏稳采 121 条、顺序连贯、去重无断层。
- **蒸馏在真实长噪声数据上泛化/幻觉**:7B 把口头梗当实体("冷知识"当游戏)、一次性提及当爱好(原神)、个别 sender 认错。对策=蒸馏 prompt 强制每条附原文引用 `[据:"原话"]`、编不出引用不准写、禁常识联想;固化后主干忠实、可逐条核对。
- **长输入(190 条)下归类错位**(把球赛闲聊塞进'承诺待办'):`distill_memory` 改为 **map-reduce**——分块(每 35 条)摘录带类型标签的要点(短输入→归类准、明确"球赛评论不算承诺")→ 合并归类去重;短历史(≤35)仍走单次。实测 190 条:承诺栏错位基本压住、主干更全;残留结构整洁度/球赛吐槽误判属 7B 上限(升级 14b/claude 质变,路已留);代价是慢(6 段约 157s,导入为一次性可接受,UI 显示"摘录 N/M 段"进度)。

### Verified
- 真机实测(微信 Nelson 对话):Cocoa 激活成功→自动滚动真正生效;拼接顺序/去重正确、无断层;蒸馏主干准确(社恐/roguelike/原神氪金/鸣潮诈骗/纽约成本全部命中且如实标"承诺无")。
- 方法论教训:首轮误判"蒸馏严重幻觉",实为两次采集**起点不同→覆盖范围不同**(第一次有篮球、第二次 92 条篮球 0 命中),拿不同数据的印象互评所致;经用户核对纠正。

## Unreleased - 2026-06-10 (军师层:关系阶段判定)

### Added
- **`agent.assess_stage()` 军师判定**:每次读取先用回复模型做一次低温短输出的关系阶段估计——L0–L5/D1/D2 八等级 + 判定规则(两类独立证据才升级、拿不准取更低、只引用真实对话、秒回/表情=弱证据),蒸馏自 13 项研究的阶段判定法(本地 skill 资产去名化入 App);非恋爱语境(同事/事务)自动输出「不适用」。输出三行:阶段(置信度)/依据(引用片段)/策略(只给方向,禁示例句)。
- 判定结果接入两处:①「AI 分析」卡显示真判定(原为 4 条写死的 JS 规则,降级为兜底);②作为 `stage_hint` 喂给每条草稿与「换个说法」——按阶段校准火候,不越级推进(实测 L1 判定下三个人设都不再直接约饭)。
- 判定失败不挡草稿生成;`/api/regenerate` 接受前端回传的 `analysis` 复用判定,不重复计算。

### Verified
- 暧昧语境 → L1(中置信)+真实片段引用;同事语境 → 「不适用(非恋爱语境)」(高置信)。
- 修掉一个真实坑:判定策略行写了示例句导致 7B 草稿整句照抄、三候选趋同——规则禁台词 + 草稿侧声明"方向非措辞"后,三人设输出重新分化。

## Unreleased - 2026-06-10 (UI 交互修复 + 真·监控)

### Added
- **「监控」开关(用户拍板把 Backlog 提前)**:开启后每 `poll_interval_seconds`(默认 5s,重新入册到配置默认值)秒调用新的 `GET /api/peek`——只截屏+OCR 取最后一条非系统消息指纹,**不生成**;指纹变化且新消息来自对方时,才触发一次完整读取+生成。仍只读屏、不模拟输入、不发送(红线 3 不动)。开启时 REC 指示与 run-pill 显示「监控中」,tooltip 写明行为。
- **模型下拉改为真正的选择器**(原来点开是一堆运行信息):`GET /api/models` 列出本地 Ollama 全部已 pull 模型(当前值不在列表也带上,如 claude-*),点选即 `POST /api/model` 切换——内存立即生效,并只改写 config.yaml 的 `reply_model` 一行(行尾注释保留)。运行信息网格移入设置(⚙)面板的「运行信息」区。
- 用量计数拆分手动/自动:`usage.auto_reads` 单独记监控触发的读取,角标显示「已读取 N 次 (自动 M)」——周留存指标只认手动,防挂机刷数。

### Fixed
- 左侧「待命」指示器现在接入真实生命周期:读取中 REC「读取中」、监控开启「监控中」、空闲「待命」(原来只挂在装饰开关上,点读取毫无反应)。
- `/api/read` 支持 `?auto=1` 标记监控触发;Handler 路由统一拆 query。
- **监控开启后 30s 无反应(用户实测)**:原逻辑只对开启之后的新消息反应且全程静默——预期不符 + 不可观测。改为:①开启时若屏幕上最后一条是对方的未回消息,立刻先出一版草稿;②每次探测在状态栏打心跳(`监控中 · HH:MM:SS 已探测,无新消息`);③探测失败不再吞掉,显示具体错误。
- 模型选择器实测正常(用户点击已把 reply_model 切到 qwen2.5vl:7b 并正确写回配置)——已把配置恢复为 qwen2.5:7b 文本模型,VL 模型聊天质量明显更平。
- 监控范围据实重写文案(用户实测指出):只盯**当前打开的对话**——微信不点开会话不渲染内容,平台物理限制;tooltip/状态栏/分发说明同步说清。侧栏红点检测与多窗口监控列为「明确不做」(见 PLAN Backlog)。

## Unreleased - 2026-06-10 (回复质量:去同质化 + 撩感)

> 问题:三条候选一个味、全是客服安慰腔、零撩感。病因三个,全部修掉。

### Changed
- **回复模型切到文本模型**:`reply_model: qwen2.5:7b`(config.yaml 与 example 均默认),原来回退到 `qwen2.5vl:7b`——7B 视觉模型客串中文聊天是同质化首因。视觉模型只管读图。
- **人设从"形容词"改成"少样本示例"**:四个 persona 全部重写为「定位 + 语气 + 4–6 条示例对话 + 禁忌」结构——7B 模型无法从"语气:亲和幽默"演出人设,只能模仿示例。flirty/shenqing 的技法蒸馏自深情流方法论(推拉、给台阶式邀约、给确定感不舔、点到为止、反鸡汤),已去名化。示例刻意避开高频输入句,防止小模型整句照抄(测试中实际发生过)。
- **按人设分采样温度**:`agent.temperature_for()`——serious 0.5 / casual 0.75 / shenqing 0.8 / flirty 0.85(原来统一 0.4,低温+同 prompt 是同质化次因);「换个说法」再 +0.15 防重生成出同一句。
- **prompt 重排**:人设放 system 最前定调、硬规则压轴(小模型对开头结尾最敏感);新增反客服腔黑名单(「多喝热水/注意休息哦/加油哦/辛苦啦」禁用)、角色锚定(别把对方处境安自己头上)、一条回复最多一个问题;删掉"默认不加表情符号"(交由人设决定)。
- **llm 按模型名路由后端**:`reply_model` 填 `claude-*` 可单独让"回复生成"上云(只传对话文字、不传截图,读图仍全本地),质量天花板留口子;默认配置仍全本地,需自带 ANTHROPIC_API_KEY。
- `分发说明.md` 安装步骤补上 Ollama 安装与模型拉取(原"三步"漏了整个模型环节,种子用户装完必卡)。

### Verified
- 固定对话 × 4 人设 × 3 轮对比:改前四条几乎同句型、零邀约;改后人设分化明显(serious 落到事/casual 接梗怼/flirty 邀约带暧昧/shenqing 接情绪给确定感),客服腔消失。7B 仍偶发嘴瓢与双问句,属模型上限,升级路已留(qwen2.5:14b 本地 / claude-* 文字上云)。
- 同步打包态数据目录(~/Library/Application Support/DraftMate):reply_model、四个新人设;真名人设按约定改 `.local.md`(seed 只在首装拷贝,不同步会导致 .app 用旧人设)。

## Unreleased - 2026-06-10 (Phase 0 产品侧)

### Added
- 仅本地的用量计数(隐私承诺内的最低成本度量):`usage.json` 记累计「读取」次数 + 最近使用日期,状态栏角标展示(title 注明"仅本地统计,不上传");`/api/status` 与读取返回的 `status` 均带 `usage`。无任何遥测,周报靠用户自愿截图角标。
- `分发说明.md`:给种子用户的一页说明(是什么 / 隐私承诺 / 安装三步 / 已知限制 / 反馈方式)。
- `指标记录.md`(含真名,不入库):用户总表 + W1–W7 周记录 + Gate 1 判据与结论页,定义复制自 PLAN.md。

### Changed
- **去名化(变现前红线)**:`skills/personas/tongjincheng.md` 改为本地私有 `tongjincheng.local.md`(已 `git rm --cached`,不入库、不入分发包),对外改为抽象流派 `shenqing.md`(深情流);copilot UI 标签映射、README、config.example 同步替换。建立 `*.local.md` = 本地私有人设约定:`load_persona` 先找 `<名>.md` 再回退 `<名>.local.md`,`setup_app.py` 打包过滤 `*.local.md`,.gitignore 同步。
- `config.example.yaml` 默认 `app_name: "WeChat"` + 常见别名(原默认空值会让打包态首跑必报"未配置",是种子用户第一个卡点)。
- 状态栏诚实化:隐藏纯装饰的「监听」开关(原 `toggleRunning` 只改样式、无轮询逻辑,会误导隐私预期),去掉假的「自动截图 5s」字样(改"手动读取",运行时被实际读取模式覆盖),「监听」标签改「当前」;新增用量角标。

### Verified
- `py_compile` 全部源文件通过;计数器/人设加载/页面内容单测通过;8766 端口 Handler 冒烟 + 打包后 App 实际启动并响应 `/api/status`、页面含角标。
- 打包产物全包扫描无「童锦程/tongjincheng」字样;bundle 内 personas 仅 serious/casual/flirty/shenqing 四个。

### Dev note
- 项目目录改名(AutoTalk→DraftMate)导致 `.venv/bin/pip` 等脚本 shebang 指向死路径;包本体完好,用 `.venv/bin/python -m pip` 绕过,后续可重建 venv。

## Unreleased - 2026-06-07 (后续)

### Added
- Copilot UI 打磨:
  - 「目标(阶段性)」快捷预设按钮(认识→暧昧 / 约出来 / 确定关系 / 维持朋友),一键填入。
  - 建议回复改为可编辑文本框,可先改后复制(复制的是改后的内容)。
  - 每条建议加 ↻「再生成」按钮:复用当前对话、同人设单独重出一条,无需整页重读。
  - 快捷键 ⌘R / Ctrl+R 触发「读取」。
  - 新增 `/api/regenerate` 接口(POST `{title, persona, messages}` → 单条建议)。
- py2app 打包:新增 `setup_app.py` + `appdirs.py`,可构建自包含的 `DraftMate.app`(`python setup_app.py py2app`)。打包态把用户数据(config / 记忆 / 截图)移到 `~/Library/Application Support/DraftMate`,开发态路径不变。

### Changed
- 文件合并(功能不变,仅整理结构):`appdirs`→`config`、`capture`+`chat_ocr`→`vision`、`persona`+`memory`→`skills`;每个合并文件内用区域注释分段。源码模块 11 → 7 个。
- 项目更名 **AutoTalk → DraftMate**:GitHub 仓库、README/界面文案、打包(`CFBundleName`、bundle id `local.draftmate.copilot`、`DraftMate.app`)、数据目录(`~/Library/Application Support/DraftMate`)、本地文件夹一并更新;内部记忆标记(`autotalk:manual-context`)保持不变以兼容已存档案。README 简介改为更中性的表述。
- Copilot UI 重做为暖黑 + 琥珀金的「Focus」设计:三层布局(标题栏 / 状态栏 / 左截图 + 右建议)、AI 分析卡、带语气标签与「★推荐」的回复卡;加载 Bricolage Grotesque / IBM Plex 字体(离线回退系统字体)。
- 手动上下文用持续的「目标(阶段性)」(`goal`)取代逐条输入的「我这次想表达」(`reply_intent`):设一次长期生效,agent 朝该阶段性目标循序渐进地给建议,不再要求每条都手填意图。同步更新 `agent` 回复策略、`copilot` UI 标签/占位与前后端字段。

### Fixed
- `memory.load()` 现在剥掉手动上下文块,避免它在 prompt 里重复(该块已单独以最高优先级注入)。

### Removed
- 精简为 copilot-only:删除自动发送链路与多余入口/界面 —— `main.py` / `watcher.py` / `sender.py` / `confirm.py` / `confirm.applescript`(自动发送流程)、`menubar.py`(菜单栏 App)、`doctor.py` / `snap.py` / `selftest.py`(排错/测试工具);均不被 copilot app 依赖。同步精简 `setup.sh` / `setup_app.py` / README,删除旧打包产物。
- 清理随之失效的死代码与配置:`memory.update()`(发送后自动摘要)、`capture.file_hash()`,以及配置项 `poll_interval_seconds` / `dry_run` / `send_with` / `update_memory` / `summary_model`。
- 去掉 profile 模板里多余的「当前目标」一行(由结构化的目标字段取代,避免两个"目标")。

## Unreleased - 2026-06-07

### Added

- Added the local copy-only Copilot UI in `copilot.py`.
  - Shows the actual analyzed screenshot/crop on the left.
  - Shows parsed conversation messages and 2-3 suggested replies on the right.
  - Provides copy buttons instead of automatic send actions.
  - Exposes runtime status for target app, provider, read mode, reply model, persona, and copy-only mode.
- Added `/api/status` for non-secret UI runtime metadata.
- Added `/api/context` for saving per-contact manual context locally.
- Added per-contact manual context storage in `skills/memory/<contact>.md`.
  - `对方信息`
  - `我这次想表达`
  - `不要提/边界`
  - `备注`
- Added UI controls for saving contact context and regenerating suggestions.
- Added README run commands for:
  - `python copilot.py`
  - `python copilot.py --window`

### Changed

- Improved reply-generation strategy in `agent.py`.
  - Manual context now has highest priority.
  - The agent is instructed to answer the other person's latest question first.
  - The agent is instructed not to repeat or re-ask questions the other person already asked.
  - Temperature and token budget were tightened for shorter, more direct replies.
- Kept the safer product direction as a reply copilot:
  - read screen
  - show analyzed context
  - generate draft
  - user copies manually
  - no keyboard simulation or automatic send from Copilot UI

### Confirmed Behavior

- The reading pipeline already crops before OCR:
  - `capture.grab()` captures the target app window.
  - `vision.read_messages()` calls `_apply_crop()`.
  - OCR runs on the cropped image path.
- The Copilot UI also renders the cropped analysis preview, so the user can inspect what the agent actually read.
- Local memory files remain under `skills/memory/`, which is ignored by git for private `.md` and `.summary.md` files.

### Verified

- Python syntax check passed:
  - `.venv/bin/python -m py_compile copilot.py agent.py memory.py watcher.py selftest.py`
- Manual-context save/load was tested against a temporary memory directory.
- Temporary local UI verification passed on:
  - desktop viewport
  - mobile viewport
- Browser console showed no errors or warnings during UI verification.

### Notes

- If the analyzed preview still includes the bottom input toolbar, tune `crop_bottom` in `config.yaml`.
- If the contact title is read as `unknown`, context can still be saved, but the better fix is to improve title detection or set per-contact memory after a reliable title is available.
- The next quality step is to let the user choose or type the current intent before generation, so the agent stops guessing between paths such as rental, green card, or general small talk.
