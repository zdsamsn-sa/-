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

每轮流程：
  1. MTProto 登录（Telethon StringSession，来自 Secrets）
  2. 发 /checkin 签到，打印机器人回复
  3. RequestWebView 现场生成新鲜 initData（不依赖网页版会话）
  4. state -> harvest-all -> plant 空地 -> visit/steal 循环直到次数用完
  5. 按最终状态算下次成熟时间，写入汇总和 GitHub Step Summary
  6. 打印汇总；全程不购买、不充值

定时策略（不要 2 小时轮询，改成成熟触发）：
  - 每次跑完，脚本按地块 remainingSec 算出下次成熟时间，用 GitHub API 改写
    workflow 里标记的那行 cron，到点自动跑一次；收完、重种后，下一轮成熟
    时间会再次改写，链条这样续下去。
  - 每天 11:15 北京时间另有一行兜底 cron：成熟定时改写失败时，仍保证每天
    至少跑一次，兜底轮也会重新算成熟时间并改写，链条能自己接回来。
  - 距成熟不到 45 分钟时，本轮直接睡到成熟后 2 分钟再跑一轮，免得等定时传播。
  - 改写定时失败不影响本次收菜（只记日志），兜底轮会再试。

环境变量（必填）：
  TG_API_ID, TG_API_HASH, TG_SESSION_STRING
Actions 自动提供（改写定时用，本地跑时没有则跳过改写）：
  GITHUB_REPOSITORY, GH_TOKEN
可选：
  FARM_DEBUG=1  打印原始 state/visit JSON（首次调试用）

退出码：0 成功；1 失败；2 缺少环境变量
"""

import asyncio
import base64
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, unquote, urlparse

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.messages import RequestWebViewRequest

BOT = "@yzaws_bot"
WEBAPP_URL = "https://cd.sd/farm-game"
API_BASE = "https://cd.sd"
TIMEOUT = 30
WORKFLOW_PATH = ".github/workflows/farm-daily.yml"
MATURITY_MARKER = "# 下次成熟（脚本自动更新）"
SLEEP_THRESHOLD_SECS = 45 * 60  # 距成熟不到 45 分钟时，本轮直接睡过去再跑一轮

try:
    from zoneinfo import ZoneInfo
    CST = ZoneInfo("Asia/Shanghai")
except Exception:
    CST = timezone(timedelta(hours=8))


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


def next_maturity(state):
    """按最终状态算下次成熟：返回 (描述, 成熟时间(CST)或None, 剩余秒数或None)。

    只看已种下、未成熟的地块，取最小 remainingSec；没有生长中的地块时
    说明原因（已有成熟作物 / 没有生长中的作物）。
    """
    plots = state.get("plots", []) or []
    growing = [p for p in plots
               if p.get("planted") and not p.get("mature")
               and isinstance(p.get("remainingSec"), (int, float))]
    if growing:
        secs = max(min(p["remainingSec"] for p in growing), 0)
        at = datetime.now(timezone.utc) + timedelta(seconds=secs)
        at_cst = at.astimezone(CST)
        desc = f"约 {secs / 3600:.1f} 小时后（{at_cst.strftime('%Y-%m-%d %H:%M')} 北京时间）"
        return desc, at_cst, secs
    if any(p.get("planted") and p.get("mature") for p in plots):
        return "已有成熟作物（本轮收菜未覆盖时下轮再收）", None, None
    return "没有生长中的作物", None, None


def write_step_summary(summary):
    """把汇总写进 GitHub Actions 的 Summary 页（本地跑时没有该环境变量，直接跳过）。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        harvest_n, harvest_gb = summary.get("harvest", (0, 0))
        steals, steal_gb = summary.get("steal", (0, 0))
        with open(path, "a", encoding="utf-8") as f:
            f.write("## 农场任务\n\n")
            f.write(f"- 签到：{summary.get('checkin_reply', '')[:120]}\n")
            f.write(f"- 收菜：{harvest_n} 块，+{harvest_gb}GB\n")
            f.write(f"- 重种：{summary.get('planted', 0)} 块\n")
            f.write(f"- 偷菜：{steals} 次，共 +{steal_gb}GB\n")
            f.write(f"- 下次成熟：{summary.get('next_mature', '未知')}\n")
    except Exception:
        pass


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


async def run_cycle(client, label, debug):
    """跑一轮完整任务：签到 -> initData -> 收菜 -> 重种 -> 偷菜 -> 算下次成熟。

    返回 (summary, 成熟时间(CST)或None, 剩余秒数或None)。
    """
    summary = {}
    log(f"--- {label} ---")

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

    # 7. 重新拉一次最终状态，再算下次成熟时间（plant 返回不一定带 plots，
    #    直接用本轮状态可能漏掉刚种下的地块）
    try:
        fresh = api("/farm/api/state", init_data)
        if action_ok(fresh):
            state = fresh
    except Exception as e:
        log(f"最终状态拉取失败，用本轮已知状态算成熟时间：{e}")
    desc, at_cst, secs = next_maturity(state)
    log(f"下次成熟：{desc}")
    summary["next_mature"] = desc
    if at_cst is not None:
        summary["next_mature_at"] = at_cst.isoformat(timespec="minutes")
    return summary, at_cst, secs


def update_workflow_cron(at_cst):
    """把 workflow 里标记的那行 cron 改成成熟时间（UTC）。失败只记日志，不抛异常。

    需要 Actions 环境变量 GITHUB_REPOSITORY 和 GH_TOKEN；本地跑时没有，直接跳过。
    改写失败不影响本次收菜——每天 11:15 的兜底轮会重新算并再试。
    """
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GH_TOKEN")
    if not repo or not token:
        log("不在 Actions 环境（无 GITHUB_REPOSITORY/GH_TOKEN），跳过更新 workflow 定时")
        return
    try:
        utc = at_cst.astimezone(timezone.utc)
        new_cron = f"{utc.minute} {utc.hour} {utc.day} {utc.month} *"
        headers = {"Authorization": f"Bearer {token}",
                   "Accept": "application/vnd.github+json"}
        url = f"https://api.github.com/repos/{repo}/contents/{WORKFLOW_PATH}"
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        pattern = re.compile(
            r"(" + re.escape(MATURITY_MARKER) + r"\n\s*- cron: \")[^\"]+(\")")
        new_content, n = pattern.subn(r"\g<1>" + new_cron + r"\g<2>", content)
        if n == 0:
            log("workflow 文件里找不到下次成熟标记行，跳过更新定时")
            return
        if new_content == content:
            log(f"workflow 定时已是 {new_cron}，无需更新")
            return
        body = {
            "message": f"农场：下次成熟 {at_cst.strftime('%Y-%m-%d %H:%M')} 北京时间（脚本自动更新）",
            "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
            "sha": data["sha"],
        }
        r2 = requests.put(url, headers=headers, json=body, timeout=30)
        r2.raise_for_status()
        log(f"workflow 定时已更新为 {new_cron}（UTC，即成熟时间）")
    except Exception as e:
        log(f"更新 workflow 定时失败（不影响本次收菜，兜底轮会再试）：{e}")


async def main():
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session_str = os.environ["TG_SESSION_STRING"]
    debug = os.environ.get("FARM_DEBUG") == "1"

    async with TelegramClient(StringSession(session_str), api_id, api_hash) as client:
        me = await client.get_me()
        log(f"MTProto 登录成功：@{me.username or me.id}")

        summary, at_cst, secs = await run_cycle(client, "第 1 轮", debug)

        # 距成熟不到 45 分钟时：本轮直接睡到成熟后 2 分钟，再跑一轮，
        # 免得等定时传播——成熟时间刚好卡在兜底轮之后时尤其有用。
        if secs is not None and 0 < secs <= SLEEP_THRESHOLD_SECS:
            wait = secs + 120
            log(f"距成熟不到 45 分钟（{secs / 60:.0f} 分钟），本轮睡 {wait / 60:.0f} 分钟后直接再跑一轮")
            await asyncio.sleep(wait)
            summary, at_cst, secs = await run_cycle(client, "第 2 轮（成熟后）", debug)
            summary["cycles"] = 2
        else:
            summary["cycles"] = 1

        # 按最后一轮的最终状态，改写 workflow 的成熟定时
        if at_cst is not None:
            update_workflow_cron(at_cst)
        else:
            log("没有生长中的作物，不改写成熟定时（靠每日 11:15 兜底轮续链）")

    write_step_summary(summary)
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
