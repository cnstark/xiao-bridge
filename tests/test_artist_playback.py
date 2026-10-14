"""按歌手随机播放的语音指令与队列测试。"""

import ast
import pathlib
import re
import types
import unittest
from unittest.mock import AsyncMock, Mock


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app" / "bridge.py"


def load_playback(tracks):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {"_norm", "_artist_names", "ot_play_search", "ot_play_artist",
             "ot_play_track_with_library"}
    functions = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
    namespace = {
        "re": re,
        "random": types.SimpleNamespace(shuffle=lambda items: items.reverse()),
        "log": Mock(),
        "STATE": {"config": {"output_id": "speaker"}},
        "ot_check": AsyncMock(return_value=True),
        "ot_library_tracks": AsyncMock(return_value=tracks),
        "ot_fuzzy_track": AsyncMock(return_value=None),
        "ot_get": AsyncMock(return_value={}),
        "ot_post": AsyncMock(return_value=True),
        "ot_select_output": AsyncMock(return_value=True),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SOURCE), "exec"),
         namespace)
    return namespace


class ArtistPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_phrasings_queue_only_requested_artist(self):
        tracks = [
            {"artist": "孙燕姿", "title": "晴天", "uri": "other"},
            {"artist": "周杰伦", "title": "晴天", "uri": "song1"},
            {"artist": "周杰伦", "title": "七里香", "uri": "song2"},
            {"artist": "周杰伦、费玉清", "title": "千里之外", "uri": "duet"},
            {"artist": "周杰伦", "title": "晴天", "uri": "song1"},
        ]
        for query in ("周杰伦的歌", "周杰伦的歌曲"):
            with self.subTest(query=query):
                ns = load_playback(tracks)
                reply = await ns["ot_play_search"](query)
                params = ns["ot_post"].await_args.kwargs["params"]
                self.assertEqual(params["uris"].split(","),
                                 ["duet", "song1", "song2"])
                self.assertEqual(params["clear"], "true")
                self.assertEqual(params["playback"], "start")
                self.assertEqual(params["playback_from_position"], 0)
                self.assertIn("共 3 首", reply)
                ns["ot_library_tracks"].assert_awaited_once()
                ns["ot_fuzzy_track"].assert_not_awaited()
                ns["ot_select_output"].assert_awaited_once()

    async def test_unknown_artist_does_not_fall_back_to_song_search(self):
        ns = load_playback([{"artist": "孙燕姿", "title": "晴天", "uri": "other"}])

        reply = await ns["ot_play_search"]("周杰伦的歌")

        self.assertIn("没找到歌手 周杰伦", reply)
        ns["ot_post"].assert_not_awaited()
        ns["ot_fuzzy_track"].assert_not_awaited()

    async def test_ordinary_song_request_keeps_existing_search_path(self):
        ns = load_playback([])

        await ns["ot_play_search"]("晴天")

        ns["ot_fuzzy_track"].assert_awaited_once_with("晴天")


if __name__ == "__main__":
    unittest.main()
