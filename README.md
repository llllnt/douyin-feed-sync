# 抖音监播 · 云端地址簿同步（GitHub Actions 版）

这个仓库只做一件事：**每 10 分钟自动解析一遍直播间状态与拉流地址，推送到云端地址簿**。
监播墙网页读取地址簿直连抖音 CDN 播放——从此不再需要 Mac 小助手开机运行。

- 运行环境：GitHub Actions（GitHub 官方免费服务器，公开仓库不限时）
- 依赖：Python 标准库，零安装
- 单次耗时：约 1~2 分钟（22 间）

---

## 部署步骤（约 10 分钟，只需做一次）

### 第 1 步：注册 / 登录 GitHub

打开 <https://github.com>，没有账号就注册一个（免费）。

### 第 2 步：创建仓库

1. 右上角 `+` → **New repository**
2. Repository name 随便起，例如 `douyin-feed`
3. 选择 **Public**（公开仓库 Actions 免费不限时；私有仓库每月只有 2000 分钟免费额度，不够用）
4. 其他都不用勾选，点 **Create repository**

### 第 3 步：上传 2 个文件

在仓库页面点 **Add file → Create new file**，分两次创建：

**文件 1**：文件名输入框里直接输入 `douyin_feed_sync.py`，把同名文件的全部内容粘贴进去，页面底部点 **Commit changes**。

**文件 2**：再点 **Add file → Create new file**，文件名输入框输入 `.github/workflows/feed.yml`（GitHub 会自动按 `/` 拆成目录），把同名文件内容粘贴进去，**Commit changes**。

> 创建完成后，仓库里应该是这样的结构：
> ```
> douyin_feed_sync.py
> .github/
>   workflows/
>     feed.yml
> ```

### 第 4 步：配置 2 个密钥（Secrets）

仓库页面顶部 → **Settings** → 左侧 **Secrets and variables** → **Actions** → **New repository secret**，添加两条：

| Name | Value |
|---|---|
| `CHAT_BIN_URL` | `（找助手索取，勿公开）` |
| `FEED_BIN_URL` | `（找助手索取，勿公开）` |

（这两个地址是云端房间清单和地址簿，不要发到公开的地方。）

### 第 5 步：手动跑一次验证

1. 仓库顶部点 **Actions** 标签（第一次进入如果提示，点绿色按钮启用 workflows）
2. 左侧点 **douyin-feed-sync** → 右侧 **Run workflow** → 绿色 **Run workflow** 按钮
3. 刷新页面，等 1~2 分钟出现 ✅ 绿色勾
4. 点进这次运行 → 点 `sync` 任务 → 展开「解析直播间并推送地址簿」，日志最后应显示：
   ```
   完成：xx/22 在播，地址簿已推送（22 间，全员 2 分钟内可见）
   ```

看到绿色勾 + 这行日志 = 部署成功 🎉 之后每 10 分钟自动运行，无需任何操作。

---

## 日常使用

- **全自动**：每 10 分钟解析一次，监播墙全员最多约 12 分钟内看到最新状态。
- **想立刻刷新**：Actions → douyin-feed-sync → Run workflow，1~2 分钟生效。
- **加/删直播间**：照旧在对话里发链接给助手，无需动这个仓库。

## 常见问题

**Q：Actions 页面显示红色 ❌ 了怎么办？**
点进去看日志。偶发的网络抖动（某间解析失败）不影响整体；如果日志显示「地址簿推送失败 404」，说明云端存储 bin 被清了，联系助手重建即可。

**Q：定时任务好像不准时？**
GitHub 定时任务高峰期可能延迟几分钟，属正常现象，不影响使用。

**Q：很久以后收到 GitHub 邮件说 "scheduled workflows disabled"？**
GitHub 规定：仓库 60 天没有任何代码变动会自动暂停定时任务。收到邮件后，打开仓库 Actions 页面点一下 **Enable workflow** 即可恢复（约 10 秒）。

**Q：Mac 小助手可以关了吗？**
云端方案稳定运行几天后可以关掉：Mac 上终端执行
```bash
launchctl unload ~/Library/LaunchAgents/com.leqeegroup.douyinmonitor.plist
```
之后 Mac 关机/休眠都不影响监播墙。（保留着不删也无妨，哪天想恢复再 `launchctl load` 回来。）
