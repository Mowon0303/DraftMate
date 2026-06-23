# DraftMate Windows 跨设备健壮性审计

> 面向「发布给不同设备的用户」做的对抗式审计(51 个分析/验证 agent,逐条对照真实代码)。
> 结论:**OCR + 截图链路对设备高度敏感**,自己一台机调好能用 ≠ 发出去稳定。
> 日期:2026-06-20。代码基线:`main` @ cb511b6 + 本地 Windows 配置。

## 各维度脆弱度

| 维度 | 评级 |
|---|---|
| DPI 缩放 | fragile |
| 分辨率 / 固定裁剪比例 | **very_fragile** |
| 截图方式(ImageGrab 截屏幕区域) | **very_fragile** |
| OCR 精度 | **very_fragile** |
| 窗口标题匹配 | fragile |
| 发言人几何判定 | **very_fragile** |
| 打包 / 分发 | **very_fragile** |

核心原因:macOS 按**窗口 ID 截窗口自身像素**,Windows 这版用 `ImageGrab` 截**屏幕上那块坐标**,所以"截到什么"取决于那一刻屏幕显示什么;再叠加一堆**固定像素/比例假设**(裁剪、发言人几何、头像阈值),换设备就漂。

---

## 🔴 高危(常见设备就会中,发布前必处理)

### H1. 窗口被遮挡 → 静默截到压在上面的窗口 — ✅ 已解决(2026-06-22)
- **位置**:`vision.py` `_win_grab`(ImageGrab over window bbox)、`_win_best_window`(不查 z-order/遮挡)
- **触发**:目标窗口上面压了浏览器/资源管理器/通知 toast/输入法候选框/画中画——单屏笔记本极常见
- **影响**:静默读到**别的窗口**的像素,把别人内容当聊天、生成回复;`>1KB` 体积检查照样放行,无报错。**有隐私泄漏面**(把别的窗口内容发去模型)
- **✅ 解法**:新增 `_win_grab_pw`,用 `PrintWindow(hwnd, hdc, PW_RENDERFULLCONTENT=2)` 截窗口**自身像素**,
  `grab()` 优先走它、黑屏才回退 ImageGrab。无视遮挡、不抢前台(监控也安全)。
- **⚠️ 修正原结论**:本条原写「PrintWindow 对微信 Chromium 内核常黑屏」。**真机实测推翻**:`flag=0` 黑
  (均亮 6.2),但 **`flag=2`(PW_RENDERFULLCONTENT)不黑(均亮 46.8)**,微信被 IDE 完全压住时仍完整
  截到本体、title=「陈婕」。mac 那种「截被挡住的窗口」在 Windows 上对这个微信版本是可行的。
- **工作量**:medium(已完成)

### H2. 发布的 .exe 根本没打包 OCR → OCR 模式直接不能用
- **位置**:`.github/workflows/build.yml`(依赖只装了 pillow/pygetwindow,**没装 easyocr/torch**);`vision.py` `ocr_easyocr` 的 `import easyocr`;`run_ocr` 抛「没有可用的 OCR 后端」
- **触发**:**每一个发出去的 .exe**,只要默认 `read_mode: ocr`——100% 命中
- **影响**:OCR 永远跑不出文字 → 回退视觉模型 → 用户没装 Ollama/没填 key → 读取彻底失败,还显示一个误导的「回退」错误
- **缓解**:别发一个自己满足不了的模式。**Windows 版默认配 `read_mode: vlm`**(加个 `config.windows.yaml`,在 windows job 里 `--add-data` 成 `config.example.yaml`;mac 版保留 `ocr`,因为它有原生 vision 后端)。或真打包 easyocr+torch(~1GB+,不划算)。顺手把 `run_ocr` 的报错在 Windows 上写清楚(「easyocr 未打包,请改 read_mode: vlm」)
- **工作量**:medium

### H3. 找不到窗口就静默全屏截 → 把配置错误伪装成"识别差"
- **位置**:`vision.py` `_win_grab`(box 为空 → `ImageGrab.grab(all_screens=True)` 全屏)
- **触发**:标题没匹配上(版本/语言/多窗口),或**默认 `app_name` 为空**(首跑必中)
- **影响**:默默截**整个桌面**去 OCR,用户只觉得"好不准",根本不知道是窗口没配对 → 把易修的配置问题变成隐形问题、直接流失
- **缓解**:`_win_window_rect` 返回 None 时**别静默全屏**,改抛明确错误并列出当前可见窗口标题供自查:`未找到目标聊天窗口…当前可见标题: …`;全屏仅作显式 opt-in。还能顺带提示首跑去配 `app_name`
- **工作量**:small ✅(快赢)

### H4. `crop_left: 0.40` 是固定比例,但会话栏是固定像素宽
- **位置**:`config.yaml` `crop_left:0.40`、`vision.py` `_apply_crop`,以及 `assign_speaker` 里所有 `W` 相关阈值
- **触发**:任何窗口宽度和开发机不同——1366×768 笔记本 vs 4K、最大化 vs 小窗、用户拖宽/收起会话栏
- **影响**:宽窗口 40% 切进消息把左侧(对方)气泡截断 → 判错/丢失;窄窗口没切干净 → 会话列表被当成假消息读进去。两头都导致**判错"我/对方"+ 幻觉消息**喂给模型
- **缓解**:动态探测会话栏/消息区分界(扫列方差/背景色找那条竖分隔),按**绝对像素**裁;按窗口宽缓存一次。便宜版:把 `crop_left` 改成自动测一次的绝对像素(~300px)。再加个气泡 x0 是否"双峰"的合理性校验,塌成一侧就回退 VLM
- **工作量**:medium

### H5. 出回复前没有置信度门槛
- **位置**:`vision.py` `assign_speaker`(0.5 兜底 / 0.3 unknown 分支)、`_read_via_ocr`(映射 我/对方 时不看置信度)
- **触发**:布局含糊、头像检测失败回退到低置信几何判定时
- **影响**:把 0.3–0.5 的**猜测当作事实**喂给回复模型 → 自信地回错人/回错话,用户看不出来源已经错了
- **缓解**:把 OCR 置信度 + 发言人置信度透传到前端;中位置信度低时在返回里给「OCR 置信度低,发送前核对」警告,必要时降级/标注候选回复
- **工作量**:small ✅(快赢)

---

## 🟡 中危

| # | 风险 | 位置 | 触发 | 缓解 | 工作量 |
|---|---|---|---|---|---|
| M1 | 头像 chroma/texture 阈值((mx-mn)>22, std>18)是主题相关的,深色模式/纯色字母头像检测失败 | `detect_avatars` 531-540 | 深色主题、扁平头像、高缩放、截图压缩 | 阈值改自适应(相对背景基线);**关键**:`_content_mask` 无条件跑,图片/表情检测从"锚头像"改为"锚消息列视觉内容块",别因头像没检到就丢〔图片〕〔表情〕 | medium |
| M2 | 固定头像带 0.14W/0.86W、贴边阈值 0.30W 假设对称布局;群聊全在左侧 → 崩 | `detect_avatars`:541、`assign_speaker`:646 | 群聊、错裁后边距落进内容 | 群聊分支:左带有 2+ 头像块时按头像聚类绑每个发言人;带宽由实测头像列位置推导,别用硬编码常数 | large |
| M3 | 系统/时间戳判定(居中+窄)误判短的居中真消息 | `assign_speaker` 651-653 | 错裁后居中的短回复;真居中灰字 | 居中+窄不够,还要正向信号(灰色填充检测 或 撤回/拍了拍/日期 等模式匹配),否则落到低置信中心判定而非直接判 system | small |
| M4 | torch 撑大 exe + 冷启动慢;缺 VC++ 运行时 → DLL load failed | `build.yml` 一旦带 torch | 低端/老机器、缺 MSVC redist、慢盘 | Windows 版换 ONNX/RapidOCR 之类无 torch 的 OCR;或在干净 VM 验证 import torch、补 vcruntime DLL,并把缺运行时的错误显式报给用户 | large |
| M5 | `%LOCALAPPDATA%\DraftMate` 建目录/拷种子无错误处理 → 受限/满盘 profile 上 import 时崩 | `config.py` 41-51、80-90 | 重定向 AppData、严格 ACL、满盘(注:你 C: 本就常满) | `mkdir`+`_seed` 包 try/except;失败回退临时目录,UI 给一条可见的明确提示(不是 stdout——GUI exe 看不到) | small ✅ |
| M6 | 无依赖锁定 + 不带 anthropic → 云端回复路径默认也死 | `build.yml`:26 vs `config.yaml`:18 | 无 Ollama 又指望默认即用;未来 CI 拉到不兼容的 torch/numpy 组合 | 启动做一次 preflight:按 reply_model 校验 key/SDK/Ollama 可达、按 read_mode 校验 OCR 后端可导入;都不可用就弹一个合并的「完成配置」面板。固定依赖版本 | small ✅ |
| M7 | 窗口在另一个虚拟桌面/匹配后被最小化 → 截到壁纸 | `_win_window_rect`(只挡 x<=-30000) | 聊天在虚拟桌面 2、你在桌面 1 | `PrintWindow` 截窗口自身;或 `IVirtualDesktopManager::IsWindowOnCurrentVirtualDesktop` 不在当前桌面就拒绝并提示 | medium |
| M8 | RDP/远程桌面:会话断开/最小化时 ImageGrab 黑屏 | `_win_grab`,只有 <1024 体积检查 | 在 RDP 里跑 .exe | `GetSystemMetrics(SM_REMOTESESSION=0x1000)` 检测远程会话,黑屏/0 行时给「RDP 黑屏」专门提示(~3 行,无新依赖) | medium |
| M9 | OCR"识别成功但是乱码"无法和真文本区分,只处理了"0 字" | `process_image` 701-721;短文本守卫只挡 ≤1/≤4 字 | 花体字、表情包叠字、低对比主题的部分误读 | 把 per-message ocr_confidence 透传,算中位/低置信计数,低时在返回里警告并可降级候选 | small ✅ |

---

## 🟢 低危(长尾,但发布量大了会零星出现)

| # | 风险 | 缓解 | 工作量 |
|---|---|---|---|
| L1 | 头像高度带 0.03–0.16H、纹理块 H//160 假设固定像素尺度,4K/高 DPI 上漏检 | 改成锚定**已算出的文字行中位高**(`cluster_bubbles` 的 `mh`),头像带≈1.2–6×行高、块≈mh/4,自动随分辨率/DPI/字号自校准 | medium |
| L2 | easyocr 冷启动 + 弱 CPU 推理慢,阻塞读取(每次 2–10s) | ✅ 启动后台线程预热 Reader(`vision.warm_ocr()`,双重检查锁,主线程 0ms 返回);缩放部分作废(伤短文本)。并发守卫已有(monitorTick 的 readingNow) | medium ✅ |
| L3 | DPI 静默错位:`size>1024` 检查发现不了缩放/偏移的图 | 校验截到的 PNG 像素尺寸 vs 物理显示器分辨率;`SetProcessDpiAwareness` 的 `except: pass` 改成记一次告警 | small ✅ |
| L4 | 老 Windows(7/8.0)无 shcore → 只 system-DPI-aware | 记录达成的感知级别并在诊断里暴露;登录后改缩放/副屏不同 DPI 时提示移到主屏 | small ✅ |
| L5 | 副屏在左/上(负坐标)+ 混合 DPI → all_screens 截偏 | 首选 `SetProcessDpiAwarenessContext(-4)` Per-Monitor V2,保留现有为回退;截后断言 PNG 尺寸==(w,h) | medium |
| L6 | 首跑下 OCR 模型(~64MB 到 C:)无离线/代理/防火墙/AV/满盘处理 | Reader 用显式 `model_storage_directory` + `download_enabled`,离线/失败快速报「预置模型或改 vlm」;C: 满会 ENOSPC | medium |

---

## 发布前「必修」清单(小工、高价值,先做这些)

1. **H3** 找不到窗口别静默全屏 → 报清楚错(顺带提示配 `app_name`)— small
2. **H2** 修 CI:Windows 版默认 `read_mode: vlm`,别发开箱即坏的包 — medium
3. **H1(过渡版)** 截图前 `activate` + 校验前台,挡掉截错窗口 — small/medium
4. **H5 / M9** 加置信度门槛 + 低置信警告 — small
5. **M5** 数据目录建立加错误处理,别让首跑崩在看不见的地方 — small

## 战略取舍(决定走多远)

本地 OCR + 几何判定 = 一堆**对设备敏感的假设**,发布到杂设备必然不稳。三难:**便宜(本地OCR) / 跨设备稳(VLM) / 隐私(本地)**。

- **自己用**:OCR 已按你这台调好,继续用。
- **发布**:
  - **A. 默认 VLM**:Claude 读整图,布局/主题/分辨率/裁剪全自己消化,跳过上面整排 very_fragile。代价:每次读图付费 + 上传截图。**发布最省心。**
  - **B. 死磕 OCR 健壮性**:动态裁剪、自适应阈值、置信度门槛、PrintWindow…工作量大,且 PrintWindow 对微信(Chromium)常黑屏,治不彻底。
  - **C. 混合**:OCR 为主 + 低置信自动回退 VLM。体验最好、最费工。

无论哪条,**截图层(找窗口、遮挡、别静默全屏)+ CI 默认配置**都得先修——这部分和模式无关。
