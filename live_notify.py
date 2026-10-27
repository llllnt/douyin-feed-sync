# -*- coding: utf-8 -*-
"""开播通知（GitHub Actions 版，与 feed_loop 解析链解耦）。

两种运行模式：
1. 常规模式（cron 每 10 分钟）：读监播地址簿（extendsclass afcbbaf）→ 与状态
   bin（dcdcbca）快照对比 → 发现「未开播 → 开播」跳变 → 推送飞书群机器人卡片。
2. 高频监看模式（burst，每天 UTC 21:50 即北京 5:50 启动，持续 150 分钟覆盖
   北京 6/7/8 点整点开播高峰）：每 POLL_SEC 秒一轮，对地址簿仍显示「未开播」
   的房间直接调用 douyin_feed_sync.fetch_room 实时解析抖音接口，绕开地址簿
   10 分钟刷新粒度，实现约 40~80 秒级开播检测。

规则：
- 「未开播 → 开播」且此前已连续离线满 MIN_OFFLINE_SEC（默认 600 秒）才提醒——
  短暂掉线抖动不重复提醒，整点真开播必提醒；
- 首次运行只建立基线快照，不把已在播的房间当「新开播」轰炸；
- 多间同时开播合并为一条卡片；推送失败保留旧快照，下一轮自动重试；
- 状态 bin 仅在内容变化时写入（burst 结束时兜底写一次，含 mode 标记）；
- 常规模式在 burst 窗口内（UTC 21:45 ~ 次日 00:25）自动跳过，避免重复通知；
- 未配置 NOTIFY_WEBHOOK_URL 时静默跳过（工作流可先行部署）；
- 开启飞书签名校验时（NOTIFY_SECRET）自动附加 timestamp+sign。

只依赖标准库，兼容 Python 3.8+。
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
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
MONITOR_BASE = os.environ.get(
    "MONITOR_BASE_URL", "https://pv-h6bbdwiu.aiproject.leqeegroup.com")
MIN_OFFLINE_SEC = float(os.environ.get("NOTIFY_MIN_OFFLINE_SEC", "600"))  # 掉线满 10 分钟再开播才提醒

BURST_MINUTES = float(os.environ.get("BURST_MINUTES", "0"))   # >0 进入高频监看
POLL_SEC = float(os.environ.get("POLL_SEC", "40"))            # burst 轮询间隔
BURST_REQ_GAP = float(os.environ.get("BURST_REQ_GAP", "0.5"))  # burst 每间解析间隔
DIRECT_RESOLVE = os.environ.get("DIRECT_RESOLVE", "1") != "0"  # 0=禁用直接解析（兜底/测试）
WINDOW_GUARD = os.environ.get("WINDOW_GUARD", "1") != "0"      # 0=禁用窗口互斥（测试）
# burst 窗口：UTC 21:45 ~ 次日 00:25（北京 5:45 ~ 8:25），常规 cron 在窗口内跳过
GUARD_START_MIN = 21 * 60 + 45
GUARD_END_MIN = 25

CST = timezone(timedelta(hours=8))  # 东八区

try:
    import douyin_feed_sync as _dfs  # 同仓库解析器：burst 模式直接解析房间状态
except Exception:
    _dfs = None


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


def load_state():
    for _ in range(3):
        try:
            st = http_json("GET", STATE_BIN)
            rooms = st.get("rooms")
            return rooms if isinstance(rooms, dict) else {}
        except Exception as e:
            log("读取状态 bin 失败（重试）: %s" % str(e)[:80])
            time.sleep(2)
    return {}


_opener = None


def direct_status(room_id):
    """burst 模式直接解析直播间状态：'live'/'offline'/None（失败=未知，不改判）。"""
    if not DIRECT_RESOLVE or _dfs is None or not room_id.isdigit():
        return None
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    try:
        r = _dfs.fetch_room("https://live.douyin.com/" + room_id, _opener)
        return "live" if r.get("status") == "live" else "offline"
    except Exception as e:
        log("  直接解析失败 %s: %s" % (room_id, str(e)[:60]))
        return None


def in_burst_window(now):
    """当前是否处于 burst 高频窗口（UTC）。"""
    t = time.gmtime(now)
    m = t.tm_hour * 60 + t.tm_min
    return m >= GUARD_START_MIN or m <= GUARD_END_MIN


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
                                              "content": "整点高峰 40 秒级监看 · 掉线 10 分钟内恢复不重复提醒"}]},
            ],
        },
    }


def push_feishu(card):
    """推送飞书，成功返回 True（一次重试）。开启签名校验时附加 timestamp+sign。"""
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


def check_once(now, prev, burst=False):
    """一轮检测。prev 为内存态快照 dict（原地更新）。返回 (rc, changed)：
    rc=1 表示有推送失败；changed 表示 prev 相对轮前有变化（需要落盘）。"""
    addr = http_json("GET", ADDR_BIN)
    rooms = addr.get("rooms") or []
    baseline = not prev

    newly_live = []
    pending = {}   # rid -> entry，推送成功后才并入 prev（失败则保留旧态下轮重试）
    changed = False
    resolved = 0
    for r in rooms:
        rid = str(r.get("id") or "")
        if not rid:
            continue
        cur = "live" if r.get("status") == "live" else "offline"
        p = prev.get(rid) or {}
        p_status = p.get("status") or "offline"
        p_off_since = p.get("offlineSince", 0)

        # burst：地址簿仍显示未开播且内存态也非在播 → 直接解析抖音接口（绕开地址簿 10 分钟粒度）
        if burst and cur == "offline" and p_status != "live":
            d = direct_status(rid)
            resolved += 1
            if d:
                cur = d
            time.sleep(BURST_REQ_GAP)

        if cur == "offline":
            off_since = p_off_since if p_status == "offline" else now
        else:
            off_since = 0
        entry = {"status": cur, "notifiedAt": p.get("notifiedAt", 0), "offlineSince": off_since}

        if not baseline and p_status == "offline" and cur == "live" \
                and now - p_off_since >= MIN_OFFLINE_SEC:
            newly_live.append({
                "id": rid,
                "name": r.get("name") or rid,
                "time_str": datetime.fromtimestamp(now, tz=CST).strftime("%H:%M"),
            })
            entry["notifiedAt"] = now
            pending[rid] = entry
        else:
            if entry != {"status": p_status, "notifiedAt": p.get("notifiedAt", 0),
                         "offlineSince": p_off_since}:
                changed = True
            prev[rid] = entry

    rc = 0
    if baseline:
        log("首次运行：建立基线快照（%d 间），不发送通知" % len(prev))
        changed = True
    elif newly_live:
        log("检测到 %d 间新开播: %s" % (len(newly_live), "、".join(r["name"] for r in newly_live)))
        if push_feishu(build_card(newly_live)):
            log("飞书推送成功")
            for rid, e in pending.items():
                prev[rid] = e
            changed = True
        else:
            log("飞书推送失败：保留旧快照，下轮重试")
            rc = 1
    else:
        if burst:
            log("本轮无新开播（直接解析 %d 间）" % resolved)
        else:
            log("无新开播")

    return rc, changed


def save_state(prev, mode):
    body = {"v": 1, "updated": int(time.time()), "mode": mode, "rooms": prev}
    http_json("PUT", STATE_BIN, body)


def run_single(now):
    if WINDOW_GUARD and in_burst_window(now):
        log("处于高频监看窗口（北京 5:45-8:25），由 burst 任务负责，本次跳过")
        return 0
    prev = load_state()
    rc, changed = check_once(now, prev, burst=False)
    if changed:
        save_state(prev, "cron")
        log("状态快照已更新（%d 间）" % len(prev))
    return rc


def run_burst(now):
    if not WEBHOOK:
        log("未配置 NOTIFY_WEBHOOK_URL，跳过（部署占位，配置后自动生效）")
        return 0
    log("进入高频监看：每 %ds 一轮，持续 %d 分钟（覆盖北京 6/7/8 点整点开播）"
        % (POLL_SEC, BURST_MINUTES))
    if _dfs is None:
        log("⚠ 未能导入 douyin_feed_sync，退化为仅读地址簿（10 分钟粒度）")
    prev = load_state()
    deadline = now + BURST_MINUTES * 60
    rounds = 0
    any_fail = 0
    while True:
        t0 = time.time()
        try:
            rc, changed = check_once(t0, prev, burst=True)
            any_fail = any_fail or rc
            if changed:
                save_state(prev, "burst")
        except Exception as e:
            log("本轮异常（继续下一轮）: %s" % str(e)[:100])
        rounds += 1
        remain = deadline - time.time()
        if remain <= 0:
            break
        time.sleep(min(POLL_SEC, max(1.0, remain)))
    save_state(prev, "burst")  # 兜底落盘（含 mode 标记）
    log("高频监看结束，共 %d 轮" % rounds)
    return 1 if any_fail else 0


def main():
    now = time.time()
    if BURST_MINUTES > 0:
        return run_burst(now)
    if not WEBHOOK:
        log("未配置 NOTIFY_WEBHOOK_URL，跳过（部署占位，配置后自动生效）")
        return 0
    return run_single(now)


if __name__ == "__main__":
    raise SystemExit(main())
