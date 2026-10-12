"""播放队列行为测试；只加载目标函数，避免依赖运行时服务。"""

import ast
import pathlib
import types
import unittest
from unittest.mock import AsyncMock, Mock


SOURCE = pathlib.Path(__file__).resolve().parents[1] / "app" / "bridge.py"


def load_queue_function():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == "ot_play_track_with_library")
    namespace = {
        "random": types.SimpleNamespace(shuffle=lambda items: items.reverse()),
        "log": Mock(),
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"),
         namespace)
    return namespace


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_song_first_then_every_other_song_once(self):
        ns = load_queue_function()
        tracks = [{"uri": f"library:track:{i}"} for i in range(121)]
        tracks.append({"uri": "library:track:4"})
        ns["ot_library_tracks"] = AsyncMock(return_value=tracks)
        ns["ot_post"] = AsyncMock(return_value=True)
        ns["ot_select_output"] = AsyncMock(return_value=True)

        reply = await ns["ot_play_track_with_library"](
            {"uri": "library:track:4", "artist": "歌手", "title": "歌曲"})

        calls = ns["ot_post"].await_args_list
        self.assertEqual(len(calls), 3)
        uris = [uri for call in calls for uri in call.kwargs["params"]["uris"].split(",")]
        self.assertEqual(uris, ["library:track:4"] + [
            f"library:track:{i}" for i in reversed(range(121)) if i != 4])
        self.assertEqual(len(uris), len(set(uris)))
        self.assertEqual(calls[0].kwargs["params"]["clear"], "true")
        self.assertEqual(calls[0].kwargs["params"]["playback"], "start")
        self.assertNotIn("clear", calls[1].kwargs["params"])
        self.assertNotIn("playback", calls[1].kwargs["params"])
        ns["ot_select_output"].assert_awaited_once()
        self.assertEqual(reply, "正在播放 歌手 歌曲")

    async def test_later_batch_failure_reports_incomplete_queue(self):
        ns = load_queue_function()
        ns["ot_library_tracks"] = AsyncMock(return_value=[
            {"uri": f"library:track:{i}"} for i in range(51)])
        ns["ot_post"] = AsyncMock(side_effect=[True, False])
        ns["ot_select_output"] = AsyncMock(return_value=True)

        reply = await ns["ot_play_track_with_library"](
            {"uri": "library:track:0", "title": "歌曲"})

        self.assertIn("播放列表未全部加入", reply)
        ns["ot_select_output"].assert_awaited_once()

    async def test_first_batch_failure_does_not_select_output(self):
        ns = load_queue_function()
        ns["ot_library_tracks"] = AsyncMock(return_value=[])
        ns["ot_post"] = AsyncMock(return_value=False)
        ns["ot_select_output"] = AsyncMock()

        reply = await ns["ot_play_track_with_library"](
            {"uri": "library:track:0", "title": "歌曲"})

        self.assertEqual(reply, "播放失败")
        ns["ot_select_output"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
