---
name: douyin-author-scraper
description: "Use when the user wants to scrape Douyin AUTHOR homepages — by name or URL. Supports single or batch mode: collects follower count (粉丝量), a selected slice of posts (skipping pinned videos) with like/comment/collect counts, image/video files, and comments (L1+L2), writing everything to a Feishu bitable. Can search authors by nickname instead of requiring URLs. This is author-page scraping ONLY; for keyword search use the separate douyin-scraper skill."
user-invocable: true
---

# Douyin Author Homepage Scraper

Scrape one or more Douyin author homepages — **by name or URL**. Given an author,

1. **粉丝量 (follower count)** and the rest of the author profile, and
2. a selected slice of the author's posts — **skipping pinned videos** — with
   their 点赞/评论/收藏 counts, the real **image/video files**, and each post's
   **comments (L1 + L2)** —

and write everything to a **5-table Feishu bitable**. Keyword search is NOT part
of this skill; that lives in the separate `douyin-scraper` skill.

## What gets written (5 tables)

| 表 | 内容 |
|---|---|
| **作者信息** | the author profile: 用户ID / 昵称 / 简介 / **粉丝数** / 关注数 / 获赞数 / 作品数 / 主页链接 / 爬取时间 |
| **视频作品** | selected video posts — 点赞/评论/收藏/分享 + **作品封面 & 作品视频(.mp4) as real attachments** |
| **图文作品** | selected image/note posts — **作品封面 & 作品图片(all images) as attachments** |
| **一级评论** | first-level comments of the selected posts |
| **二级评论** | second-level (reply) comments — carries 父评论ID / 回复对象 / 所属一级评论作者 |

Rules (kept identical to the douyin data-model agreement):
- Media are uploaded as Feishu **attachments** (type 17), not URLs.
- Link fields (`作品链接` / `作者主页` / `主页链接`) show the **raw URL** (type 15), built with `storage.feishu.url_field(url)`.
- Every record carries a `来源` field = the author homepage URL, so multiple authors can share one bitable.

## Which posts (avoiding pinned videos)

Pinned videos sit at the top of a homepage and carry the API's `is_top` flag.
The selector **drops detected pinned posts and keeps the next 5**. If the API
does not return `is_top`, it falls back to **skipping the first 3** as pinned —
i.e. the **4th–8th** posts. Both are tunable (`--recent-count`, `--skip-top`).

## Date range filtering

Use `--date-from` and/or `--date-to` to only keep posts published within a time
window. Format: `YYYY-MM-DD` or `YYYY-MM-DD HH:MM:SS`. The filter runs after
pinned-post removal but before the `--recent-count` cap, so you can combine them:

```bash
# Only posts from June 2026
python main.py scrape-batch 作者A --date-from 2026-06-01 --date-to 2026-06-30

# Posts from the last week, keep up to 10
python main.py scrape-batch 作者A --date-from 2026-06-25 --recent-count 10
```

## Run it

### 按名字搜索作者

不需要知道主页 URL，直接用昵称搜索：

```bash
# 搜索作者，返回主页链接和粉丝数
python main.py search "清华凌霄学习舅"
```

### 批量模式（推荐 — 支持名字和 URL 混合）

传入作者昵称或主页 URL，自动建一张多维表格，所有作者的数据写入同一张表：

```bash
# 直接用昵称批量采集（自动搜索 → 找到主页 → 采集）
python main.py scrape-batch 清华凌霄学习舅 于泽老师的思维课 成祥老师带你重学英语

# 昵称和 URL 混合
python main.py scrape-batch 清华凌霄学习舅 "https://www.douyin.com/user/MS4wLjABAAAA..."

# 指定飞书文件夹 + CDP 模式
python main.py scrape-batch 作者A 作者B --folder <飞书文件夹> --cdp http://localhost:9222

# 跳过评论 / 自定义取第4-8条
python main.py scrape-batch 作者A 作者B --no-comments --skip-top 3 --recent-count 5
```

所有参数（`--skip-top`、`--recent-count`、`--no-comments`、`--cdp`、`--date-from`、`--date-to` 等）与单作者模式相同。

### CDP 模式（推荐 — 连接已登录的 Chrome）

不导出 Cookie，直接复用浏览器登录态：

```bash
# 1. 启动 Chrome 时开启远程调试端口
chrome --remote-debugging-port=9222

# 2. 在 Chrome 里登录抖音（完成滑块/验证码）

# 3. 用 --cdp 连接该浏览器直接跑
python main.py scrape-author "https://www.douyin.com/user/MS4wLjABAAAA..." --folder <folder> --cdp http://localhost:9222
```

也可以在 `.env` 配 `CDP_ENDPOINT=http://localhost:9222`，省略 `--cdp` 参数。

### 单作者模式（Cookie 方式）

```bash
# One shot: create a NEW 5-table bitable AND scrape the author (profile + posts + comments)
python main.py scrape-author "https://www.douyin.com/user/MS4wLjABAAAA..." --folder <folder_token_or_url>

# Keep 8 non-pinned posts; skip 3 pinned on the fallback path
python main.py scrape-author "<homepage_url>" --folder <folder> --recent-count 8 --skip-top 3

# Only build the 5 empty tables / skip comments / force headless / capture L2 replies
python main.py scrape-author "<url>" --folder <folder> --structure-only
python main.py scrape-author "<url>" --folder <folder> --no-comments
python main.py scrape-author "<url>" --folder <folder> --headless      # 默认带界面，加此参数强制无头
python main.py scrape-author "<url>" --folder <folder> --ui-comments   # incl. 二级评论
```

Omit `--folder` to create the bitable in the app's own space. Creating inside a
folder needs the self-built app to be a collaborator with edit rights on it
(`drive:drive` + `bitable:app`), otherwise Feishu returns `DriveNodePermNotAllow`.

**Reuse an existing bitable:** set `FEISHU_APP_TOKEN` + `AUTHOR_TABLE_ID` +
`VIDEO_TABLE_ID` / `IMAGE_TABLE_ID` / `COMMENT_L1_TABLE_ID` / `COMMENT_L2_TABLE_ID`
and `DOUYIN_AUTHOR_URL` in `.env`, then run `python pipeline.py`.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
cp .env.example .env      # fill FEISHU_* and DOUYIN_COOKIE, or run: python main.py login
```

| Variable | Required | Notes |
|---|---|---|
| `DOUYIN_COOKIE` | 二选一 | login cookie; or run `python main.py login` to capture it |
| `CDP_ENDPOINT` | 二选一 | 已登录 Chrome 的 CDP 端点，如 `http://localhost:9222`（推荐，免 Cookie） |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | yes | self-built Feishu app |
| `FEISHU_APP_TOKEN` | reuse-only | set when reusing an existing bitable |
| `AUTHOR_TABLE_ID` / `VIDEO_TABLE_ID` / `IMAGE_TABLE_ID` / `COMMENT_L1_TABLE_ID` / `COMMENT_L2_TABLE_ID` | reuse-only | when reusing an existing bitable |
| `AUTHOR_RECENT_COUNT` / `AUTHOR_TOP_SKIP` | no | default 5 / 3 |

**认证方式:**
- **CDP（推荐）**: 启动 Chrome 时加 `--remote-debugging-port=9222`，在 Chrome 里登录抖音，
  然后用 `--cdp http://localhost:9222` 或在 `.env` 设 `CDP_ENDPOINT`。
  所有 API 调用直接走浏览器 fetch()，不导出 Cookie，不容易被封。
- **Cookie**: 传统方式，在 `.env` 设 `DOUYIN_COOKIE` 或 `python main.py login`。

**Security:** never log, print, or paste the cookie into comments. If `.env` is
missing, configure it on the runtime directly.

## How fan count is obtained

Read from `/user/profile/other/` over plain HTTP first; if that endpoint is
signature-blocked, it retries from a browser context, then falls back to the
`follower_count` embedded in one of the author's posts.

## Notes & limits

- Play count is always 0 from the Web API (Douyin blocks it for third parties).
- Reply (L2) comments come from the UI-click scraper (`--ui-comments`); the raw
  reply API is `bd-ticket-guard`'d and returns empty to hand-built requests.
- Browser defaults to headed (visible) to reduce anti-bot detection risk; pass `--headless` only if needed.
- Cookies expire after ~60 days — re-run `python main.py login`.
