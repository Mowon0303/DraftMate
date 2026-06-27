"""DraftMate 回归测试 —— 覆盖纯逻辑与可隔离的文件操作,不依赖 ollama/网络/截图/真机。

跑: .venv/bin/python -m unittest test_draftmate -v
"""
from __future__ import annotations

import datetime
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import agent
import config
import history
import llm
import memory_store
import skills


# ════════════════════ 历史拼接去重(history.stitch / _overlap_len)════════════════════
class TestStitch(unittest.TestCase):
    def _msgs(self, *texts):
        return [{"sender": "对方", "text": t} for t in texts]

    def test_overlap_dedup(self):
        # earlier(更早一屏)的尾部与 known 的头部重叠 → 只把更早的新消息 prepend
        known = self._msgs("c", "d", "e")
        earlier = self._msgs("a", "b", "c", "d")     # 尾 c,d 与 known 头 c,d 重叠
        out, added = history.stitch(known, earlier)
        self.assertEqual([m["text"] for m in out], ["a", "b", "c", "d", "e"])
        self.assertEqual(added, 2)                   # 只新增 a,b

    def test_no_known(self):
        earlier = self._msgs("a", "b")
        out, added = history.stitch([], earlier)
        self.assertEqual([m["text"] for m in out], ["a", "b"])
        self.assertEqual(added, 2)

    def test_empty_earlier(self):
        known = self._msgs("a")
        out, added = history.stitch(known, [])
        self.assertEqual(out, known)
        self.assertEqual(added, 0)

    def test_full_overlap_zero_added(self):
        # 到顶后再滚,earlier 完全被 known 覆盖 → 新增 0(触发到顶检测)
        known = self._msgs("a", "b", "c")
        out, added = history.stitch(known, self._msgs("a", "b", "c"))
        self.assertEqual(added, 0)
        self.assertEqual(len(out), 3)

    def test_sender_in_key(self):
        # 同文本不同发言人不算重叠
        known = [{"sender": "我", "text": "好"}]
        earlier = [{"sender": "对方", "text": "好"}]
        _, added = history.stitch(known, earlier)
        self.assertEqual(added, 1)


class TestMergeScreens(unittest.TestCase):
    """批量上传截图:顺序无关的自动排序拼接(history.merge_screens)。"""

    def _s(self, *texts):
        return [{"sender": "对方", "text": t} for t in texts]

    def _sys(self, t):
        return {"sender": "系统", "text": t}

    def test_reorders_by_overlap(self):
        # 三屏各与邻屏重叠 2 条;打乱输入顺序仍还原成连续历史
        A = self._s("a", "b", "c", "d", "e")
        B = self._s("d", "e", "f", "g", "h")
        C = self._s("g", "h", "i", "j", "k")
        out = history.merge_screens([C, A, B])              # 乱序输入
        self.assertEqual([m["text"] for m in out],
                         ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"])

    def test_disconnected_ordered_by_timestamp(self):
        # 无重叠两屏:按系统时间戳(英文日期)排序,与输入顺序无关
        today = datetime.date(2025, 6, 20)
        S1 = [self._sys("Jun 8, 2025 20:30"), *self._s("x1", "x2")]
        S2 = [self._sys("Jun 14, 2025 19:40"), *self._s("y1", "y2")]
        out = history.merge_screens([S2, S1], today=today)  # 新的在前,仍把旧的排前
        texts = [m["text"] for m in out]
        self.assertLess(texts.index("x1"), texts.index("y1"))

    def test_single_and_empty(self):
        self.assertEqual(history.merge_screens([]), [])
        one = self._s("a", "b")
        self.assertEqual(history.merge_screens([one]), one)

    def test_english_date_parsed(self):
        today = datetime.date(2025, 6, 20)
        self.assertEqual(history.parse_wechat_date("Jun 14, 2025 19:45", today),
                         datetime.date(2025, 6, 14))
        self.assertEqual(history.parse_wechat_date("June 8, 2025", today),
                         datetime.date(2025, 6, 8))


# ════════════════════ 微信时间戳解析(history.parse_wechat_date)════════════════════
class TestParseDate(unittest.TestCase):
    T = datetime.date(2026, 6, 12)   # 周五

    def test_pure_time_is_today(self):
        self.assertEqual(history.parse_wechat_date("07:03", self.T), self.T)
        self.assertEqual(history.parse_wechat_date("0:26", self.T), self.T)

    def test_yesterday(self):
        y = self.T - datetime.timedelta(days=1)
        self.assertEqual(history.parse_wechat_date("昨天 16:11", self.T), y)
        self.assertEqual(history.parse_wechat_date("昨大 21:23", self.T), y)   # OCR 容错

    def test_weekday(self):
        self.assertEqual(history.parse_wechat_date("星期三 11:48", self.T), datetime.date(2026, 6, 10))
        self.assertEqual(history.parse_wechat_date("星期二 19:22", self.T), datetime.date(2026, 6, 9))

    def test_ymd(self):
        self.assertEqual(history.parse_wechat_date("2025年12月25日", self.T), datetime.date(2025, 12, 25))

    def test_md_current_year(self):
        self.assertEqual(history.parse_wechat_date("10.7", self.T), datetime.date(2026, 10, 7))

    def test_garbage_returns_none(self):
        self.assertIsNone(history.parse_wechat_date("乱码xyz", self.T))
        self.assertIsNone(history.parse_wechat_date("", self.T))
        self.assertIsNone(history.parse_wechat_date("13.99", self.T))   # 非法月日

    def test_earliest_in_screen_monotonic(self):
        # 一屏多个戳取最早(吸收 OCR 把二/三读混的抖动)
        scr = [{"sender": "系统", "text": "星期三 11:48"},
               {"sender": "系统", "text": "星期二 12:46"},
               {"sender": "对方", "text": "正文不算"}]
        self.assertEqual(history._earliest_in_screen(scr, self.T), datetime.date(2026, 6, 9))

    def test_earliest_no_system(self):
        self.assertIsNone(history._earliest_in_screen([{"sender": "对方", "text": "hi"}], self.T))


# ════════════════════ agent 辅助(render / 温度 / 手动上下文)════════════════════
class TestAgentHelpers(unittest.TestCase):
    def test_render_truncates_to_last_n(self):
        msgs = [{"sender": "对方", "text": str(i)} for i in range(10)]
        out = agent.render(msgs, 3)
        self.assertEqual(out, "对方: 7\n对方: 8\n对方: 9")

    def test_temperature_per_persona(self):
        self.assertLess(agent.temperature_for("serious"), agent.temperature_for("flirty"))
        self.assertGreater(agent.temperature_for("flirty", regen=True), agent.temperature_for("flirty"))
        self.assertLessEqual(agent.temperature_for("flirty", regen=True), 1.0)   # 不超 1.0

    def test_render_manual_context(self):
        self.assertEqual(agent._render_manual_context(None), "(暂无)")
        self.assertEqual(agent._render_manual_context({}), "(暂无)")
        out = agent._render_manual_context({"goal": "推进到暧昧", "person_info": ""})
        self.assertIn("推进到暧昧", out)
        self.assertNotIn("对方信息", out)   # 空字段不出现

    def test_split_bubbles(self):
        # 已分行:去编号/项目符号/整行引号/空行,最多 3 条
        self.assertEqual(agent.split_bubbles("1. 你好\n2. 在吗"), "你好\n在吗")
        self.assertEqual(agent.split_bubbles("- 嗯\n\n- 行"), "嗯\n行")
        self.assertEqual(agent.split_bubbles('"哈哈"\n好'), "哈哈\n好")
        self.assertEqual(agent.split_bubbles("a\nb\nc\nd"), "a\nb\nc")
        # 未分行的长句:按句末标点拆成真人连发的短句(7B 兜底)
        self.assertEqual(
            agent.split_bubbles("听起来好辛苦呢。要不先休息一下。需要我帮忙吗？").count("\n"), 2)
        # 短句不拆
        self.assertEqual(agent.split_bubbles("就一句短的"), "就一句短的")


# ════════════════════ 人设加载(skills.load_persona,.md 优先 / .local.md 回退)════════════════════
class TestPersona(unittest.TestCase):
    def test_public_persona(self):
        self.assertIn("深情流", skills.load_persona("shenqing"))

    def test_local_fallback(self):
        # 真名版以 .local.md 存在(不入库),应能回退加载
        local = skills.PERSONA_DIR / "tongjincheng.local.md"
        if local.exists():
            self.assertTrue(skills.load_persona("tongjincheng"))
        else:
            self.skipTest("无 tongjincheng.local.md")

    def test_missing_persona(self):
        self.assertEqual(skills.load_persona("不存在的人设xyz"), "")

    def test_lovehelper_context(self):
        with tempfile.TemporaryDirectory() as d:
            adapter = Path(d) / "draftmate-adapter.md"
            adapter.write_text("LoveHelper adapter marker", encoding="utf-8")
            with mock.patch.object(skills, "LOVEHELPER_ADAPTER", adapter):
                self.assertIn("adapter marker", skills.lovehelper_context())

    def test_lovehelper_playbook(self):
        with tempfile.TemporaryDirectory() as d:
            playbook = Path(d) / "human-progression-playbook.md"
            playbook.write_text("playbook marker", encoding="utf-8")
            with mock.patch.object(skills, "LOVEHELPER_PLAYBOOK", playbook):
                self.assertIn("playbook marker", skills.lovehelper_playbook())

    def test_manual_context_roundtrip(self):
        # save → load 往返;隔离到临时目录,不碰真实记忆
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(skills, "MEM_DIR", Path(d)):
                skills.save_manual_context("测试人", {"goal": "约出来", "person_info": "USC同学"})
                got = skills.manual_context("测试人")
                self.assertEqual(got["goal"], "约出来")
                self.assertEqual(got["person_info"], "USC同学")

    def test_save_summary(self):
        with tempfile.TemporaryDirectory() as d:
            mem = Path(d) / "memory"
            db = Path(d) / "memory.sqlite3"
            with mock.patch.object(skills, "MEM_DIR", mem), \
                 mock.patch.object(skills, "MEMORY_DB", db):
                p = skills.save_summary("张三", "## 对方画像\n- 爱猫")
                self.assertTrue(p.exists())
                self.assertIn("爱猫", skills.load_memory("张三"))
                self.assertIn("结构化记忆(SQLite facts)", skills.load_memory("张三"))

                skills.save_compacts("张三", '[{"kind":"profile","content":"喜欢猫","evidence_count":2}]')
                loaded = skills.load_memory("张三")
                self.assertIn("压缩记忆(SQLite compact)", loaded)
                self.assertIn("喜欢猫", loaded)


# ════════════════════ SQLite 结构化记忆(memory_store)════════════════════
class TestMemoryStore(unittest.TestCase):
    def test_parse_summary_sections(self):
        summary = (
            "## 对方画像\n"
            "- 爱猫 [据:\"我家猫又拆家了\"]\n"
            "## 承诺与待办\n"
            "- 周末一起吃饭 [据:\"周末吃饭\"]\n"
            "## 最近氛围\n"
            "一句话: 轻松但还没落地\n"
        )
        facts = memory_store.parse_summary(summary)
        self.assertEqual([f["kind"] for f in facts], ["profile", "commitment", "mood"])
        self.assertEqual(facts[0]["source_ref"], "我家猫又拆家了")

    def test_replace_summary_facts_and_render(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "memory.sqlite3"
            n = memory_store.replace_summary_facts(
                "张三",
                "## 对方画像\n- 爱猫\n## 雷区/边界\n- 不喜欢被催",
                safe_name="张三",
                db_path=db,
            )
            self.assertEqual(n, 2)
            rendered = memory_store.render_facts("张三", db_path=db)
            self.assertIn("结构化记忆(SQLite facts)", rendered)
            self.assertIn("不喜欢被催", rendered)
            self.assertIn("爱猫", rendered)

    def test_render_facts_topic_selection(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "memory.sqlite3"
            memory_store.replace_summary_facts(
                "张三",
                "## 对方画像\n"
                "- 爱猫\n"
                "- 喜欢篮球\n"
                "## 一起经历/聊过的大事\n"
                "- 最近在准备数学考试\n",
                db_path=db,
            )
            rendered = memory_store.render_facts("张三", db_path=db, query_text="考试复习", limit=1)
            self.assertIn("数学考试", rendered)
            self.assertNotIn("爱猫", rendered)

    def test_boundary_stays_high_priority(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "memory.sqlite3"
            memory_store.replace_summary_facts(
                "张三",
                "## 雷区/边界\n"
                "- 不喜欢被催\n"
                "## 对方画像\n"
                "- 喜欢篮球\n",
                db_path=db,
            )
            rendered = memory_store.render_facts("张三", db_path=db, query_text="篮球", limit=1)
            self.assertIn("不喜欢被催", rendered)

    def test_legacy_statuses_migrate_to_active(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "memory.sqlite3"
            memory_store.replace_summary_facts("张三", "## 对方画像\n- 爱猫", db_path=db)
            # closing() 确保连接真正关闭(sqlite3 的 `with ... as con` 只提交事务、不关连接);
            # 否则 Windows 上 TemporaryDirectory 清理会因文件句柄未释放而 WinError。
            with closing(sqlite3.connect(db)) as con:
                con.execute("UPDATE memory_facts SET status = 'candidate'")
                con.commit()
            memory_store.init_db(db)
            with closing(sqlite3.connect(db)) as con:
                status = con.execute("SELECT status FROM memory_facts").fetchone()[0]
            self.assertEqual(status, "active")

    def test_compact_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "memory.sqlite3"
            n = memory_store.replace_compacts_from_json(
                "张三",
                """```json
                [{"kind":"boundary","content":"不喜欢被催","confidence":0.9,"evidence_count":2}]
                ```""",
                db_path=db,
            )
            self.assertEqual(n, 1)
            rendered = memory_store.render_compacts("张三", db_path=db)
            self.assertIn("压缩记忆(SQLite compact)", rendered)
            self.assertIn("不喜欢被催", rendered)

    def test_compact_priority_over_topic_when_boundary(self):
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "memory.sqlite3"
            memory_store.replace_compacts_from_json(
                "张三",
                "["
                "{\"kind\":\"boundary\",\"content\":\"不喜欢被催\",\"evidence_count\":1},"
                "{\"kind\":\"profile\",\"content\":\"喜欢篮球\",\"evidence_count\":5}"
                "]",
                db_path=db,
            )
            rendered = memory_store.render_compacts("张三", db_path=db, query_text="篮球", limit=1)
            self.assertIn("不喜欢被催", rendered)


# ════════════════════ 用量计数(copilot,手动/自动分计)════════════════════
class TestUsage(unittest.TestCase):
    def setUp(self):
        import copilot
        self.copilot = copilot
        _fd, _p = tempfile.mkstemp(suffix=".json")
        os.close(_fd)                 # 关掉句柄,否则 Windows 上 unlink 会 WinError 32
        self._tmp = Path(_p)
        self._tmp.unlink()
        self._orig = copilot.USAGE_PATH
        copilot.USAGE_PATH = self._tmp
        _fd2, _p2 = tempfile.mkstemp(suffix=".log")
        os.close(_fd2)
        self._tmplog = Path(_p2)
        self._origlog = copilot.TOKENLOG_PATH
        copilot.TOKENLOG_PATH = self._tmplog

    def tearDown(self):
        self.copilot.USAGE_PATH = self._orig
        self.copilot.TOKENLOG_PATH = self._origlog
        if self._tmp.exists():
            self._tmp.unlink()
        if self._tmplog.exists():
            self._tmplog.unlink()

    def test_split_manual_auto(self):
        u0 = self.copilot._usage()
        self.assertEqual((u0["reads"], u0["auto_reads"], u0["last_used"]), (0, 0, ""))
        self.assertEqual((u0["in_tokens"], u0["out_tokens"],
                          u0["cache_hit_tokens"], u0["cache_miss_tokens"]), (0, 0, 0, 0))
        self.copilot._bump_usage()
        self.copilot._bump_usage(auto=True)
        self.copilot._bump_usage(auto=True)
        u = self.copilot._usage()
        self.assertEqual(u["reads"], 1)          # 手动只算 1(周留存指标)
        self.assertEqual(u["auto_reads"], 2)     # 监控触发单独记

    def test_token_usage_accumulates(self):
        import llm
        llm.drain_calls()                        # 清掉别的测试可能留下的记录
        llm._record_usage("deepseek", "deepseek-chat", 1000, 50, 900, 100)
        llm._record_usage("deepseek", "deepseek-chat", 800, 40, 750, 50)
        self.copilot._log_tokens("read")         # drain → 落盘 usage.json + 明细
        self.assertEqual(llm.drain_calls(), [])  # 已被 drain 干净
        u = self.copilot._usage()
        self.assertEqual(u["in_tokens"], 1800)
        self.assertEqual(u["out_tokens"], 90)
        self.assertEqual(u["cache_hit_tokens"], 1650)
        self.assertEqual(u["cache_miss_tokens"], 150)
        self.copilot._bump_usage()               # token 累计与 reads 计数互不覆盖
        self.assertEqual(self.copilot._usage()["in_tokens"], 1800)


# ════════════════════ 截图左边界自动检测(vision._detect_left_boundary)════════════════════
class TestCropBoundary(unittest.TestCase):
    def setUp(self):
        import vision
        import numpy as np
        self.vision = vision
        self.np = np
        vision._MODE["crop_auto"] = True

    def _img(self, w, h, split, left_v, right_v):
        from PIL import Image
        arr = self.np.full((h, w), right_v, dtype="uint8")
        arr[:, :split] = left_v
        return Image.fromarray(arr, mode="L")

    def test_detects_list_pane_divider(self):
        # 左亮(会话列表)右暗(聊天区),分界在 x=140 → 应检测到分界附近,而非固定比例 0.40→160
        im = self._img(400, 300, 140, 180, 40)
        x0 = self.vision._detect_left_boundary(im, 400, 300, 0.40)
        self.assertTrue(130 <= x0 <= 150, f"detected {x0}, 期望≈140")

    def test_falls_back_when_no_contrast(self):
        # 整张同色(浅色模式/无列表)→ 对比太弱,回退固定比例
        im = self._img(400, 300, 140, 100, 100)
        self.assertEqual(self.vision._detect_left_boundary(im, 400, 300, 0.40), 160)

    def test_disabled_uses_fraction(self):
        self.vision._MODE["crop_auto"] = False
        im = self._img(400, 300, 140, 180, 40)
        self.assertEqual(self.vision._detect_left_boundary(im, 400, 300, 0.40), 160)
        self.vision._MODE["crop_auto"] = True


# ════════════════════ OCR 后台预热(vision.warm_ocr）════════════════════
class TestWarmOcr(unittest.TestCase):
    def setUp(self):
        import vision
        self.vision = vision
        # 隔离全局状态,跑完恢复(替身代替真 easyocr，绝不加载真模型)
        self._save = (vision._EASY, vision._WARMING, vision._get_easy_reader, dict(vision._MODE))
        vision._EASY = None
        vision._WARMING = False

    def tearDown(self):
        easy, warming, getter, mode = self._save
        self.vision._EASY = easy
        self.vision._WARMING = warming
        self.vision._get_easy_reader = getter
        self.vision._MODE.clear(); self.vision._MODE.update(mode)

    def _spy(self):
        import threading
        ev = threading.Event()
        def fake():
            ev.set()
            return "reader"
        self.vision._get_easy_reader = fake
        return ev

    def test_skips_when_not_ocr_mode(self):
        import time
        self.vision._MODE["read_mode"] = "vlm"
        ev = self._spy()
        self.vision.warm_ocr()
        self.assertFalse(ev.wait(0.3), "vlm 模式不该预热 OCR")

    def test_skips_when_backend_not_easy(self):
        self.vision._MODE.update(read_mode="ocr", ocr_backend="tesseract")
        ev = self._spy()
        self.vision.warm_ocr()
        self.assertFalse(ev.wait(0.3), "非 easyocr 后端不该预热 easyocr")

    def test_skips_when_already_loaded(self):
        self.vision._MODE.update(read_mode="ocr", ocr_backend="auto")
        self.vision._EASY = object()        # 已加载
        ev = self._spy()
        self.vision.warm_ocr()
        self.assertFalse(ev.wait(0.3), "已加载就别再预热")

    def test_warms_in_background_and_clears_flag(self):
        import time
        self.vision._MODE.update(read_mode="ocr", ocr_backend="auto")
        ev = self._spy()
        self.vision.warm_ocr()
        self.assertTrue(ev.wait(2.0), "ocr 模式应后台预热 easyocr")
        # 预热结束后 _WARMING 应复位
        for _ in range(100):
            if not self.vision._WARMING:
                break
            time.sleep(0.02)
        self.assertFalse(self.vision._WARMING)


# ════════════════════ 军师阶段判定缓存/门控(copilot._staged_analysis)════════════════════
class TestStageGate(unittest.TestCase):
    def setUp(self):
        import copilot
        import agent
        self.copilot = copilot
        self.agent = agent
        _fd, _p = tempfile.mkstemp(suffix=".json")
        os.close(_fd)
        self._tmp = Path(_p)
        self._tmp.unlink()
        self._orig_stage = copilot.STAGE_PATH
        copilot.STAGE_PATH = self._tmp
        _fd2, _p2 = tempfile.mkstemp(suffix=".json")    # 沙盒 token 计量,别污染真实数据目录
        os.close(_fd2)
        self._u = Path(_p2)
        self._orig_u = copilot.USAGE_PATH
        copilot.USAGE_PATH = self._u
        self.calls = []
        self._orig_assess = agent.assess_stage

        def fake(messages, mem, model, last_n=8, manual=None):
            self.calls.append(len(messages))             # 记每次「真的打了模型」
            return "阶段: 20分"
        agent.assess_stage = fake

    def tearDown(self):
        self.copilot.STAGE_PATH = self._orig_stage
        self.copilot.USAGE_PATH = self._orig_u
        self.agent.assess_stage = self._orig_assess
        for p in (self._tmp, self._u):
            if p.exists():
                p.unlink()

    def _msgs(self, *texts):
        return [{"sender": "对方", "text": t} for t in texts]

    def test_reuse_and_triggers(self):
        c = self.copilot
        m1 = self._msgs("在吗", "吃饭了没")
        a, recomputed = c._staged_analysis("小A", m1, "", {})
        self.assertEqual((a, recomputed), ("阶段: 20分", True))
        self.assertEqual(len(self.calls), 1)             # 首次无缓存 → 算
        _, recomputed = c._staged_analysis("小A", m1, "", {})
        self.assertFalse(recomputed)                     # 同消息 → 复用
        self.assertEqual(len(self.calls), 1)
        _, recomputed = c._staged_analysis("小A", m1 + self._msgs("哈哈"), "", {})
        self.assertFalse(recomputed)                     # +1 条普通(未达阈值、无关键词)→ 复用
        self.assertEqual(len(self.calls), 1)
        _, recomputed = c._staged_analysis("小A", m1 + self._msgs("周末出来约个饭?"), "", {})
        self.assertTrue(recomputed)                      # 含「约」关键词 → 重算
        self.assertEqual(len(self.calls), 2)
        _, recomputed = c._staged_analysis("小A", m1, "", {}, force=True)
        self.assertTrue(recomputed)                      # 手动强刷 → 重算
        self.assertEqual(len(self.calls), 3)

    def test_threshold_new_messages(self):
        c = self.copilot
        base = self._msgs("a")
        c._staged_analysis("小B", base, "", {})
        self.assertEqual(len(self.calls), 1)
        more = base + self._msgs("b", "c", "d", "e")      # 对方新增 4 条 → 达阈值重算
        _, recomputed = c._staged_analysis("小B", more, "", {})
        self.assertTrue(recomputed)
        self.assertEqual(len(self.calls), 2)

    def test_unknown_title_not_cached(self):
        c = self.copilot
        m = self._msgs("hi")
        c._staged_analysis("unknown", m, "", {})
        c._staged_analysis("unknown", m, "", {})
        self.assertEqual(len(self.calls), 2)             # 标题没读准 → 不缓存,每次都算
        self.assertFalse(c.STAGE_PATH.exists())          # 不写缓存文件(防串到别的联系人)


# ════════════════════ 追加建议(agent.draft_followup,我方结尾时该不该追)════════════════════
class TestFollowup(unittest.TestCase):
    def setUp(self):
        import agent
        import llm
        self.agent = agent
        self.llm = llm
        self._orig = llm.call_text

    def tearDown(self):
        self.llm.call_text = self._orig

    def _run(self, ret):
        self.llm.call_text = lambda *a, **k: ret
        return self.agent.draft_followup([{"sender": "我", "text": "在吗"}], "人设", "", "deepseek-chat")

    def test_hold_with_reason(self):
        r = self._run("不追加 | 发完先等对方回")
        self.assertTrue(r["hold"])
        self.assertEqual(r["text"], "")
        self.assertIn("等对方", r["reason"])

    def test_hold_without_pipe(self):
        r = self._run("先别追了,显得需要")
        self.assertTrue(r["hold"])
        self.assertEqual(r["text"], "")

    def test_draft_returned(self):
        r = self._run("刚那句说急了\n其实我想说周末有空一起吃饭")
        self.assertFalse(r["hold"])
        self.assertEqual(r["reason"], "")
        self.assertIn("周末", r["text"])
        self.assertIn("\n", r["text"])           # 多行气泡保留


# ════════════════════ 云端检测(copilot._cloud_available,无 key 必本地)════════════════════
class TestCloudGate(unittest.TestCase):
    def test_no_key_is_local(self):
        import copilot
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(copilot._cloud_available())

    def test_deepseek_key_enables_cloud(self):
        import copilot
        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test"}, clear=True):
            self.assertTrue(copilot._deepseek_available())
            self.assertTrue(copilot._cloud_available())


class TestEnvFile(unittest.TestCase):
    def test_load_env_file_without_overriding_shell(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text(
                "DEEPSEEK_API_KEY=from_file\n"
                "ANTHROPIC_API_KEY='quoted_value'\n",
                encoding="utf-8",
            )
            with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "from_shell"}, clear=True):
                loaded = config.load_env_file(env)
                self.assertEqual(loaded, 1)
                self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "from_shell")
                self.assertEqual(os.environ["ANTHROPIC_API_KEY"], "quoted_value")

    def test_load_env_file_skips_empty_values(self):
        with tempfile.TemporaryDirectory() as d:
            env = Path(d) / ".env"
            env.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(config.load_env_file(env), 0)
                self.assertNotIn("DEEPSEEK_API_KEY", os.environ)


# ════════════════════ DeepSeek 路由与输出清理(llm)════════════════════
class TestDeepSeekBackend(unittest.TestCase):
    def test_deepseek_model_aliases(self):
        self.assertEqual(llm._deepseek_model("dsv4"), "deepseek-v4-flash")
        self.assertEqual(llm._deepseek_model("dsv4-pro"), "deepseek-v4-pro")
        self.assertEqual(llm._deepseek_model("deepseek-v4-flash"), "deepseek-v4-flash")

    def test_backend_routes_deepseek_models(self):
        self.assertEqual(llm._backend("deepseek-v4-flash"), "deepseek")
        self.assertEqual(llm._backend("dsv4"), "deepseek")

    def test_strip_thinking_tags(self):
        self.assertEqual(llm._strip_thinking("<think>hidden</think>可以发"), "可以发")

    def test_redact_deepseek_key_suffix(self):
        msg = "Authentication Fails, Your api key: ****abcd is invalid"
        self.assertEqual(
            llm._redact_key_suffix(msg),
            "Authentication Fails, Your api key: [redacted] is invalid",
        )


# ════════════════════ 冷启动开场白(空聊天 + 已有对方动态记忆)════════════════════
class TestColdStartOpener(unittest.TestCase):
    def setUp(self):
        import copilot
        self.copilot = copilot

    def test_has_distilled_memory(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(skills, "MEM_DIR", Path(d) / "mem"), \
                 mock.patch.object(skills, "MEMORY_DB", Path(d) / "memory.sqlite3"):
                self.assertFalse(skills.has_distilled_memory("没档案的人"))   # summary 文件都没有
                skills.save_summary("空档", "## 对方画像\n- (无)\n## 雷区/边界\n- (无)")
                self.assertFalse(skills.has_distilled_memory("空档"))          # 全 (无) 占位 → 不算
                skills.save_summary("有料", "## 对方画像\n- 爱爬山,常拍风景")
                self.assertTrue(skills.has_distilled_memory("有料"))           # 有实质内容
        self.assertFalse(skills.has_distilled_memory(""))                     # 空标题

    def test_opener_fires_on_empty_chat_with_memory(self):
        with mock.patch.object(self.copilot.skills, "has_manual_context", return_value=False), \
             mock.patch.object(self.copilot.skills, "has_distilled_memory", return_value=True), \
             mock.patch.object(self.copilot.agent, "draft_opener",
                               return_value="最近爬山了吗\n哪条线路最值") as m:
            sugg, fu = self.copilot._one_suggestion("目标", [], "记忆文本", {}, "", "pursue")
            self.assertTrue(m.called)
            self.assertEqual(len(sugg), 1)
            self.assertTrue(sugg[0].get("opener"))
            self.assertIsNone(fu)
            self.assertIn("开场白", self.copilot._note_for(sugg, fu))

    def test_opener_fires_on_system_only_with_memory(self):
        sys_only = [{"sender": "系统", "text": "你已添加对方为好友"}]
        with mock.patch.object(self.copilot.skills, "has_manual_context", return_value=False), \
             mock.patch.object(self.copilot.skills, "has_distilled_memory", return_value=True), \
             mock.patch.object(self.copilot.agent, "draft_opener", return_value="嗨") as m:
            sugg, fu = self.copilot._one_suggestion("目标", sys_only, "记忆", {}, "", "pursue")
            self.assertTrue(m.called)
            self.assertEqual(len(sugg), 1)
            self.assertTrue(sugg[0].get("opener"))

    def test_opener_fires_on_manual_context_only(self):
        with mock.patch.object(self.copilot.skills, "has_manual_context", return_value=True), \
             mock.patch.object(self.copilot.skills, "has_distilled_memory", return_value=False), \
             mock.patch.object(self.copilot.agent, "draft_opener", return_value="你好呀") as m:
            sugg, fu = self.copilot._one_suggestion("目标", [], "", {"person_info": "同学"}, "", "pursue")
            self.assertTrue(m.called)
            self.assertEqual(len(sugg), 1)
            self.assertTrue(sugg[0].get("opener"))

    def test_no_opener_when_no_info(self):
        # 对这个人一无所知(无导入记忆、无手填上下文)→ 保持沉默,不硬挤开场白
        with mock.patch.object(self.copilot.skills, "has_manual_context", return_value=False), \
             mock.patch.object(self.copilot.skills, "has_distilled_memory", return_value=False), \
             mock.patch.object(self.copilot.agent, "draft_opener") as m:
            sugg, fu = self.copilot._one_suggestion("陌生人", [], "", {}, "", "pursue")
            self.assertEqual((sugg, fu), ([], None))
            self.assertFalse(m.called)


if __name__ == "__main__":
    unittest.main()
