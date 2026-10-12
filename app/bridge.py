#!/usr/bin/env python3
"""xiaoair: 小爱音箱(语音入口) -> OwnTone -> AirPlay 音箱

原理:
1. 通过 miservice 登录小米账号, 轮询小爱音箱的对话记录
2. 命中以触发词(默认"影院")开头的指令时, 转发给 OwnTone
3. OwnTone 把音乐通过 AirPlay 推到指定音箱
4. 同时暂停小爱自身的播放, 并让小爱 TTS 播报确认

小米登录支持新设备风控(短信验证): 全流程在设置页内完成, 无需复制任何链接。
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from urllib import parse as urlparse

from aiohttp import BasicAuth, ClientSession, ClientTimeout
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from miservice import MiAccount, MiNAService

DATA_DIR = os.environ.get("DATA_DIR", "/data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
TOKEN_PATH = os.path.join(DATA_DIR, "mi_token.json")
LATEST_ASK_API = (
    "https://userprofile.mina.mi.com/device_profile/v2/conversation"
    "?source=dialogu&hardware={hardware}&timestamp={ts}&limit=2"
)
POLL_INTERVAL = 1.0
UA = "MiHome/6.0.103 (com.xiaomi.mihome; build:6.0.103.1; iOS 14.4.0) Alamofire/6.0.103"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("xiaoair")

DEFAULT_CONFIG = {
    "mi_user": "",
    "mi_pass": "",
    "mi_device_id": "",
    "speaker_device_id": "",   # 监听的小爱音箱 deviceID
    "speaker_name": "",
    "speaker_hardware": "",
    "speaker_did": "",
    "ot_url": os.environ.get("OT_URL", "http://192.168.1.2:3689"),
    "ot_user": os.environ.get("OT_USER", "admin"),
    "ot_pass": os.environ.get("OT_PASS", "changeme"),
    "output_id": "",           # OwnTone 输出设备 id
    "output_name": "",
    "trigger": os.environ.get("TRIGGER", "影院"),  # 触发词, 如 "影院播放晴天"
    "tts_reply": True,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log.warning("读取配置失败: %s", e)
    return cfg


def save_config(cfg):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


STATE = {
    "config": load_config(),
    "mi_logged_in": False,
    "mi_error": "",
    "mi_need_2fa": False,     # 等待短信验证码
    "mi_2fa_hint": "",
    "devices": [],
    "tfa": None,              # {"session": ClientSession, "context": str, "flag": int}
    "ot_ok": False,
    "ot_outputs": [],
    "last_command": "",
    "last_seen_query": "",
}


def ensure_device_id():
    cfg = STATE["config"]
    if not cfg.get("mi_device_id"):
        import secrets
        cfg["mi_device_id"] = secrets.token_hex(8).upper()
        save_config(cfg)
    return cfg["mi_device_id"]


# ---------------- 小米登录(含风控短信验证) ----------------

async def mi_init_from_token():
    """已有 token 文件时直接初始化, 成功返回 True"""
    if not os.path.isfile(TOKEN_PATH):
        return False
    try:
        session = ClientSession()
        account = MiAccount(session, STATE["config"]["mi_user"] or "0",
                            STATE["config"]["mi_pass"] or "x", TOKEN_PATH)
        if not account.token or "micoapi" not in account.token:
            return False
        # 对话记录接口需要 userId/serviceToken cookie, 从 token 注入会话
        service_token = account.token["micoapi"][1]
        user_id = str(account.token.get("userId", ""))
        if service_token and user_id:
            from yarl import URL
            session.cookie_jar.update_cookies(
                {"userId": user_id, "serviceToken": service_token},
                URL("https://userprofile.mina.mi.com"),
            )
        STATE["mi_session"] = session
        STATE["mi_mina"] = MiNAService(account)
        STATE["mi_logged_in"] = True
        STATE["mi_error"] = ""
        log.info("使用已保存的小米登录态")
        return True
    except Exception as e:
        log.warning("token 初始化失败: %s", e)
        return False


async def mi_password_login():
    """密码登录; 返回 'ok' / 'need_2fa' / 'fail:原因'"""
    cfg = STATE["config"]
    device_id = ensure_device_id()
    session = ClientSession()
    cookies = {"sdkVersion": "3.9", "deviceId": device_id, "passToken": ""}
    headers = {"User-Agent": UA}
    try:
        async with session.get(
            "https://account.xiaomi.com/pass/serviceLogin?sid=micoapi&_json=true",
            cookies=cookies, headers=headers, ssl=False,
        ) as r:
            raw = await r.read()
            resp = json.loads(raw.decode()[11:])
        data = {
            "_json": "true", "qs": resp.get("qs"), "sid": resp.get("sid"),
            "_sign": resp.get("_sign"), "callback": resp.get("callback"),
            "user": cfg["mi_user"],
            "hash": hashlib.md5(cfg["mi_pass"].encode()).hexdigest().upper(),
        }
        async with session.post(
            "https://account.xiaomi.com/pass/serviceLoginAuth2",
            data=data, cookies=cookies, headers=headers, ssl=False,
        ) as r:
            raw = await r.read()
            resp2 = json.loads(raw.decode()[11:])

        if resp2.get("ssecurity"):
            # 直接登录成功
            token = {
                "deviceId": device_id,
                "userId": str(resp2.get("userId", "")),
                "passToken": resp2.get("passToken", ""),
                "micoapi": [resp2["ssecurity"], ""],
            }
            # 取 serviceToken
            nsec = "nonce=" + str(resp2.get("nonce")) + "&" + resp2["ssecurity"]
            sign = base64.b64encode(hashlib.sha1(nsec.encode()).digest()).decode()
            async with session.get(
                resp2["location"] + "&clientSign=" + urlparse.quote(sign),
                headers=headers, ssl=False,
            ) as r:
                st = r.cookies.get("serviceToken")
                token["micoapi"][1] = st.value if st else ""
            with open(TOKEN_PATH, "w", encoding="utf-8") as f:
                json.dump(token, f)
            await session.close()
            return "ok"

        notify = resp2.get("notificationUrl")
        if notify:
            context = urlparse.parse_qs(urlparse.urlparse(notify).query).get("context", [""])[0]
            STATE["tfa"] = {"session": session, "context": context, "flag": 4,
                            "cookies": cookies, "notify": notify}
            return "need_2fa"

        return "fail:" + resp2.get("description", str(resp2.get("code", "未知")))
    except Exception as e:
        await session.close()
        log.error("密码登录异常: %s", e)
        return "fail:" + str(e)


async def tfa_send_code():
    """走 identity/list + sendMsgTicket, 给用户手机发验证码"""
    tfa = STATE.get("tfa")
    if not tfa:
        return "没有待验证的登录会话, 请先提交账号密码"
    session = tfa["session"]
    context = tfa["context"]
    headers = {"User-Agent": UA}
    try:
        # GET authStart, 获取 ick 等 cookie
        async with session.get(tfa["notify"], headers=headers, ssl=False) as r:
            await r.read()
        # GET identity/list, 拿 identity_session 和验证方式 flag
        async with session.get(
            "https://account.xiaomi.com/identity/list",
            params={"sid": "micoapi", "context": context, "_locale": "zh_CN"},
            headers=headers, ssl=False,
        ) as r:
            raw = await r.read()
            info = json.loads(raw.decode()[11:]) if raw[:11] == b"&&&START&&&" else await r.json(content_type=None)
        log.info("identity/list 响应: %s", json.dumps(info, ensure_ascii=False)[:400])
        flag = info.get("flag", 4)
        options = info.get("options", [flag])
        tfa["flag"] = 4 if 4 in options else (8 if 8 in options else flag)
        identity_session = None
        for c in session.cookie_jar:
            if c.key == "identity_session":
                identity_session = c.value
        if not identity_session:
            return "获取验证会话失败(identity_session 缺失)"
        tfa["identity_session"] = identity_session
        ick = ""
        for c in session.cookie_jar:
            if c.key == "ick":
                ick = c.value
        api = "/identity/auth/sendPhoneTicket" if tfa["flag"] == 4 else "/identity/auth/sendEmailTicket"
        async with session.post(
            "https://account.xiaomi.com" + api,
            params={"_dc": str(int(time.time() * 1000)), "sid": "micoapi",
                    "context": context, "mask": "0", "_locale": "zh_CN"},
            data={"retry": "0", "icode": "", "_json": "true", "ick": ick},
            headers=headers, ssl=False,
        ) as r:
            body = await r.read()
            log.info("sendTicket 响应 status=%s body=%s", r.status, body[:400])
        log.info("验证方式 flag=%s, 验证码已请求发送", tfa["flag"])
        return "ok"
    except Exception as e:
        log.error("发送验证码失败: %s", e)
        return "发送失败: " + str(e)


async def tfa_verify_code(code):
    """提交验证码, 完成登录并写 token"""
    tfa = STATE.get("tfa")
    if not tfa or "identity_session" not in tfa:
        return "会话已过期, 请重新发送验证码"
    session = tfa["session"]
    flag = tfa.get("flag", 4)
    api = "/identity/auth/verifyPhone" if flag == 4 else "/identity/auth/verifyEmail"
    headers = {"User-Agent": UA}
    try:
        async with session.post(
            "https://account.xiaomi.com" + api,
            params={"_dc": str(int(time.time() * 1000))},
            data={"_flag": str(flag), "ticket": code.strip(),
                  "trust": "true", "_json": "true"},
            cookies={"identity_session": tfa["identity_session"]},
            headers=headers, ssl=False,
        ) as r:
            raw = await r.read()
            text = raw.decode()
            data = json.loads(text[11:]) if text.startswith("&&&START&&&") else json.loads(text)
        if data.get("code") != 0:
            return "验证失败: " + data.get("description", str(data))
        location = data.get("location")
        if not location:
            return "验证成功但未返回登录地址"
        # 完成登录链路, 收集 cookie
        async with session.get(location, headers=headers, ssl=False) as r:
            await r.read()
        service_token = ""
        user_id = ""
        for c in session.cookie_jar:
            if c.key == "serviceToken" and not service_token:
                service_token = c.value
            if c.key == "userId" and not user_id:
                user_id = c.value
        if not service_token:
            return "未获取到 serviceToken"
        token = {
            "deviceId": ensure_device_id(),
            "userId": user_id or STATE["config"]["mi_user"],
            "passToken": "",
            "micoapi": ["", service_token],
        }
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            json.dump(token, f)
        await session.close()
        STATE["tfa"] = None
        log.info("短信验证完成, 登录态已保存 userId=%s", token["userId"])
        return "ok"
    except Exception as e:
        log.error("验证码校验异常: %s", e)
        return "验证异常: " + str(e)


async def mi_login():
    """统一登录入口: 优先已存 token, 其次密码(可能触发 need_2fa)"""
    cfg = STATE["config"]
    if not cfg["mi_user"] or not cfg["mi_pass"]:
        STATE["mi_error"] = "未配置小米账号"
        return False
    if await mi_init_from_token():
        return True
    result = await mi_password_login()
    if result == "ok":
        return await mi_init_from_token()
    if result == "need_2fa":
        STATE["mi_need_2fa"] = True
        STATE["mi_error"] = "需要短信验证(新设备风控)"
        return False
    STATE["mi_error"] = result[5:]
    STATE["mi_need_2fa"] = False
    return False


async def mi_fetch_devices():
    try:
        devices = await STATE["mi_mina"].device_list()
        STATE["devices"] = [
            {
                "deviceID": d.get("deviceID", ""),
                "name": d.get("name", ""),
                "hardware": d.get("hardware", ""),
                "did": d.get("miotDID", "") or d.get("did", ""),
            }
            for d in (devices or [])
        ]
        return STATE["devices"]
    except Exception as e:
        log.error("获取设备列表失败: %s", e)
        STATE["mi_error"] = str(e)
        return []


async def mi_pause_speaker():
    cfg = STATE["config"]
    try:
        await STATE["mi_mina"].player_pause(cfg["speaker_device_id"])
    except Exception as e:
        log.warning("暂停小爱失败: %s", e)


async def mi_tts(text):
    cfg = STATE["config"]
    if not cfg["tts_reply"] or not text:
        return
    try:
        await STATE["mi_mina"].text_to_speech(cfg["speaker_device_id"], text)
    except Exception as e:
        log.warning("TTS 失败: %s", e)


async def fetch_latest_query():
    cfg = STATE["config"]
    session = STATE["mi_session"]
    url = LATEST_ASK_API.format(
        hardware=cfg["speaker_hardware"], ts=int(time.time() * 1000)
    )
    cookies = {"deviceId": cfg["speaker_device_id"]}
    try:
        async with session.get(url, timeout=ClientTimeout(total=10), cookies=cookies) as r:
            if r.status != 200:
                log.warning("对话记录请求失败 status=%s", r.status)
                return None
            data = await r.json()
    except Exception as e:
        log.warning("对话记录请求异常: %s", e)
        return None
    d = data.get("data")
    if not d:
        return None
    try:
        records = json.loads(d).get("records")
    except Exception:
        return None
    if not records:
        return None
    rec = records[0]
    return {"query": rec.get("query", "").strip(), "time": rec.get("time", 0)}


# ---------------- OwnTone 侧 ----------------

def ot_auth():
    cfg = STATE["config"]
    return BasicAuth(cfg["ot_user"], cfg["ot_pass"])


async def ot_get(path, params=None):
    cfg = STATE["config"]
    try:
        async with ClientSession() as s:
            async with s.get(cfg["ot_url"] + path, params=params, auth=ot_auth(),
                             timeout=ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
                log.warning("OwnTone GET %s -> %s", path, r.status)
                return None
    except Exception as e:
        log.warning("OwnTone GET %s 异常: %s", path, e)
        STATE["ot_ok"] = False
        return None


async def ot_put(path, params=None, payload=None):
    cfg = STATE["config"]
    try:
        async with ClientSession() as s:
            async with s.put(cfg["ot_url"] + path, params=params, json=payload,
                             auth=ot_auth(), timeout=ClientTimeout(total=10)) as r:
                if r.status not in (200, 204):
                    log.warning("OwnTone PUT %s -> %s", path, r.status)
                return r.status in (200, 204)
    except Exception as e:
        log.warning("OwnTone PUT %s 异常: %s", path, e)
        return False


async def ot_post(path, params=None):
    cfg = STATE["config"]
    try:
        async with ClientSession() as s:
            async with s.post(cfg["ot_url"] + path, params=params, auth=ot_auth(),
                              timeout=ClientTimeout(total=15)) as r:
                if r.status not in (200, 204):
                    log.warning("OwnTone POST %s -> %s", path, r.status)
                return r.status in (200, 204)
    except Exception as e:
        log.warning("OwnTone POST %s 异常: %s", path, e)
        return False


async def ot_check():
    data = await ot_get("/api/config")
    STATE["ot_ok"] = data is not None
    return STATE["ot_ok"]


async def ot_fetch_outputs():
    data = await ot_get("/api/outputs")
    if not data:
        return []
    STATE["ot_outputs"] = [
        {"id": o["id"], "name": o["name"], "type": o.get("type", ""),
         "selected": o.get("selected", False)}
        for o in data.get("outputs", [])
    ]
    return STATE["ot_outputs"]


async def ot_select_output():
    cfg = STATE["config"]
    if not cfg["output_id"]:
        return False
    await ot_fetch_outputs()
    for o in STATE["ot_outputs"]:
        if o["id"] == cfg["output_id"] and o["selected"]:
            return True
    return await ot_put("/api/outputs/set", payload={"outputs": [cfg["output_id"]]})


async def ot_play_search(query):
    cfg = STATE["config"]
    if not cfg["output_id"]:
        return "还没选择输出音箱"
    if not await ot_check():
        return "音乐服务未连接"
    # 首选: 曲库模糊匹配(容错语音识别误差)
    track = await ot_fuzzy_track(query)
    if track:
        ok = await ot_post("/api/queue/items/add", params={
            "uris": track["uri"], "clear": "true", "playback": "start",
            "playback_from_position": 0,
        })
        if ok:
            await ot_select_output()
            return f"正在播放 {track.get('artist', '')} {track.get('title', '')}".strip()
        return "播放失败"
    # 单曲没匹配到, 试试歌单(OwnTone 原生搜索)
    data = await ot_get("/api/search", params={
        "type": "playlist", "query": query, "media_kind": "music",
        "limit": 3, "offset": 0,
    })
    playlists = (data or {}).get("playlists", {}).get("items", [])
    if playlists:
        p = playlists[0]
        ok = await ot_post("/api/queue/items/add", params={
            "uris": p["uri"], "clear": "true", "playback": "start",
        })
        if ok:
            await ot_select_output()
            return f"正在播放歌单 {p.get('name', '')}"
        return "播放失败"
    return f"没找到 {query}"


TRACKS_CACHE = {"time": 0.0, "items": []}


async def ot_library_tracks(force=False):
    """拉取全部曲目并缓存 5 分钟"""
    if not force and TRACKS_CACHE["items"] and time.time() - TRACKS_CACHE["time"] < 300:
        return TRACKS_CACHE["items"]
    items = []
    offset = 0
    while True:
        data = await ot_get("/api/search", params={
            "type": "track", "query": "", "media_kind": "music",
            "limit": 500, "offset": offset,
        })
        if not data:
            break
        tracks = data.get("tracks", {})
        batch = tracks.get("items", [])
        items.extend(batch)
        total = tracks.get("total", 0)
        offset += len(batch)
        if not batch or offset >= total:
            break
    if items:
        TRACKS_CACHE["items"] = items
        TRACKS_CACHE["time"] = time.time()
        log.info("曲库缓存: %d 首", len(items))
    return TRACKS_CACHE["items"]


def _norm(s):
    return re.sub(r"[\s,，。.!！?？、·'\"\"''\-_（）()《》的]", "", s or "")


async def ot_fuzzy_track(query, threshold=75):
    """模糊匹配曲库, 返回最佳曲目或 None"""
    tracks = await ot_library_tracks()
    if not tracks:
        return None
    from rapidfuzz import fuzz
    q = _norm(query)
    if not q:
        return None
    best, best_score = None, 0.0
    for t in tracks:
        title = _norm(t.get("title") or t.get("name") or "")
        artist = _norm(t.get("artist", ""))
        if not title:
            continue
        score = max(
            fuzz.partial_ratio(q, title),
            fuzz.ratio(q, title),
            fuzz.ratio(q, artist + title) if artist else 0,
        )
        if score > best_score:
            best, best_score = t, score
    if best and best_score >= threshold:
        log.info("模糊匹配: %s -> %s - %s (%.0f)", query,
                 best.get("artist"), best.get("title") or best.get("name"), best_score)
        return best
    log.info("模糊匹配失败: %s (最佳 %.0f)", query, best_score)
    return None


async def ot_action(action, arg=None):
    mapping = {
        "next": "/api/player/next",
        "previous": "/api/player/previous",
        "pause": "/api/player/pause",
        "play": "/api/player/play",
        "stop": "/api/player/stop",
    }
    if action == "volume_set":
        await ot_put("/api/player/volume", params={"volume": int(arg)})
        return
    await ot_put(mapping[action])


async def ot_volume_delta(delta):
    data = await ot_get("/api/player")
    cur = 50
    if data:
        cur = data.get("volume", 50)
    await ot_action("volume_set", min(100, max(0, cur + delta)))


# ---------------- 指令解析 ----------------

CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def cn_to_int(s):
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s in CN_NUM:
        return CN_NUM[s]
    if "十" in s:
        parts = s.split("十")
        tens = CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return None


async def handle_command(query):
    cfg = STATE["config"]
    trig = cfg["trigger"]
    if not query.startswith(trig):
        return False
    rest = query[len(trig):].strip("，。,. ")
    log.info("命中指令: %s -> %s", query, rest)
    STATE["last_command"] = f"{time.strftime('%H:%M:%S')} {query}"

    reply = "好的"
    try:
        m = re.match(r"^(?:播放|放|来一首|来首|我想听|我要听)(.+)$", rest)
        if m:
            reply = await ot_play_search(m.group(1).strip())
        elif rest in ("下一首", "下首歌", "切歌"):
            await ot_action("next")
            reply = "下一首"
        elif rest in ("上一首", "上首歌"):
            await ot_action("previous")
            reply = "上一首"
        elif rest in ("暂停", "暂停播放"):
            await ot_action("pause")
            reply = "已暂停"
        elif rest in ("继续", "继续播放"):
            await ot_action("play")
            reply = "继续播放"
        elif rest in ("停止", "停止播放", "关机"):
            await ot_action("stop")
            reply = "已停止"
        elif rest in ("刷新列表", "刷新曲库", "更新曲库"):
            await ot_put("/api/update")
            tracks = await ot_library_tracks(force=True)
            reply = f"曲库已刷新, 共 {len(tracks)} 首"
        elif rest in ("音量大一点", "大点声", "声音大一点"):
            await ot_volume_delta(+10)
            reply = "音量加大"
        elif rest in ("音量小一点", "小点声", "声音小一点"):
            await ot_volume_delta(-10)
            reply = "音量减小"
        else:
            m2 = re.match(r"^音量(?:调到)?([0-9零一二两三四五六七八九十百]+)$", rest)
            if m2:
                vol = cn_to_int(m2.group(1))
                if vol is not None:
                    await ot_action("volume_set", min(100, max(0, vol)))
                    reply = f"音量调到 {vol}"
                else:
                    return True
            else:
                reply = await ot_play_search(rest)
    except Exception as e:
        log.error("执行指令失败: %s", e)
        reply = "出错了, 请稍后再试"

    await mi_pause_speaker()
    await mi_tts(reply)
    return True


# ---------------- 轮询主循环 ----------------

async def poll_loop():
    last_ts = int(time.time() * 1000)
    while True:
        cfg = STATE["config"]
        ready = (
            cfg["mi_user"] and cfg["mi_pass"] and cfg["speaker_device_id"]
            and cfg["speaker_hardware"] and cfg["output_id"]
        )
        if not ready:
            await asyncio.sleep(3)
            continue
        if not STATE["mi_logged_in"]:
            if not await mi_login():
                await asyncio.sleep(10)
                continue
        rec = await fetch_latest_query()
        if rec and rec["time"] > last_ts:
            last_ts = rec["time"]
            q = rec["query"]
            STATE["last_seen_query"] = q
            log.info("收到对话: %s", q)
            try:
                await handle_command(q)
            except Exception as e:
                log.error("处理指令异常: %s", e)
        await asyncio.sleep(POLL_INTERVAL)


# ---------------- Web 设置界面 ----------------

PAGE = """<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小爱→AirPlay 桥接</title>
<style>
body {{ font-family: -apple-system, "PingFang SC", sans-serif; max-width: 640px;
  margin: 24px auto; padding: 0 16px; color: #222; }}
h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 28px; }}
.card {{ border: 1px solid #ddd; border-radius: 10px; padding: 16px; margin: 12px 0; }}
input[type=text], input[type=password] {{ width: 100%; box-sizing: border-box;
  padding: 8px; margin: 4px 0 12px; border: 1px solid #ccc; border-radius: 6px; }}
button, .btn {{ background: #07c160; color: #fff; border: 0; padding: 10px 18px;
  border-radius: 8px; font-size: 15px; cursor: pointer; text-decoration: none;
  display: inline-block; }}
label {{ font-size: 14px; color: #555; }}
.item {{ display: flex; justify-content: space-between; align-items: center;
  padding: 8px 0; border-bottom: 1px solid #eee; }}
.ok {{ color: #07c160; }} .err {{ color: #e64340; }}
.muted {{ color: #999; font-size: 13px; }}
.warn {{ background: #fffbe6; border: 1px solid #ffe58f; border-radius: 8px;
  padding: 12px; margin-top: 12px; }}
</style></head><body>
<h1>🎵 小爱 → AirPlay 桥接</h1>
<div class="card">
  <div>小米登录: {mi_status}</div>
  <div>OwnTone: {ot_status}</div>
  <div>监听音箱: {speaker}</div>
  <div>输出音箱: {output}</div>
  <div>触发词: 「{trigger}」 <span class="muted">例如说: 小爱同学, {trigger}播放晴天</span></div>
  <div class="muted">最近指令: {last_command}</div>
  <div class="muted">最近听到: {last_query}</div>
</div>

<h2>1. 小米账号</h2>
<div class="card"><form method="post" action="/setup/mi">
  <label>小米账号(手机号/邮箱)</label>
  <input type="text" name="mi_user" value="{mi_user}" autocomplete="off">
  <label>小米密码</label>
  <input type="password" name="mi_pass" autocomplete="off">
  <button type="submit">登录并获取音箱列表</button>
</form>
{tfa_block}
</div>

<h2>2. 选择监听的小爱音箱</h2>
<div class="card">{device_list}</div>

<h2>3. 选择输出的 AirPlay 音箱</h2>
<div class="card">{output_list}</div>

<h2>4. 触发词</h2>
<div class="card"><form method="post" action="/setup/trigger">
  <label>语音指令前缀(默认"影院")</label>
  <input type="text" name="trigger" value="{trigger}">
  <button type="submit">保存</button>
</form></div>
</body></html>"""

TFA_BLOCK = """<div class="warn">
  <b>小米风控验证</b>：新设备登录需要短信验证<br>
  <span class="muted">点下方按钮给你手机发验证码，收到后在下面输入，全程不用离开本页面。</span><br><br>
  <form method="post" action="/setup/send_code" style="display:inline">
    <button type="submit">发送验证码</button>
  </form>
  <span class="muted">{send_hint}</span>
  <form method="post" action="/setup/verify_code">
    <label>短信验证码</label>
    <input type="text" name="code" autocomplete="off" placeholder="6位数字">
    <button type="submit">完成验证并登录</button>
  </form>
</div>"""


def render_page():
    cfg = STATE["config"]
    mi_status = ('<span class="ok">已登录</span>' if STATE["mi_logged_in"]
                 else f'<span class="err">未登录 {STATE["mi_error"]}</span>')
    ot_status = ('<span class="ok">已连接</span>' if STATE["ot_ok"]
                 else '<span class="err">未连接</span>')
    tfa_block = ""
    if STATE["mi_need_2fa"] and not STATE["mi_logged_in"]:
        tfa_block = TFA_BLOCK.replace("{send_hint}", STATE["mi_2fa_hint"])
    if STATE["devices"]:
        device_list = "".join(
            f'<div class="item"><span>{d["name"]} <span class="muted">{d["hardware"]}</span></span>'
            f'<a class="btn" href="/setup/speaker?device_id={d["deviceID"]}'
            f'&name={d["name"]}&hardware={d["hardware"]}&did={d["did"]}">选它</a></div>'
            for d in STATE["devices"]
        )
    else:
        device_list = '<span class="muted">先完成第 1 步登录</span>'
    if STATE["ot_outputs"]:
        output_list = "".join(
            f'<div class="item"><span>{o["name"]} <span class="muted">{o["type"]}'
            f'{" · 已选中" if o["selected"] else ""}</span></span>'
            f'<a class="btn" href="/setup/output?output_id={o["id"]}'
            f'&name={o["name"]}">选它</a></div>'
            for o in STATE["ot_outputs"]
        )
    else:
        output_list = '<span class="muted">未获取到输出设备, <a href="/setup/refresh_outputs">刷新</a></span>'
    return PAGE.format(
        mi_status=mi_status, ot_status=ot_status,
        speaker=cfg["speaker_name"] or "未选择", output=cfg["output_name"] or "未选择",
        trigger=cfg["trigger"], last_command=STATE["last_command"] or "-",
        last_query=STATE["last_seen_query"] or "-",
        mi_user=cfg["mi_user"], device_list=device_list, output_list=output_list,
        tfa_block=tfa_block,
    )


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index():
    return render_page()


@app.post("/setup/mi")
async def setup_mi(mi_user: str = Form(...), mi_pass: str = Form(...)):
    STATE["config"]["mi_user"] = mi_user.strip()
    STATE["config"]["mi_pass"] = mi_pass.strip()
    save_config(STATE["config"])
    # 清了旧 token 重新来
    if os.path.isfile(TOKEN_PATH):
        os.remove(TOKEN_PATH)
    ok = await mi_login()
    if ok:
        STATE["mi_need_2fa"] = False
        await mi_fetch_devices()
    return RedirectResponse("/", status_code=303)


@app.post("/setup/send_code")
async def setup_send_code():
    hint = await tfa_send_code()
    STATE["mi_2fa_hint"] = "验证码已发送, 请查收短信" if hint == "ok" else hint
    return RedirectResponse("/", status_code=303)


@app.post("/setup/verify_code")
async def setup_verify_code(code: str = Form(...)):
    result = await tfa_verify_code(code)
    if result == "ok":
        if await mi_init_from_token():
            STATE["mi_need_2fa"] = False
            await mi_fetch_devices()
    else:
        STATE["mi_2fa_hint"] = result
    return RedirectResponse("/", status_code=303)


@app.get("/setup/speaker")
async def setup_speaker(device_id: str, name: str, hardware: str, did: str = ""):
    STATE["config"].update({
        "speaker_device_id": device_id, "speaker_name": name,
        "speaker_hardware": hardware, "speaker_did": did,
    })
    save_config(STATE["config"])
    return RedirectResponse("/", status_code=303)


@app.get("/setup/refresh_outputs")
async def refresh_outputs():
    if await ot_check():
        await ot_fetch_outputs()
    return RedirectResponse("/", status_code=303)


@app.get("/setup/output")
async def setup_output(output_id: str, name: str):
    STATE["config"]["output_id"] = output_id
    STATE["config"]["output_name"] = name
    save_config(STATE["config"])
    return RedirectResponse("/", status_code=303)


@app.post("/setup/trigger")
async def setup_trigger(trigger: str = Form(...)):
    STATE["config"]["trigger"] = trigger.strip() or "影院"
    save_config(STATE["config"])
    return RedirectResponse("/", status_code=303)


@app.get("/status")
async def status():
    cfg = dict(STATE["config"])
    cfg["mi_pass"] = "***" if cfg["mi_pass"] else ""
    cfg["ot_pass"] = "***"
    return JSONResponse({
        "config": cfg,
        "mi_logged_in": STATE["mi_logged_in"],
        "mi_need_2fa": STATE["mi_need_2fa"],
        "mi_2fa_hint": STATE["mi_2fa_hint"],
        "ot_ok": STATE["ot_ok"],
        "last_command": STATE["last_command"],
        "last_seen_query": STATE["last_seen_query"],
    })


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8091")))
