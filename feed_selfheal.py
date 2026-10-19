# -*- coding: utf-8 -*-
"""断链自愈看门狗（GitHub Actions 版）。

检查监播云端地址簿 bin（extendsclass afcbbaf）顶层 updated 时间戳：
- 新鲜（<= STALE_LIMIT 秒）：链路健康，直接退出；
- 确认过期：取消可能卡死的同名工作流在跑实例，再 dispatch 一次 feed.yml 重启解析链；
- bin 读取失败：没有在跑实例时 dispatch 兜底；有实例在跑时仅记录（可能是 extendsclass
  自身故障，避免误杀健康实例）。

配合 feed_loop.py 的互斥机制（已有实例在跑时新实例秒退），不会并发堆积。
由 .github/workflows/watchdog.yml 每 10 分钟调度（与 feed.yml 的 */10 错开 7 分钟）。
"""
from __future__ import annotations  # 兼容 Python 3.8/3.9

import json
import os
import time
import urllib.request

BIN_URL = os.environ.get(
    "BIN_URL", "https://extendsclass.com/api/json-storage/bin/afcbbaf")
# 健康链路最坏写入间隔 ≈ 每轮间隔 600s + 单轮硬超时 480s = 18 分钟；25 分钟阈值留出余量
STALE_LIMIT = int(os.environ.get("STALE_LIMIT", "1500"))
REPO = os.environ.get("GITHUB_REPOSITORY", "")
WORKFLOW_NAME = os.environ.get("WATCH_WORKFLOW_NAME", "douyin-feed-sync")
WORKFLOW_REF = os.environ.get("WORKFLOW_FILE", "feed.yml")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def bin_age():
    """地址簿距今天数（秒）；读取失败或字段缺失返回 None。"""
    try:
        req = urllib.request.Request(
            BIN_URL + "?_t=%d" % int(time.time() * 1000),
            headers={"User-Agent": "feed-selfheal"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        updated = data.get("updated")
        if not updated:
            log("bin 缺少 updated 字段，无法判断新鲜度")
            return None
        return int(time.time()) - int(updated)
    except Exception as e:
        log("bin 读取失败: %s" % str(e)[:120])
        return None


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


def active_runs():
    """同名工作流的在跑实例 id 列表；查询失败返回 None（无法判断）。"""
    try:
        d = gh_api("GET", "/actions/runs?status=in_progress&per_page=50")
        return [r["id"] for r in d.get("workflow_runs", [])
                if r.get("name") == WORKFLOW_NAME]
    except Exception as e:
        log("查询在跑实例失败: %s" % str(e)[:120])
        return None


def cancel_run(run_id):
    log("取消卡死实例 %s" % run_id)
    if DRY_RUN:
        log("[DRY_RUN] 跳过实际取消")
        return
    gh_api("POST", "/actions/runs/%s/cancel" % run_id)


def dispatch():
    log("dispatch 重启解析链（%s）" % WORKFLOW_REF)
    if DRY_RUN:
        log("[DRY_RUN] 跳过实际 dispatch")
        return True
    gh_api("POST", "/actions/workflows/%s/dispatches" % WORKFLOW_REF, {"ref": "main"})
    return True


def main():
    age = bin_age()
    if age is not None:
        log("地址簿 updated 距今 %d 秒（阈值 %d 秒）" % (age, STALE_LIMIT))

    if not TOKEN:
        log("未提供 GITHUB_TOKEN，仅能检查不能自愈")
        return 0 if (age is not None and age <= STALE_LIMIT) else 1

    if age is not None and age <= STALE_LIMIT:
        log("链路健康，无需处理")
        return 0

    runs = active_runs()
    if age is None:
        # bin 读不出：可能是 extendsclass 故障，也可能是链路断了
        if runs is None:
            log("无法判断在跑实例，保守起见不动")
            return 0
        if runs:
            log("有实例在跑（%d 个），疑似 extendsclass 故障，不动" % len(runs))
            return 0
        log("无实例在跑，dispatch 兜底重启")
        return 0 if dispatch() else 1

    # 确认断链/卡死
    log("确认链路异常（过期 %d 秒）" % age)
    if runs is None:
        log("在跑实例查询失败，仍尝试 dispatch（互斥机制防堆积）")
    else:
        for rid in runs:
            cancel_run(rid)
        if runs and not DRY_RUN:
            time.sleep(5)  # 等取消生效，避免新实例被互斥秒退
    return 0 if dispatch() else 1


if __name__ == "__main__":
    raise SystemExit(main())
