#!/usr/bin/env python3
"""@yzaws_bot 农场每日任务（GitHub Actions 版）

接口契约全部实测自 https://cd.sd/js/farm-game.js（2026-10-27），没有猜测的接口：
  - 统一 POST + Content-Type: application/json，body = {"initData": ...} + 各接口字段
  - POST /farm/api/state        {initData}                  -> 状态（含 plots）
  - POST /farm/api/harvest-all  {initData}                  -> action{count, gainedGb}
  - POST /farm/api/plant        {initData, slot}            -> action + 最新状态
  - POST /farm/api/visit        {initData}                  -> 邻居对象
  - POST /farm/api/steal        {initData, victimId, slot}  -> gainedGb, stealLeftToday
  - 小程序里没有签到接口：签到就是给机器人发 /checkin 命令

流程：
  1. MTProto 登录（Telethon StringSession，来自 Secrets）
  2. 发 /checkin 签到，打印机器人回复
  3. RequestWebView 现场生成新鲜 initData（不依赖网页版会话）
  4. state -> harvest-all -> plant 空地 -> visit/steal 循环直到次数用完
  5. 打印汇总；全程不购买、不充值

环境变量（必填）：
  TG_API_ID, TG_API_HASH, TG_SESSION_STRING
可选：
  FARM_DEBUG=1  打印原始 state/visit JSON（首次调试用）

退出码：0 成功；1 失败；2 缺少环境变量
"""

import asyncio
import json
import os
import sys
from urllib.parse import parse_qsl, unquote, urlparse

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import RequestWebViewRequest

BOT = "@yzaws_bot"
WEBAPP_URL = "https://cd.sd/farm-game"
API_BASE = "https://cd.sd"
TIMEOUT = 30


def log(msg):
    print(msg, flush=True)


def api(path, init_data, **fields):
    """调 cd.sd 后端接口，返回 JSON；HTTP 层异常直接抛给上层。"""
    body = {"initData": init_data}
    body.update(fields)
    r = requests.post(API_BASE + path, json=body, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def action_ok(resp):
    """JS 里的成功判定：顶层 ok 且 action（若有）也 ok。"""
    if resp.get("ok") is False:
        return False
    action = resp.get("action")
    if action is not None and action.get("ok") is False:
        return False
    return True


async def get_init_data(client):
    """现场生成新鲜 initData：MTProto -> RequestWebView -> 取 fragment 里的 tgWebAppData。"""
    bot = await client.get_entity(BOT)
    res = await client(
        RequestWebViewRequest(
            peer=bot, bot=bot, platform="android",
            url=WEBAPP_URL, from_bot_menu=False,
        )
    )
    params = dict(parse_qsl(urlparse(res.url).fragment))
    raw = params.get("tgWebAppData", "")
    if not raw:
        raise RuntimeError("RequestWebView 返回里没有 tgWebAppData")
    init_data = unquote(raw)
    if not init_data:
        raise RuntimeError("tgWebAppData 解码后为空")
    return init_data


async def checkin(client):
    """给机器人发 /checkin，返回机器人回复文本（120 秒超时）。"""
    bot = await client.get_entity(BOT)
    async with client.conversation(bot, timeout=150) as conv:
        await conv.send_message("/checkin")
        try:
            resp = await conv.get_response(timeout=120)
        except asyncio.TimeoutError:
            return "(机器人 120 秒内未回复)"
        return (resp.text or "").strip()


async def main():
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session_str = os.environ["TG_SESSION_STRING"]
    debug = os.environ.get("FARM_DEBUG") == "1"

    summary = {}
    async with TelegramClient(StringSession(session_str), api_id, api_hash) as client:
        me = await client.get_me()
        log(f"MTProto 登录成功：@{me.username or me.id}")

        # 1. 签到（机器人命令，小程序里没有签到接口）
        reply = await checkin(client)
        log(f"签到 /checkin 回复：{reply[:300]}")
        summary["checkin_reply"] = reply[:300]

        # 2. 新鲜 initData（每轮现场生成，不要写死）
        init_data = await get_init_data(client)
        log(f"initData 已生成（长度 {len(init_data)}）")

        # 3. 状态
        state = api("/farm/api/state", init_data)
        if not action_ok(state):
            raise RuntimeError(f"state 失败：{state.get('error')}")
        if debug:
            log("state 原始：" + json.dumps(state, ensure_ascii=False)[:3000])
        log(f"状态：种子 {state.get('seeds')}/{state.get('seedCap')}，"
            f"地块 {state.get('unlocked')}/{state.get('totalPlots')}，"
            f"会员组 {state.get('group')}，单块产量 {state.get('cropYieldGb')}GB")

        # 4. 一键收菜（只收成熟的，未成熟的不动）
        h = api("/farm/api/harvest-all", init_data)
        act = h.get("action") or {}
        if act.get("ok"):
            log(f"收菜：{act.get('count')} 块，+{act.get('gainedGb')}GB")
            summary["harvest"] = (act.get("count"), act.get("gainedGb"))
        else:
            log(f"收菜：无可收获（{act.get('error', '')}）")
            summary["harvest"] = (0, 0)
        state = h if h.get("plots") is not None else api("/farm/api/state", init_data)

        # 5. 重种空地（有种子才种，种满或无空地即停）
        planted = 0
        for _ in range(10):  # 安全上限，避免死循环
            seeds = state.get("seeds", 0)
            empty = [p["slot"] for p in state.get("plots", [])
                     if not p.get("locked") and not p.get("planted")]
            if seeds <= 0 or not empty:
                break
            r = api("/farm/api/plant", init_data, slot=empty[0])
            if not action_ok(r):
                log(f"种地块 {empty[0]} 失败：{(r.get('action') or {}).get('error') or r.get('error')}")
                break
            planted += 1
            state = r if r.get("plots") is not None else state
        log(f"重种：{planted} 块，剩余种子 {state.get('seeds')}")
        summary["planted"] = planted

        # 6. 偷菜循环（按 JS 逻辑：偷失败就换一家，次数用完即停）
        stolen_gb = 0
        steals = 0
        no_target_victim = None
        for _ in range(20):  # 安全上限
            v = api("/farm/api/visit", init_data)
            if not v.get("ok", True) or v.get("error"):
                log(f"逛邻居结束：{v.get('error', '')}")
                break
            left = v.get("stealLeftToday", 0)
            if left <= 0:
                log("偷菜次数已用完")
                break
            if debug:
                log("visit 原始：" + json.dumps(v, ensure_ascii=False)[:2000])
            target = None
            for p in v.get("plots", []):
                if (p.get("mature") and not p.get("stolenByMe")
                        and (p.get("stealableLeftGb") or 0) > 0):
                    target = p
                    break
            if target is None:
                if v.get("victimId") == no_target_victim:
                    log("连续两家都无可偷地块，结束偷菜")
                    break
                no_target_victim = v.get("victimId")
                log(f"邻居 {v.get('victimName')} 无可偷地块，换一家")
                continue
            no_target_victim = None
            s = api("/farm/api/steal", init_data,
                    victimId=v["victimId"], slot=target["slot"])
            if not s.get("ok", True):
                log(f"偷菜失败（{s.get('error', '')}），换一家")
                continue
            gained = s.get("gainedGb", 0)
            stolen_gb += gained
            steals += 1
            log(f"偷菜：{v.get('victimName')} 地块 {target['slot']} +{gained}GB，"
                f"今日还剩 {s.get('stealLeftToday')} 次")
            if (s.get("stealLeftToday") or 0) <= 0:
                log("偷菜次数已用完")
                break
        log(f"偷菜：{steals} 次，共 +{stolen_gb}GB")
        summary["steal"] = (steals, stolen_gb)

    log("汇总：" + json.dumps(summary, ensure_ascii=False))
    log("完成，全程无购买操作。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyError as e:
        print(f"缺少环境变量：{e}，请检查 GitHub Secrets", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"失败：{e}", file=sys.stderr)
        sys.exit(1)
