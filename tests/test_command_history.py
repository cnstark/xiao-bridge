"""指令历史与执行结果测试；只加载目标函数，避免依赖运行时服务。"""

import ast
import json
import os
import pathlib
import re
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app" / "bridge.py"


def load_functions(names):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    functions = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
    namespace = {"json": json, "os": os, "re": re, "time": time, "log": Mock()}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"),
         namespace)
    return namespace


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.ns = load_functions({"load_history", "record_command", "handle_command",
                                  "cn_to_int", "ot_action", "ot_volume_delta"})
        self.ns.update({
            "DATA_DIR": self.temp_dir.name,
            "HISTORY_PATH": os.path.join(self.temp_dir.name, "command_history.json"),
            "HISTORY_LIMIT": 100,
            "COMMAND_HISTORY": [],
            "STATE": {"config": {"trigger": "影院"}, "last_command": ""},
            "mi_pause_speaker": AsyncMock(),
            "mi_tts": AsyncMock(),
            "ot_play_search": AsyncMock(return_value="正在播放 周杰伦 晴天"),
            "ot_put": AsyncMock(return_value=True),
        })

    async def test_records_match_miss_and_playback_result(self):
        self.assertFalse(await self.ns["handle_command"]("今天天气怎么样"))
        self.assertTrue(await self.ns["handle_command"]("影院播放晴天"))
        self.ns["ot_play_search"].return_value = "没找到 不存在的歌"
        self.assertTrue(await self.ns["handle_command"]("影院播放不存在的歌"))

        entries = self.ns["COMMAND_HISTORY"]
        self.assertEqual([entry["query"] for entry in entries],
                         ["影院播放不存在的歌", "影院播放晴天", "今天天气怎么样"])
        self.assertEqual([entry["action"] for entry in entries],
                         ["点歌", "点歌", "未命中"])
        self.assertEqual([entry["success"] for entry in entries], [False, True, None])
        self.assertEqual(entries[2]["result"], "-")
        self.assertEqual(self.ns["load_history"](), entries)
        self.ns["mi_pause_speaker"].assert_awaited()

    async def test_failed_player_action_is_recorded_as_failure(self):
        self.ns["ot_put"].return_value = False
        await self.ns["handle_command"]("影院暂停")
        entry = self.ns["COMMAND_HISTORY"][0]
        self.assertEqual((entry["action"], entry["result"], entry["success"]),
                         ("暂停", "暂停失败", False))

    async def test_history_keeps_latest_100_after_reload(self):
        for number in range(105):
            self.ns["record_command"](str(number), False, "未命中", "-", None)
        entries = self.ns["load_history"]()
        self.assertEqual(len(entries), 100)
        self.assertEqual(entries[0]["query"], "104")
        self.assertEqual(entries[-1]["query"], "5")
        self.assertTrue(all("time" in entry for entry in entries))


if __name__ == "__main__":
    unittest.main()
