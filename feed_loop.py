# -*- coding: utf-8 -*-
"""自续跑循环（无人值守供数主循环，GitHub Actions 版）。
单次运行内每 INTERVAL 秒同步一轮地址簿，跑满 ITERATIONS 轮后接力触发下一次运行
（workflow_dispatch 不受 cron 延迟影响）；cron 每 10 分钟的触发仅作断链后备——
已有实例在跑时秒退，防止并发堆积。"""
from __future__ import annotations  # 兼容 Python 3.8/3.9

import json
import os
import subprocess
import sys
import time
import urllib.request

ITERATIONS = int(os.environ.get("LOOP_ITERATIONS", "25"))  # 25 轮 × 约 11 分钟 ≈ 4.5 小时（job 上限 6 小时）
INTERVAL = int(os.environ.get("LOOP_INTERVAL", "600"))     # 每轮间隔 10 分钟
SYNC_TIMEOUT = 480                                          # 单轮同步硬超时（22 间极端全卡 20s = 440s）
REPO = os.environ.get("GITHUB_REPOSITORY", "")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
WORKFLOW_NAME = os.environ.get("GITHUB_WORKFLOW", "")
WORKFLOW_REF = os.environ.get("WORKFLOW_FILE", "feed.yml")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
BASE = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


def api(method, path, body=None):
    req = urllib.request.Request(
        "https://api.github.com/repos/%s%s" % (REPO, path),
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "User-Agent": "feed-loop"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}


def other_active():
    """同名工作流的其他在跑实例数（互斥依据）"""
    try:
        d = api("GET", "/actions/runs?status=in_progress&per_page=50")
        return sum(1 for r in d.get("workflow_runs", [])
                   if str(r.get("id")) != str(RUN_ID) and r.get("name") == WORKFLOW_NAME)
    except Exception as e:
        log("互斥检查失败（按无冲突继续）: %s" % str(e)[:80])
        return 0


def main():
    if other_active() > 0:
        log("已有实例在跑，本次触发秒退（断链后备不叠加）")
        return 0
    for i in range(1, ITERATIONS + 1):
        log("===== 第 %d/%d 轮 =====" % (i, ITERATIONS))
        try:
            subprocess.run([sys.executable, "douyin_feed_sync.py"], cwd=BASE, timeout=SYNC_TIMEOUT)
        except Exception as e:
            log("本轮同步异常（下轮继续）: %s" % str(e)[:80])
        if i < ITERATIONS:
            time.sleep(INTERVAL)
            if other_active() > 0:
                log("检测到并发实例，主动退出让位（链路自我收敛）")
                return 0
    # 接力触发下一次运行（workflow_dispatch 不受 cron 延迟/限流影响）
    try:
        api("POST", "/actions/workflows/%s/dispatches" % WORKFLOW_REF, {"ref": "main"})
        log("已接力触发下一次运行")
    except Exception as e:
        log("接力触发失败（cron 后备将在 10 分钟内重启链路）: %s" % str(e)[:80])
    return 0


if __name__ == "__main__":
    sys.exit(main())
