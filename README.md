# @yzaws_bot 农场每日任务（GitHub Actions 版）

在 GitHub Actions 上跑，不走 Muse、不耗 token、不弹审批卡、不用反复扫码。

## 它每天做什么

1. MTProto 登录（Telethon 会话，存在 Secrets 里）
2. 给 @yzaws_bot 发 `/checkin` 签到（小程序里没有签到接口，签到就是机器人命令）
3. RequestWebView 现场生成新鲜 initData（小程序鉴权，不依赖网页版会话）
4. 调 `https://cd.sd` 后端接口：`state` → `harvest-all` 收菜 → `plant` 重种空地 → `visit` / `steal` 循环偷菜直到次数用完
5. 打印汇总；全程不购买、不充值

接口契约实测自 `https://cd.sd/js/farm-game.js`（2026-10-27），见 `farm_daily.py` 顶部的注释。

## 部署步骤

1. 新建一个 GitHub 仓库（公开或私有都行，私有更稳妥），把 `farm_daily.py` 放到仓库根目录，
   把 `farm-daily.yml` 放到 `.github/workflows/farm-daily.yml`。
2. 在仓库 Settings → Secrets and variables → Actions 里加三个 Secrets：
   - `TG_API_ID`
   - `TG_API_HASH`
   - `TG_SESSION_STRING`
3.  done。每天 11:15 北京时间自动跑；想手动跑就去 Actions 页点 Run workflow。

## 三个 Secrets 怎么来

### TG_API_ID / TG_API_HASH

去 https://my.telegram.org/apps 登录后创建应用（之前给 Telethon 导出用过的那套就能用），
把 api_id（数字）和 api_hash（字符串）分别贴进 Secrets。

### TG_SESSION_STRING（关键的一步，跑一次就行）

在任意一台有 Python 的机器上跑下面这段，首次会问手机号和验证码，登录成功后打印一串字符：

```python
import asyncio
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("api_id: "))
API_HASH = input("api_hash: ").strip()

async def main():
    async with TelegramClient(StringSession(), API_ID, API_HASH) as c:
        await c.start()
        print("TG_SESSION_STRING:")
        print(c.session.save())

asyncio.run(main())
```

把打印出的字符串完整贴进 Secrets 的 `TG_SESSION_STRING`。

**安全红线：这三个值绝不要提交进仓库、不要贴到聊天记录、不要写进代码文件。**
只放 Secrets。贴完后本地那段输出就可以删了。

## 运行时间

- 定时：`15 3 * * *`（UTC），即每天 11:15 北京时间。
- GitHub Actions 免费账号每月 2000 分钟额度，这个脚本一次跑 1 分钟左右，绰绰有余。

## 调试

- 首次跑建议在 workflow 里临时加 `FARM_DEBUG: "1"`，日志里会打印原始 state/visit 返回，确认字段无误后删掉。
- 常见失败：
  - `state 失败`：initData 失效（正常情况下每轮现场生成，不会过期；如果出现，先看 Actions 日志里的具体 error）。
  - 机器人 120 秒未回复：Telegram 侧抖动，第二天定时跑会自动重试，也可手动 Run workflow。
  - `RequestWebView 返回里没有 tgWebAppData`：MTProto 会话失效，需要重新导一次 `TG_SESSION_STRING`。
- 如果游戏后端改了接口（路径/字段变了），脚本会明确报错，不会静默编造数据——按报错和 `farm-game.js` 的最新内容对照改。

## 和原来 Muse 定时任务的关系

这套跑通后，原来的 `telegram-yzaws-daily`（每天 11:15 的 Muse 定时任务）就可以停了，
避免两边同一天重复执行（签到、偷菜次数都是按天算的，重复跑第二次会直接提示已用完，不会多扣，
但没必要浪费一次运行）。
