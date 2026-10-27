# -*- coding: utf-8 -*-
"""开播通知（GitHub Actions 版，与 feed_loop 解析链解耦）。

每 10 分钟由 .github/workflows/live-notify.yml 调度：
- 读监播地址簿（extendsclass afcbbaf）拿到 22 间房的实时状态；
- 与状态 bin（dcdcbca）里的上次快照对比，发现「未开播 → 开播」跳变；
- 批量推送一条飞书群机器人卡片（含房间名、检测时间、监播详情直达链接）；
- 同一房间 2 小时内重复开播不重复提醒（防上下播抖动刷屏）；
- 首次运行只建立基线快照，不把已在播的房间当「新开播」轰炸；
- webhook 推送失败时不更新快照，下一轮自动重试，不丢通知；
- 未配置 NOTIFY_WEBHOOK_URL 时静默跳过（工作流可先行部署）。

只依赖标准库，兼容 Python 3.8+。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ADDR_BIN = os.environ.get(
    "ADDR_BIN_URL", "https://extendsclass.com/api/json-storage/bin/afcbbaf")
STATE_BIN = os.environ.get(
    "STATE_BIN_URL", "https://extendsclass.com/api/json-storage/bin/dcdcbca")
WEBHOOK = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
SECRET = os.environ.get("NOTIFY_SECRET", "").strip()  # 飞书机器人签名校验密钥
COOLDOWN = int(os.environ.get("NOTIFY_COOLDOWN_SEC", "7200"))  # 2 小时冷却
MONITOR_BASE = os.environ.get(
    "MONITOR_BASE_URL", "https://pv-h6bbdwiu.aiproject.leqeegroup.com")
CST = timezone(timedelta(hours=8))  # 东八区


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def http_json(method, url, body=None, timeout=15):
    req = urllib.request.Request(
        url + ("?_t=%d" % int(time.time() * 1000) if method == "GET" else ""),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": "live-notify"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}


def room_link(room_id):
    return "%s/#/monitor/room/cloud-%s" % (MONITOR_BASE, room_id)


def build_card(newly_live):
    """飞书互动卡片：批量列出本轮新开播房间。"""
    lines = []
    for r in newly_live:
        lines.append("**%s**\n%s 检测到开播 · [直达监播](%s)"
                     % (r["name"], r["time_str"], room_link(r["id"])))
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🔴 开播提醒（%d 间）" % len(newly_live)},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(lines)}},
                {"tag": "note", "elements": [{"tag": "plain_text",
                                              "content": "10 分钟轮询检测 · 同一房间 2 小时内重复开播不重复提醒"}]},
            ],
        },
    }


def push_feishu(card):
    """推送飞书，成功返回 True（一次重试）。开启签名校验时附加 timestamp+sign。"""
    import base64
    import hashlib
    import hmac as hmac_mod
    payload = dict(card)
    if SECRET:
        ts = str(int(time.time()))
        string_to_sign = "%s\n%s" % (ts, SECRET)
        sign = base64.b64encode(
            hmac_mod.new(string_to_sign.encode("utf-8"), b"",
                         digestmod=hashlib.sha256).digest()).decode("utf-8")
        payload["timestamp"] = ts
        payload["sign"] = sign
    for attempt in range(2):
        try:
            resp = http_json("POST", WEBHOOK, payload, timeout=10)
            if resp.get("code") == 0 or resp.get("StatusCode") == 0:
                return True
            log("飞书返回异常: %s" % str(resp)[:120])
        except Exception as e:
            log("飞书推送失败（第%d次）: %s" % (attempt + 1, str(e)[:100]))
        time.sleep(2)
    return False


def main():
    if not WEBHOOK:
        log("未配置 NOTIFY_WEBHOOK_URL，跳过（部署占位，配置后自动生效）")
        return 0

    now = time.time()
    addr = http_json("GET", ADDR_BIN)
    rooms = addr.get("rooms") or []
    log("地址簿 %d 间房" % len(rooms))

    try:
        state = http_json("GET", STATE_BIN)
    except Exception:
        state = {}
    prev = state.get("rooms") if isinstance(state.get("rooms"), dict) else {}
    baseline = not prev  # 首次运行：只建基线，不通知

    newly_live = []
    next_state = {}
    for r in rooms:
        rid = str(r.get("id", ""))
        if not rid:
            continue
        cur = "live" if r.get("status") == "live" else "offline"
        p = prev.get(rid, {})
        pst = p.get("status")
        entry = {"status": cur, "notifiedAt": p.get("notifiedAt", 0)}
        if not baseline and pst == "offline" and cur == "live" \
                and now - entry["notifiedAt"] >= COOLDOWN:
            newly_live.append({
                "id": rid,
                "name": r.get("name") or rid,
                "time_str": datetime.fromtimestamp(now, tz=CST).strftime("%H:%M"),
            })
            entry["notifiedAt"] = now  # 推送成功才落盘（见下）
        next_state[rid] = entry

    if baseline:
        log("首次运行：建立基线快照（%d 间），不发送通知" % len(next_state))
    elif newly_live:
        log("检测到 %d 间新开播: %s" % (len(newly_live), "、".join(r["name"] for r in newly_live)))
        if push_feishu(build_card(newly_live)):
            log("飞书推送成功")
        else:
            log("飞书推送失败：保留旧快照，下轮重试")
            return 1  # 不写状态，下轮重新检测同一批跳变
    else:
        log("无新开播")

    http_json("PUT", STATE_BIN, {"v": 1, "updated": int(now), "rooms": next_state})
    log("状态快照已更新（%d 间）" % len(next_state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
