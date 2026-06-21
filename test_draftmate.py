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


if __name__ == "__main__":
    unittest.main()
