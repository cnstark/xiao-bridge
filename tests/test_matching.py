"""语音点歌匹配回归测试，无需连接 OwnTone。"""

import ast
from difflib import SequenceMatcher
import pathlib
import re
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app" / "bridge.py"


def similarity(a, b):
    return SequenceMatcher(None, a, b).ratio() * 100


def partial_similarity(a, b):
    if not a or not b:
        return 0
    shorter, longer = sorted((a, b), key=len)
    return max(similarity(shorter, longer[i:i + len(shorter)])
               for i in range(len(longer) - len(shorter) + 1))


def load_matcher(tracks):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"_norm", "_artist_names", "ot_fuzzy_track"}
    functions = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
    namespace = {
        "re": re,
        "log": Mock(),
        "ot_library_tracks": AsyncMock(return_value=tracks),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"),
         namespace)
    return namespace["ot_fuzzy_track"]


class MatchingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fake_rapidfuzz = types.SimpleNamespace(fuzz=types.SimpleNamespace(
            ratio=similarity, partial_ratio=partial_similarity))
        self.module_patch = patch.dict(sys.modules, {"rapidfuzz": fake_rapidfuzz})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)

    async def test_artist_before_or_after_title_selects_requested_singer(self):
        tracks = [
            {"artist": "孙燕姿", "title": "晴天", "uri": "wrong"},
            {"artist": "周杰伦", "title": "晴天", "uri": "right"},
        ]
        match = load_matcher(tracks)
        for query in ("周杰伦的晴天", "晴天周杰伦", "周杰伦唱的晴天"):
            with self.subTest(query=query):
                self.assertEqual((await match(query))["uri"], "right")

    async def test_exact_title_beats_longer_version_for_same_artist(self):
        match = load_matcher([
            {"artist": "周杰伦", "title": "晴天现场版", "uri": "live"},
            {"artist": "周杰伦", "title": "晴天", "uri": "studio"},
        ])
        self.assertEqual((await match("周杰伦晴天"))["uri"], "studio")

    async def test_title_only_keeps_existing_first_match_behavior(self):
        match = load_matcher([
            {"artist": "孙燕姿", "title": "晴天", "uri": "first"},
            {"artist": "周杰伦", "title": "晴天", "uri": "second"},
        ])
        self.assertEqual((await match("晴天"))["uri"], "first")

    async def test_unknown_artist_does_not_select_wrong_singer_by_title(self):
        match = load_matcher([
            {"artist": "孙燕姿", "title": "晴天", "uri": "wrong"},
        ])
        self.assertIsNone(await match("未知歌手晴天"))

    async def test_unknown_artist_does_not_match_even_with_long_title(self):
        match = load_matcher([
            {"artist": "孙燕姿", "title": "一首很长的歌曲名字", "uri": "wrong"},
        ])
        self.assertIsNone(await match("周杰伦一首很长的歌曲名字"))

    async def test_one_artist_in_collaboration_can_be_requested(self):
        match = load_matcher([
            {"artist": "孙燕姿", "title": "晴天", "uri": "wrong"},
            {"artist": "周杰伦、费玉清", "title": "晴天", "uri": "right"},
        ])
        self.assertEqual((await match("费玉清晴天"))["uri"], "right")


if __name__ == "__main__":
    unittest.main()
