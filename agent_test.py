"""本机可跑的编排器逻辑测试（无需服务器 / DeepSeek key / open_clip）。

运行: python agent_test.py
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_pipeline import DeepSeekAgent, Deliverer, QualityGate, run_tasks


class FakeClient:
    """伪造 openai client，返回预设的 message。"""

    def __init__(self, message):
        self.message = message
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **kw: SimpleNamespace(
                    choices=[SimpleNamespace(message=self.message)])))


def make_message(tool_args):
    return SimpleNamespace(
        tool_calls=[SimpleNamespace(
            function=SimpleNamespace(name="generate_assets", arguments=json.dumps(tool_args)))],
        content=None,
    )


class TestParseRequirement(unittest.TestCase):
    def _agent(self, msg):
        return DeepSeekAgent(cfg={}, api_key="test", client=FakeClient(msg))

    def test_valid_single_task(self):
        msg = make_message({"requests": [
            {"prompt": "a knight in mtg card style", "count": 3, "mode": "txt2img"}]})
        tasks = self._agent(msg).parse_requirement("做 3 张精灵卡牌")
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual(t["count"], 3)
        self.assertEqual(t["mode"], "txt2img")
        self.assertNotEqual(t["prompt"].strip(), "")
        self.assertIn("knight", t["prompt"])

    def test_multiple_tasks_with_ref(self):
        msg = make_message({"requests": [
            {"prompt": "hero portrait", "count": 2, "mode": "txt2img"},
            {"prompt": "redraw this hero", "count": 1, "mode": "img2img",
             "ref_image": "hero.png"}]})
        tasks = self._agent(msg).parse_requirement("做英雄立绘，再翻新一张")
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[1]["mode"], "img2img")
        self.assertEqual(tasks[1]["ref_image"], "hero.png")

    def test_retry_on_bad_json_then_success(self):
        calls = {"n": 0}

        def create(req=None, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return make_message({"requests": "not-an-array"})
            return make_message({"requests": [{"prompt": "ok", "count": 1, "mode": "txt2img"}]})

        agent = DeepSeekAgent(cfg={"agent": {"max_retries": 3}}, api_key="test", client=FakeClient(None))
        agent._call_llm = create
        tasks = agent.parse_requirement("需求")
        self.assertEqual(len(tasks), 1)
        self.assertGreater(calls["n"], 1)  # 第一次非法 JSON，确实发生了一次失败重试

    def test_plain_content_json_without_tool_call(self):
        msg = SimpleNamespace(tool_calls=None,
                              content='```json\n{"requests":[{"prompt":"a dragon","count":2,"mode":"txt2img"}]}\n```')
        tasks = self._agent(msg).parse_requirement("做 2 张龙")
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["count"], 2)

    def test_exhausts_retries_then_raises(self):
        agent = DeepSeekAgent(cfg={"agent": {"max_retries": 2}}, api_key="test", client=FakeClient(None))
        agent._call_llm = lambda req: make_message({"requests": []})
        with self.assertRaises(ValueError):
            agent.parse_requirement("需求")

    def test_rejects_invalid_task_fields(self):
        agent = self._agent(make_message({"requests": []}))
        invalid = [
            {"requests": [{"prompt": "x", "count": 0, "mode": "txt2img"}]},
            {"requests": [{"prompt": "x", "count": 1, "mode": "unknown"}]},
            {"requests": [{"prompt": "x", "count": 1, "mode": "img2img"}]},
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertIsNone(agent._extract_tasks(make_message(payload)))


class TestToolSchema(unittest.TestCase):
    def test_schema_well_formed(self):
        tool = DeepSeekAgent.TOOLS[0]
        self.assertEqual(tool["type"], "function")
        fn = tool["function"]
        self.assertEqual(fn["name"], "generate_assets")
        props = fn["parameters"]["properties"]
        self.assertIn("requests", props)
        reqs = props["requests"]["items"]["properties"]
        for field in ("prompt", "count", "mode"):
            self.assertIn(field, reqs)
        self.assertEqual(reqs["mode"]["enum"], ["txt2img", "img2img", "ipadapter"])
        self.assertIn("requests", fn["parameters"]["required"])


class TestQualityGate(unittest.TestCase):
    def setUp(self):
        # 只测 retry 判定逻辑，不触发 CLIP 加载
        self.gate = QualityGate(threshold=0.30)

    def test_below_threshold_retries(self):
        self.assertTrue(self.gate.retry(0.25))

    def test_above_threshold_passes(self):
        self.assertFalse(self.gate.retry(0.35))

    def test_exact_threshold_passes(self):
        self.assertFalse(self.gate.retry(0.30))


class TestDeliverer(unittest.TestCase):
    def test_publish_asset_copies_to_batch_root(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            src_dir = root / "_post"
            src_dir.mkdir()
            src = src_dir / "generated.png"
            src.write_bytes(b"image-bytes")

            name = Deliverer.publish_asset(root, src, "asset_0")

            self.assertEqual(name, "asset_0.png")
            self.assertEqual((root / name).read_bytes(), b"image-bytes")


class TestWorkerPool(unittest.TestCase):
    @staticmethod
    def _cfg(out_root, workers):
        return {
            "comfyui": {
                "base_url": workers[0], "workers": workers, "workflows_dir": ".",
                "max_poll_sec": 1, "poll_interval": 0.01, "input_dir": None,
            },
            "quality": {"threshold": 0.3, "max_retries": 0},
            "generation": {}, "postprocess": {},
            "deliver": {"out_root": str(out_root)},
        }

    def test_two_workers_are_used(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self._cfg(Path(root), ["http://worker-0", "http://worker-1"])

            def fake_gen(scheduler, gate, post, batch_dir, mode, prompt, ref_name, config, prefix):
                time.sleep(0.02)
                return {"status": "ok", "image": f"{prefix}.png", "attempts": []}

            with patch("agent_pipeline._gen_one", side_effect=fake_gen):
                _, records = run_tasks(
                    [{"prompt": "p", "count": 4, "mode": "txt2img"}], cfg, "pool_test"
                )
            self.assertEqual(len(records), 4)
            self.assertEqual({r["worker_url"] for r in records}, set(cfg["comfyui"]["workers"]))

    def test_failed_item_is_retried_on_resume(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = self._cfg(Path(root), ["http://worker-0"])
            task = [{"prompt": "p", "count": 1, "mode": "txt2img"}]
            with patch("agent_pipeline._gen_one", return_value={"status": "failed", "image": None, "attempts": []}):
                run_tasks(task, cfg, "resume_test")
            with patch("agent_pipeline._gen_one", return_value={"status": "ok", "image": "ok.png", "attempts": []}):
                _, records = run_tasks(task, cfg, "resume_test")
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
