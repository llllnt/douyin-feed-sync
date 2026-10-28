# -*- coding: utf-8 -*-
"""断链自愈看门狗（GitHub Actions 版，自接力循环）。

单次运行内每 WATCH_INTERVAL 秒（默认 600）巡检一轮，跑满 WATCH_RUN_MINUTES（默认
120）后 workflow_dispatch 接力下一次运行——本仓库 cron 受 GitHub 严重限流（实测间隔
可达数小时），workflow_dispatch 不受影响（feed.yml/live-notify.yml 同款机制）。

两条监控链：
1) 供数链：地址簿 bin（extendsclass afcbbaf）顶层 updated 时间戳过期 → 取消可能
   卡死的同名工作流在跑实例，dispatch feed.yml 重启解析链；
2) 通知链：开播通知状态 bin（dcdcbca）顶层 updated 过期且无 live-notify 实例在跑
   → dispatch live-notify.yml 重启通知链（状态 bin 每次运行结束兜底写一次，正常
   链路最坏更新间隔 ≈ RUN_MINUTES 120 分钟，阈值留余量）。
bin 读取失败：保守策略——有对应实例在跑则不动（可能是 extendsclass 自身故障），
无实例在跑才 dispatch 兜底。

配合 feed_loop.py / live_notify.py 的互斥机制（已有实例在跑时新实例秒退），
不会并发堆积。
"""
from __future__ import annotations  # 兼容 Python 3.8/3.9

import json
import os
import time
import urllib.request

BIN_URL = os.environ.get(
    "FEED_BIN_URL", "https://extendsclass.com/api/json-storage/bin/afcbbaf")
NOTIFY_BIN_URL = os.environ.get(
    "NOTIFY_STATE_BIN_URL", "https://extendsclass.com/api/json-storage/bin/dcdcbca")
NOTIFY_WORKFLOW_NAME = os.environ.get("NOTIFY_WORKFLOW_NAME", "live-notify")
NOTIFY_WORKFLOW_REF = os.environ.get("NOTIFY_WORKFLOW_FILE", "live-notify.yml")
# 健康通知链最坏写入间隔 ≈ RUN_MINUTES 120 分钟 + 接力开销，180 分钟阈值留余量
NOTIFY_STALE_LIMIT = int(os.environ.get("NOTIFY_STALE_LIMIT", "10800"))
# 健康供数链最坏写入间隔 ≈ 每轮间隔 600s + 单轮硬超时 480s = 18 分钟；25 分钟阈值留出余量
STALE_LIMIT = int(os.environ.get("STALE_LIMIT", "1500"))
REPO = os.environ.get("GITHUB_REPOSITORY", "")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
WORKFLOW_NAME = os.environ.get("WATCH_WORKFLOW_NAME", "douyin-feed-sync")
WORKFLOW_REF = os.environ.get("WORKFLOW_FILE", "feed.yml")
WATCH_RELAY_REF = os.environ.get("WATCH_RELAY_FILE", "watchdog.yml")
SELF_WORKFLOW_NAME = os.environ.get("GITHUB_WORKFLOW", "feed-watchdog")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
WATCH_INTERVAL = int(os.environ.get("WATCH_INTERVAL", "600"))
WATCH_RUN_MINUTES = float(os.environ.get("WATCH_RUN_MINUTES", "120"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def gh_api(method, path, body=None):
    req = urllib.request.Request(
        "https://api.github.com/repos/%s%s" % (REPO, path),
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "feed-selfheal"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}


def bin_age(url):
    """bin 顶层 updated 距今天数（秒）；读取失败或字段缺失返回 None。"""
    try:
        req = urllib.request.Request(
            url + "?_t=%d" % int(time.time() * 1000),
            headers={"User-Agent": "feed-selfheal"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        updated = data.get("updated")
        if not updated:
            log("bin 缺少 updated 字段，无法判断新鲜度: %s" % url[-8:])
            return None
        return int(time.time()) - int(updated)
    except Exception as e:
        log("bin 读取失败 %s: %s" % (url[-8:], str(e)[:100]))
        return None


def active_runs(name):
    """指定工作流的在跑实例 id 列表；查询失败返回 None（无法判断）。"""
    try:
        d = gh_api("GET", "/actions/runs?status=in_progress&per_page=50")
        return [r["id"] for r in d.get("workflow_runs", [])
                if r.get("name") == name]
    except Exception as e:
        log("查询在跑实例失败: %s" % str(e)[:120])
        return None


def cancel_run(run_id):
    log("取消卡死实例 %s" % run_id)
    if DRY_RUN:
        log("[DRY_RUN] 跳过实际取消")
        return
    gh_api("POST", "/actions/runs/%s/cancel" % run_id)


def dispatch(ref):
    log("dispatch 重启（%s）" % ref)
    if DRY_RUN:
        log("[DRY_RUN] 跳过实际 dispatch")
        return True
    gh_api("POST", "/actions/workflows/%s/dispatches" % ref, {"ref": "main"})
    return True


def relay_self():
    """接力触发下一次 watchdog 运行。"""
    if not TOKEN:
        log("无 GITHUB_TOKEN，跳过自接力")
        return
    try:
        gh_api("POST", "/actions/workflows/%s/dispatches" % WATCH_RELAY_REF, {"ref": "main"})
        log("已接力触发下一次看门狗运行")
    except Exception as e:
        log("看门狗自接力失败（cron 后备将择机重启）: %s" % str(e)[:80])


def check_feed_chain():
    """供数链巡检：地址簿过期→取消卡死实例+dispatch feed.yml。"""
    age = bin_age(BIN_URL)
    if age is not None:
        log("地址簿 updated 距今 %d 秒（阈值 %d 秒）" % (age, STALE_LIMIT))
    if not TOKEN:
        log("未提供 GITHUB_TOKEN，供数链仅检查不能自愈")
        return
    if age is not None and age <= STALE_LIMIT:
        log("供数链健康，无需处理")
        return
    runs = active_runs(WORKFLOW_NAME)
    if age is None:
        # bin 读不出：可能是 extendsclass 故障，也可能是链路断了
        if runs is None:
            log("无法判断在跑实例，保守起见不动")
            return
        if runs:
            log("供数链有实例在跑（%d 个），疑似 extendsclass 故障，不动" % len(runs))
            return
        log("供数链无实例在跑，dispatch 兜底重启")
        dispatch(WORKFLOW_REF)
        return
    # 确认断链/卡死
    log("确认供数链异常（过期 %d 秒）" % age)
    if runs is None:
        log("在跑实例查询失败，仍尝试 dispatch（互斥机制防堆积）")
    else:
        for rid in runs:
            cancel_run(rid)
        if runs and not DRY_RUN:
            time.sleep(5)  # 等取消生效，避免新实例被互斥秒退
    dispatch(WORKFLOW_REF)


def check_notify_chain():
    """通知链巡检：状态 bin 过期且无 live-notify 实例在跑→dispatch 重启。"""
    age = bin_age(NOTIFY_BIN_URL)
    if age is not None:
        log("通知状态 bin updated 距今 %d 秒（阈值 %d 秒）" % (age, NOTIFY_STALE_LIMIT))
    if not TOKEN:
        log("未提供 GITHUB_TOKEN，通知链仅检查不能自愈")
        return
    if age is not None and age <= NOTIFY_STALE_LIMIT:
        log("通知链健康，无需处理")
        return
    runs = active_runs(NOTIFY_WORKFLOW_NAME)
    if runs:
        log("live-notify 有实例在跑（%d 个），不重复 dispatch" % len(runs))
        return
    if age is None and runs is None:
        log("通知状态 bin 读不出且实例查询失败，保守起见不动")
        return
    log("通知链确认断链，dispatch live-notify.yml 重启")
    dispatch(NOTIFY_WORKFLOW_REF)


def check_self_mutex():
    """看门狗自身互斥：其他实例在跑时秒退（cron 后备不叠加）。"""
    if not TOKEN:
        return 0
    try:
        d = gh_api("GET", "/actions/runs?status=in_progress&per_page=50")
        return sum(1 for r in d.get("workflow_runs", [])
                   if str(r.get("id")) != str(RUN_ID) and r.get("name") == SELF_WORKFLOW_NAME)
    except Exception as e:
        log("看门狗互斥检查失败（按无冲突继续）: %s" % str(e)[:80])
        return 0


def main():
    if check_self_mutex() > 0:
        log("已有看门狗实例在跑，本次触发秒退（后备不叠加）")
        return 0
    deadline = time.time() + WATCH_RUN_MINUTES * 60
    round_no = 0
    while True:
        round_no += 1
        log("===== 巡检第 %d 轮 =====" % round_no)
        try:
            check_feed_chain()
        except Exception as e:
            log("供数链巡检异常（下轮继续）: %s" % str(e)[:100])
        try:
            check_notify_chain()
        except Exception as e:
            log("通知链巡检异常（下轮继续）: %s" % str(e)[:100])
        remain = deadline - time.time()
        if remain <= 0:
            break
        time.sleep(min(float(WATCH_INTERVAL), max(1.0, remain)))
    relay_self()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
