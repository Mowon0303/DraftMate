"""根据上下文 + 人设 + 记忆生成回复草稿。"""
from __future__ import annotations

import re

import llm
import skills

_ROLE = (
    "你在替用户本人在聊天软件里回消息。下面的「人设」就是你的说话方式,"
    "人设里的示例是语感参照——模仿那个味道,但禁止照抄原句、禁止套用到不贴合的语境。"
)

_RULES = (
    "- 你在替「我」说话:别把对方的处境、情绪、待办安到自己头上。\n"
    "- 像真人发微信:拆成 1~3 条**短消息**,每条单独占一行(我会一条一条发出去)。别把一句话硬拆成两行,也别为凑数注水——一句话能说清就只发一条。\n"
    "- 每条都短、口语;不加引号、不解释、不署名、不加编号。\n"
    "- 对方最后一句若是提问、二选一或求确认,先正面回应它。\n"
    "- 不反问对方已问过的问题,不复述对方刚说的话,不编造没发生的事。\n"
    "- 禁止客服腔和正确废话:「多喝热水」「注意休息哦」「加油哦」「辛苦啦」「没关系的呢」这类一律不出现。\n"
    "- 一条回复最多一个问题,不查户口。\n"
    "- 如果设了「目标(阶段性)」,朝它轻推一步就够,别用力过猛;没设目标就正常接话。\n"
    "- 信息不足时给出自己的倾向或自然地问清,不要瞎编。"
)

# 各人设的采样温度:撩/走心需要变化和灵气,正式场合要稳。
_TEMPS = {"serious": 0.5, "casual": 0.75, "flirty": 0.85, "shenqing": 0.8}


def temperature_for(persona_name: str, regen: bool = False) -> float:
    """人设 → 采样温度;「换个说法」再加一点随机,避免重生成出同一句。"""
    t = _TEMPS.get(persona_name, 0.7)
    return min(1.0, t + 0.15) if regen else t


def _lovehelper_block(include_playbook: bool = False) -> str:
    ctx = skills.lovehelper_context().strip()
    playbook = skills.lovehelper_playbook().strip() if include_playbook else ""
    if not ctx and not playbook:
        return ""
    parts = []
    if ctx:
        parts.append(f"## LoveHelper 副驾规则(内部使用,最终输出仍遵守本次调用格式)\n{ctx}")
    if playbook:
        parts.append(
            "## LoveHelper human-progression-playbook(回复生成时必须参与决策层和口吻层)\n"
            f"{playbook}"
        )
    return "\n\n".join(parts) + "\n\n"


# 军师层:关系阶段判定规则。前台使用 LoveHelper 的 0/10/20/30/40/50 口径,
# 旧 L0-L5 只作为兼容映射;原则是证据不足就降级,只引用真实对话。
_STAGE_RUBRIC = (
    "你是恋爱/暧昧聊天的军师,从对话证据估计当前关系阶段,再给下一步方向。前台阶段用这些标签:\n"
    "- 0分 陌生:只有问候/事务,无跟进、无记忆\n"
    "- 10分 认识:背景问答(学校/工作/兴趣),双向但浅\n"
    "- 20分 有好感:有时主动、记得旧细节、会接话/追问、有轻松互动或软计划\n"
    "- 30分 吸引阶段:对方会接你的节奏,有一点暧昧/张力/约会暗示,但还没稳定暧昧\n"
    "- 40分 暧昧:明显双向暧昧,有调情、情绪拉扯、具体一对一计划或关系试探\n"
    "- 50分 步入恋爱:明确身份/排他承诺,或稳定约会、维护、修复、社交融入等伴侣化行为\n"
    "- friend zone/朋友位:聊得多但缺少浪漫框架、性张力、主动好奇或互惠投入\n"
    "- 减分信号/降温:回复质量下降、回避见面、只索取陪聊/帮忙、用户过度解释/追问\n"
    "- 下头/风险:明确反感、持续躲避、施压/控制/查岗/不接受拒绝/低姿态乞求后回避\n"
    "判定规则:\n"
    "- 20分以上至少需要两类独立证据;拿不准就取更低一级并说还缺什么\n"
    "- 秒回和表情是弱证据;暖心支持不等于浪漫;聊得多但没有张力可能只是朋友位\n"
    "- 只引用对话里真实出现的内容,绝不脑补画面外的事\n"
    "- 对话明显不是恋爱/暧昧语境(同事/事务/家人)时,阶段行写「不适用(非恋爱语境)」,策略行照常给一句普通沟通建议\n"
    "输出格式(恰好三行,每行一句,不要标题不要多余文字):\n"
    "阶段: <0/10/20/30/40/50分+中文名,可附朋友位/降温/风险旗标>(置信度 低/中/高)\n"
    "依据: <引用 1-2 个对话片段说明为什么>\n"
    "策略: <降压/轻推/调侃/抽离/约见/澄清 中选一个主策略,留有拒绝空间;只描述方向,禁止写示例句、禁止引号台词>"
)


def assess_stage(messages, memory_text: str, model: str, last_n: int = 8,
                 manual_context: dict | None = None) -> str:
    """军师判定:估计关系阶段 + 下一步方向。低温短输出,供 UI 展示并喂给草稿生成校准火候。"""
    convo = render(messages, last_n)
    system = (
        f"{_lovehelper_block()}{_STAGE_RUBRIC}\n\n## 关于对方的已知信息\n"
        f"{_render_manual_context(manual_context)}\n{(memory_text or '').strip()}"
    )
    user = f"## 当前对话(最后一条是对方刚发的)\n{convo}\n\n请按三行格式输出判定:"
    return llm.call_text(model, system, user, max_tokens=220, temperature=0.3)


# 历史导入:把长聊天蒸馏成记忆档案。
# 真实数据实测:7B 输入越长越容易①把口头梗当实体/以偏概全 ②归类错位(把球赛闲聊塞进'承诺待办')。
# 对策 = map-reduce:分块摘录(每块短→归类准)→ 合并归类去重;全程强制原文引用 [据:"原话"] 抑制编造。
_MAP_PROMPT = (
    "下面是一段聊天记录的**一个片段**(OCR,有噪声乱码,看不懂的行跳过)。\n"
    "逐条摘录**以后聊天用得上**的事实,每条一行,格式严格为:  [类型] 内容 [据:\"原话\"]\n"
    "类型只能用这五种:\n"
    "  画像 = 对方身份/处境/性格/喜好    雷区 = TA 明说过的不喜欢/敏感点\n"
    "  事件 = 聊过的话题或发生的事        承诺 = **明确约好将来要做的事**(如'下周一起吃饭')\n"
    "  氛围 = 这段聊天的语气\n"
    "铁律:只摘真实出现的,引用不出原话就不写;乱码不猜;不联想常识(别因聊到某游戏就补没出现的游戏名)。\n"
    "**特别注意**:球赛/游戏的评论、感叹、玩笑**都不是承诺**;别人聊的话题不等于 TA 的喜好。\n"
    "本段没有的类型就不写。只输出要点行,不要小标题、不要总结。"
)

# 单块/reduce 共用的结构化输出模板:只整理不推断。
_STRUCT_PROMPT = (
    "把下面的信息**整理**成一份联系人记忆档案。只做归类、去重、整理,"
    "**不新增推断、不联想常识**;矛盾时取有原文引用支撑的那条。\n"
    "按结构输出,某类没内容写「(无)」:\n"
    "## 关系背景\n一句话:怎么认识/现在算什么关系(没线索写'(无足够线索)')\n"
    "## 对方画像\n- 身份/处境/性格/喜好,每条带 [据:\"原话\"]\n"
    "## 雷区/边界\n- TA 明说过的不喜欢/敏感点 [据:\"原话\"]\n"
    "## 一起经历/聊过的大事\n- 具体话题或事件 [据:\"原话\"]\n"
    "## 承诺与待办\n- **只保留明确约好将来要做的事**;球赛/游戏评论、感叹、玩笑不算 [据:\"原话\"]\n"
    "## 最近氛围\n一句话"
)

_COMPACT_PROMPT = (
    "你在维护一个聊天副驾的联系人长期记忆库。输入是一组已抽取 facts,请压缩成长期可用的 compact memory。\n"
    "规则:\n"
    "- 只基于输入 facts,不新增事实、不猜测。\n"
    "- 合并重复和近义事实,每条 content 不超过 30 个中文字符。\n"
    "- 边界/雷区保守保留;未完成约定写成 open_loop;互动习惯写 rhythm。\n"
    "- 不要保留一次性闲聊,不要保留没有用的赞美。\n"
    "- kind 只能是: profile, boundary, relationship, preference, rhythm, open_loop。\n"
    "- 输出纯 JSON 数组,不要 Markdown,不要解释。\n"
    "格式:\n"
    "[{\"kind\":\"profile\",\"content\":\"喜欢猫,常聊猫日常\",\"confidence\":0.8,\"evidence_count\":2}]"
)


def distill_memory(messages, model: str, manual_context: dict | None = None,
                   chunk_size: int = 35, on_progress=None) -> str:
    """长聊天蒸馏成记忆档案。短历史(≤chunk_size)单次蒸馏;长历史 map-reduce
    (分块摘录→合并归类),避免 7B 在长输入上归类错位/泛化。on_progress({phase,i,n}) 可选进度回调。"""
    msgs = list(messages or [])
    ctx = _render_manual_context(manual_context)
    if len(msgs) <= chunk_size:
        convo = render(msgs, len(msgs))
        user = (f"## 已知信息(辅助,别和聊天矛盾)\n{ctx}\n\n"
                f"## 聊天记录(OCR,有噪声)\n{convo}\n\n整理成档案:")
        return llm.call_text(model, _STRUCT_PROMPT, user, max_tokens=900, temperature=0.2)
    # map:分块摘录带类型标签的要点(每块输入短,归类准)
    chunks = [msgs[i:i + chunk_size] for i in range(0, len(msgs), chunk_size)]
    points = []
    for idx, c in enumerate(chunks):
        if on_progress:
            on_progress({"phase": "map", "i": idx + 1, "n": len(chunks)})
        convo = render(c, len(c))
        out = llm.call_text(model, _MAP_PROMPT,
                            f"## 片段({idx + 1}/{len(chunks)})\n{convo}\n\n摘录要点:",
                            max_tokens=600, temperature=0.2)
        if out.strip():
            points.append(out.strip())
    # reduce:把各块要点(已干净)归类去重成档案
    if on_progress:
        on_progress({"phase": "reduce", "i": len(chunks), "n": len(chunks)})
    allpoints = "\n".join(points)
    user = (f"## 已知信息(辅助,别和聊天矛盾)\n{ctx}\n\n"
            f"## 从长聊天分段摘录的要点(已带[类型]标签和原文引用)\n{allpoints}\n\n整理成档案:")
    return llm.call_text(model, _STRUCT_PROMPT, user, max_tokens=900, temperature=0.2)


def compact_memory(facts_text: str, model: str) -> str:
    """把 SQLite facts 渲染文本压缩成长期 compact memory JSON。"""
    if not (facts_text or "").strip():
        return "[]"
    user = f"## 已抽取 facts\n{facts_text}\n\n请输出 compact memory JSON:"
    return llm.call_text(model, _COMPACT_PROMPT, user, max_tokens=700, temperature=0.2)


def render(messages, last_n: int) -> str:
    """把消息列表渲染成 '发送者: 内容' 的多行文本。"""
    rows = []
    for m in messages[-last_n:]:
        rows.append(f"{m.get('sender', '?')}: {m.get('text', '')}")
    return "\n".join(rows)


def _render_manual_context(manual_context: dict | None) -> str:
    if not manual_context:
        return "(暂无)"
    labels = {
        "person_info": "对方信息",
        "goal": "目标(阶段性)",
        "avoid": "不要提/边界",
        "notes": "备注",
    }
    rows = []
    for key, label in labels.items():
        value = (manual_context.get(key) or "").strip()
        if value:
            rows.append(f"- {label}: {value}")
    return "\n".join(rows) if rows else "(暂无)"


def split_bubbles(text: str, max_bubbles: int = 3) -> str:
    """把模型输出整理成「每行一个气泡」的多行串(真人连发的节奏):
    去空行、去行首项目符号/编号、去整行包裹的引号;最多 max_bubbles 条。前端按 \\n 切分渲染。
    兜底:7B 常不分行、把多句塞一行——若只剩一条且偏长,按句末标点拆成真人连发的短句。"""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        line = re.sub(r"^[-*•·]\s*", "", line)        # 项目符号
        line = re.sub(r"^\d+[.、)]\s*", "", line)       # 编号
        if len(line) >= 2 and line[0] in "\"“'" and line[-1] in "\"”'":
            line = line[1:-1].strip()                  # 整行被引号包裹
        if line:
            out.append(line)
    if len(out) == 1 and len(out[0]) > 12:            # 模型没分行 → 按句末标点拆
        parts = [p.strip() for p in re.split(r"(?<=[。！？!?；])\s*", out[0]) if p.strip()]
        if len(parts) > 1:
            out = parts
    return "\n".join(out[:max_bubbles]) if out else (text or "").strip()


def draft_reply(
    messages,
    persona_text: str,
    memory_text: str,
    model: str,
    last_n: int = 8,
    manual_context: dict | None = None,
    temperature: float = 0.7,
    stage_hint: str = "",
) -> str:
    convo = render(messages, last_n)
    stage_block = (
        f"## 军师判定(按它的阶段校准火候,别越级推进;它给的是方向不是台词,禁止照抄它的措辞)\n{stage_hint}\n\n"
        if stage_hint else ""
    )
    # 人设放最前定调,硬规则压轴 —— 小模型对开头和结尾的指令最敏感
    system = (
        f"{_ROLE}\n\n{_lovehelper_block(include_playbook=True)}## 人设(你的说话方式)\n{persona_text}\n\n"
        f"{stage_block}"
        f"## 手动上下文(优先级最高)\n{_render_manual_context(manual_context)}\n\n"
        f"## 关于该联系人的记忆\n{memory_text or '(暂无)'}\n\n"
        f"## 输出硬规则\n{_RULES}"
    )
    user = f"## 当前对话(最后一条是对方刚发的)\n{convo}\n\n请直接给出你要发送的回复(像真人微信,1~3 条短消息,每条一行):"
    return split_bubbles(llm.call_text(model, system, user, max_tokens=300, temperature=temperature))
