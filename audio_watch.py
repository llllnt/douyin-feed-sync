# -*- coding: utf-8 -*-
"""直播间无声告警（GitHub Actions 长任务，与 feed 解析链/开播通知解耦）。

监看窗口：UTC 22:00 ~ 次日 16:00（北京 6:00-24:00 直播时段）。
运行方式：主 cron 21:55 UTC 启动，单实例循环运行约 5 小时后自接力 dispatch 下一次
（feed_loop 同款互斥模式：同名工作流已有实例在跑时秒退；另有每 2 小时后备 cron）。

每 CYCLE_SEC 秒一轮：
1. 读监播地址簿（afcbbaf）筛出在播房间；
2. 8 路并行用 ffmpeg 抓 CAPTURE_SEC 秒音频（flv 优先，失败回退 HLS），
   重采样 16kHz 单声道后跑 Silero VAD（models/silero_vad_16k.onnx，窗口 576 样本）
   得语音占比；
3. 状态机（状态 bin fedbadd）：语音样本刷新 lastSpeechAt；静默/无音轨样本不动
   时钟；**距上次语音满 MIN_SILENT_SEC（300s）且本事件未告警过 → 推飞书卡片**
   （同一次静默只告一次，恢复说话自动复位，再静默再告）；
4. 抓取失败 ≠ 静默（不改判、不计时），避免网络抖动误报。

依赖：pip install onnxruntime numpy；ffmpeg（GA ubuntu-latest 自带）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac as hmac_mod
import json
import os
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

ADDR_BIN = os.environ.get(
    "ADDR_BIN_URL", "https://extendsclass.com/api/json-storage/bin/afcbbaf")
STATE_BIN = os.environ.get(
    "STATE_BIN_URL", "https://extendsclass.com/api/json-storage/bin/fedbadd")
WEBHOOK = os.environ.get("NOTIFY_WEBHOOK_URL", "").strip()
SECRET = os.environ.get("NOTIFY_SECRET", "").strip()
MONITOR_BASE = os.environ.get(
    "MONITOR_BASE_URL", "https://pv-h6bbdwiu.aiproject.leqeegroup.com")

CAPTURE_SEC = float(os.environ.get("CAPTURE_SEC", "30"))
CYCLE_SEC = float(os.environ.get("CYCLE_SEC", "120"))
WORKERS = int(os.environ.get("WORKERS", "8"))
SILENT_THRESH = float(os.environ.get("SILENT_THRESH", "0.02"))  # 语音占比低于此值=静默样本
MIN_SILENT_SEC = float(os.environ.get("MIN_SILENT_SEC", "300"))  # 满 5 分钟无语音才告警
SEGMENT_MINUTES = float(os.environ.get("SEGMENT_MINUTES", "300"))  # 单实例最长 5 小时后接力
GRACE_BEFORE_START = float(os.environ.get("GRACE_BEFORE_START", "1800"))  # 窗口开始前最多等 30 分钟

# 监看窗口（UTC，跨天）：北京 6:00-24:00
WATCH_START_MIN = 22 * 60   # 22:00 UTC
WATCH_END_MIN = 16 * 60     # 次日 16:00 UTC

WATCH_MINUTES_OVERRIDE = float(os.environ.get("WATCH_MINUTES_OVERRIDE", "0"))  # >0=手动测试
REPO = os.environ.get("GITHUB_REPOSITORY", "")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
WORKFLOW_NAME = os.environ.get("GITHUB_WORKFLOW", "")
WORKFLOW_REF = os.environ.get("WORKFLOW_FILE", "audio-watch.yml")
TOKEN = os.environ.get("GITHUB_TOKEN", "")

CST = timezone(timedelta(hours=8))
MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "models", "silero_vad_16k.onnx")


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def http_json(method, url, body=None, timeout=15):
    req = urllib.request.Request(
        url + ("?_t=%d" % int(time.time() * 1000) if method == "GET" else ""),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": "audio-watch"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}


def gh_api(method, path, body=None):
    req = urllib.request.Request(
        "https://api.github.com/repos/%s%s" % (REPO, path),
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "audio-watch"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}


def other_active():
    """同名工作流其他在跑实例数（互斥依据，照搬 feed_loop）"""
    try:
        d = gh_api("GET", "/actions/runs?status=in_progress&per_page=50")
        return sum(1 for r in d.get("workflow_runs", [])
                   if str(r.get("id")) != str(RUN_ID) and r.get("name") == WORKFLOW_NAME)
    except Exception as e:
        log("互斥检查失败（按无冲突继续）: %s" % str(e)[:80])
        return 0


def in_window(now):
    t = time.gmtime(now)
    m = t.tm_hour * 60 + t.tm_min
    if WATCH_START_MIN < WATCH_END_MIN:
        return WATCH_START_MIN <= m < WATCH_END_MIN
    return m >= WATCH_START_MIN or m < WATCH_END_MIN


def window_end_deadline(now):
    """当前窗口的结束时刻（epoch）。now 必须在窗口内。"""
    t = time.gmtime(now)
    base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)) - time.timezone
    if t.tm_hour * 60 + t.tm_min >= WATCH_START_MIN:
        return base + 86400 + WATCH_END_MIN * 60  # 次日 end
    return base + WATCH_END_MIN * 60


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


# ── Silero VAD（窗口 576 样本 @16kHz，state [2,1,128]）──
_session = None


def _sess():
    global _session
    if _session is None:
        import numpy as np
        import onnxruntime as ort
        _session = (ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"]), np)
    return _session


def speech_ratio(pcm_bytes, sr=16000, thresh=0.5):
    s, np = _sess()
    audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if len(audio) < 576:
        return None
    state = np.zeros((2, 1, 128), dtype=np.float32)
    sr_arr = np.array(sr, dtype=np.int64)
    speech = total = 0
    for i in range(0, len(audio) - 575, 576):
        out, state = s.run(None, {"input": audio[i:i + 576].reshape(1, -1),
                                  "state": state, "sr": sr_arr})
        total += 1
        if float(np.array(out).flatten()[0]) >= thresh:
            speech += 1
    return speech / total if total else None


def _grab(url, seconds):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
           "-t", str(int(seconds)), "-i", url,
           "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=seconds + 45)
    except subprocess.TimeoutExpired:
        return "error", None
    if r.returncode == 0 and len(r.stdout) >= 16000 * 2 * seconds * 0.5:
        return "ok", r.stdout
    # 抓取失败：区分「无音轨」与「流不可用」——去掉 -map 探测视频是否可读
    if r.returncode != 0 and r.stdout == b"":
        probe = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
             "-t", "6", "-i", url, "-f", "null", "-"],
            capture_output=True, timeout=40)
        if probe.returncode == 0:
            return "noaudio", None
    return "error", None


def evaluate_room(room, seconds):
    """返回 (kind, ratio)：kind ∈ speech/silent/noaudio/error"""
    urls = [u for u in (room.get("url"), room.get("urlHls")) if u]
    for u in urls:
        status, pcm = _grab(u, seconds)
        if status == "ok":
            ratio = speech_ratio(pcm)
            if ratio is None:
                return "error", None
            return ("speech" if ratio >= SILENT_THRESH else "silent"), ratio
        if status == "noaudio":
            return "noaudio", None
    return "error", None


# ── 状态机（纯函数，便于测试）──
def update_room(prev, kind, ratio, now, min_silent=MIN_SILENT_SEC):
    """返回 (entry, alert)。error 样本不改判不计时；新房间热身（从观察起算静默）。"""
    if prev is None:
        return {"lastSpeechAt": now, "alerted": False,
                "lastRatio": ratio, "lastKind": kind}, False
    e = dict(prev)
    e["lastRatio"] = ratio
    e["lastKind"] = kind
    if kind == "speech":
        e["lastSpeechAt"] = now
        e["alerted"] = False
        return e, False
    if kind in ("silent", "noaudio"):
        if now - e.get("lastSpeechAt", now) >= min_silent and not e.get("alerted"):
            e["alerted"] = True
            return e, True
    return e, False  # error：保持 lastSpeechAt / alerted 不动


def room_link(room_id):
    return "%s/#/monitor/room/cloud-%s" % (MONITOR_BASE, room_id)


def build_card(alerts):
    lines = []
    for a in alerts:
        if a["kind"] == "noaudio":
            desc = "已持续约 %d 分钟**检测不到音轨**" % a["minutes"]
        else:
            desc = "已持续约 %d 分钟**无人说话**" % a["minutes"]
        lines.append("**%s**\n%s · [直达监播](%s)" % (a["name"], desc, room_link(a["id"])))
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": "🔇 无声告警（%d 间）" % len(alerts)},
                       "template": "orange"},
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n\n".join(lines)}},
                {"tag": "note", "elements": [{"tag": "plain_text",
                                              "content": "每 2 分钟采样 30 秒音频 · 恢复说话后自动复位，同一次静默只提醒一次"}]},
            ],
        },
    }


def push_feishu(card):
    payload = dict(card)
    if SECRET:
        ts = str(int(time.time()))
        sign = base64.b64encode(hmac_mod.new(
            ("%s\n%s" % (ts, SECRET)).encode("utf-8"), b"",
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


def save_state(prev, mode):
    http_json("PUT", STATE_BIN,
              {"v": 1, "updated": int(time.time()), "mode": mode, "rooms": prev})


def cycle(now, prev):
    """单轮：采样全部在播房间 → 更新状态机 → 批量告警 → 按需落盘。"""
    addr = http_json("GET", ADDR_BIN)
    live_rooms = [r for r in (addr.get("rooms") or [])
                  if r.get("status") == "live" and (r.get("url") or r.get("urlHls"))]
    live_ids = set()
    results = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(evaluate_room, r, CAPTURE_SEC): r for r in live_rooms}
        for f in as_completed(futs):
            r = futs[f]
            try:
                results[str(r.get("id"))] = f.result()
            except Exception as e:
                log("采样异常 %s: %s" % (r.get("name"), str(e)[:60]))
                results[str(r.get("id"))] = ("error", None)

    changed = False
    alerts = []
    speech_n = silent_n = noaudio_n = error_n = 0
    for r in live_rooms:
        rid = str(r.get("id"))
        live_ids.add(rid)
        kind, ratio = results.get(rid, ("error", None))
        if kind == "speech":
            speech_n += 1
        elif kind == "silent":
            silent_n += 1
        elif kind == "noaudio":
            noaudio_n += 1
        else:
            error_n += 1
            continue  # 抓取失败：不改判不计时
        entry, alert = update_room(prev.get(rid), kind, ratio, now, MIN_SILENT_SEC)
        if entry != prev.get(rid):
            changed = True
        prev[rid] = entry
        if alert:
            alerts.append({"id": rid, "name": r.get("name") or rid, "kind": kind,
                           "minutes": int((now - entry["lastSpeechAt"]) // 60) or 1})
            log("⚠ %s %s，持续 %d 分钟" % (r.get("name"), kind, alerts[-1]["minutes"]))

    for rid in list(prev.keys()):  # 已下播房间清出状态
        if rid not in live_ids:
            del prev[rid]
            changed = True

    if alerts:
        if push_feishu(build_card(alerts)):
            log("无声告警已推送（%d 间）" % len(alerts))
            save_state(prev, "watch")
        else:
            log("飞书推送失败：保留状态，下轮重试同一批告警")
            return False
    elif changed:
        save_state(prev, "watch")
    log("本轮：在播 %d，语音 %d，静默 %d，无音轨 %d，采样失败 %d"
        % (len(live_rooms), speech_n, silent_n, noaudio_n, error_n))
    return True


def main():
    now = time.time()
    if not WEBHOOK:
        log("未配置 NOTIFY_WEBHOOK_URL，跳过")
        return 0

    if WATCH_MINUTES_OVERRIDE > 0:
        deadline = now + WATCH_MINUTES_OVERRIDE * 60
        log("手动模式：监看 %g 分钟" % WATCH_MINUTES_OVERRIDE)
    else:
        if not in_window(now):
            # 窗口未开：若 30 分钟内将开窗则等待，否则退出交给后备 cron
            t = time.gmtime(now)
            base = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)) - time.timezone
            m = t.tm_hour * 60 + t.tm_min
            start_today = base + WATCH_START_MIN * 60
            start = start_today if start_today > now else start_today + 86400
            wait = start - now
            if wait <= GRACE_BEFORE_START:
                log("距窗口开始还有 %ds，等待…" % int(wait))
                time.sleep(wait)
                now = time.time()
            else:
                log("不在监看窗口内且距开始超过 30 分钟，退出（后备 cron 会接手）")
                return 0
        if other_active() > 0:
            log("已有实例在跑，本次触发秒退（后备不叠加）")
            return 0
        deadline = min(now + SEGMENT_MINUTES * 60, window_end_deadline(now))
        log("进入监看窗口，本段截止 %s（UTC）"
            % time.strftime("%H:%M", time.gmtime(deadline)))

    if not os.path.exists(MODEL_PATH):
        log("模型文件缺失: %s" % MODEL_PATH)
        return 1
    prev = load_state()
    rounds = 0
    push_failed = False
    while True:
        t0 = time.time()
        if t0 >= deadline:
            break
        try:
            if not cycle(t0, prev):
                push_failed = True
        except Exception as e:
            log("本轮异常（继续下一轮）: %s" % str(e)[:100])
        rounds += 1
        remain_cycle = CYCLE_SEC - (time.time() - t0)
        remain_deadline = deadline - time.time()
        if remain_deadline <= 0:
            break
        time.sleep(max(1.0, min(remain_cycle, remain_deadline)))
    save_state(prev, "watch")
    log("监看结束，共 %d 轮" % rounds)

    # 接力：仍在窗口内（非手动模式）→ dispatch 下一段
    if WATCH_MINUTES_OVERRIDE <= 0 and in_window(time.time()):
        try:
            gh_api("POST", "/actions/workflows/%s/dispatches" % WORKFLOW_REF, {"ref": "main"})
            log("已接力触发下一段监看")
        except Exception as e:
            log("接力触发失败（后备 cron 2 小时内接手）: %s" % str(e)[:80])
    return 1 if push_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
