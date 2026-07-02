"""CLI for the Douyin author-homepage scraper (single purpose).

Only scrapes an author's homepage: profile (粉丝量) + selected posts + comments,
into a new 5-table Feishu bitable. Keyword search is NOT part of this tool — use
the separate douyin-scraper skill for that.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import click

from storage.feishu import FeishuBitable
from config.settings import DOUYIN_COOKIE


def _folder_token(value: str) -> str:
    """Accept a raw folder token or a full Feishu folder URL, return the token."""
    if not value:
        return ""
    value = value.strip().rstrip("/")
    if "/folder/" in value:
        value = value.split("/folder/")[-1]
    return value.split("?")[0]


@click.group()
def cli():
    """抖音作者主页数据爬取工具 —— 粉丝量 + 选定作品(避开置顶) + 评论 → 飞书多维表格"""
    pass


# --------------------------------------------------------------------------- #
# login — capture a fresh cookie into .env / cookies.json                       #
# --------------------------------------------------------------------------- #
def _save_cookie_to_env(cookie_str: str):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    out, done = [], False
    for ln in lines:
        if ln.startswith("DOUYIN_COOKIE="):
            out.append("DOUYIN_COOKIE=" + cookie_str)
            done = True
        else:
            out.append(ln)
    if not done:
        out.append("DOUYIN_COOKIE=" + cookie_str)
    with open(env_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")


@cli.command()
@click.option("--timeout", default=480, help="等待登录的最长秒数（默认 480=8分钟）")
def login(timeout):
    """打开可见浏览器登录抖音，自动检测登录并保存 Cookie 到 .env / cookies.json。"""
    asyncio.run(_login(timeout))


async def _login(timeout=480):
    import time as _t
    from playwright.async_api import async_playwright
    from core.browser import STEALTH_JS, COOKIE_FILE
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    pw = await async_playwright().start()
    br = await pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    ctx = await br.new_context(
        viewport={"width": 1440, "height": 900},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
        locale="zh-CN", timezone_id="Asia/Shanghai",
    )
    await ctx.add_init_script(STEALTH_JS)
    page = await ctx.new_page()
    try:
        await page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=60000)
    except Exception:
        pass

    click.echo("浏览器已打开。请登录抖音（扫码或手机号），完成任何滑块/验证码。")
    click.echo(f"检测到登录后会自动保存 Cookie；最多等待 {timeout} 秒。")

    def cookie_str(cookies):
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    def is_logged_in(cookies):
        by = {c["name"]: c["value"] for c in cookies}
        has_session = len(by.get("sessionid_ss", "")) > 10 or len(by.get("sessionid", "")) > 10
        return has_session and ("sid_guard" in by or "uid_tt" in by)

    start = _t.time()
    logged_in = False
    last_str = ""
    while _t.time() - start < timeout:
        await asyncio.sleep(5)
        cookies = await ctx.cookies()
        last_str = cookie_str(cookies)
        if last_str:
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            _save_cookie_to_env(last_str)
        if is_logged_in(cookies):
            logged_in = True
            await asyncio.sleep(3)
            cookies = await ctx.cookies()
            last_str = cookie_str(cookies)
            with open(COOKIE_FILE, "w", encoding="utf-8") as f:
                json.dump(cookies, f, ensure_ascii=False, indent=2)
            _save_cookie_to_env(last_str)
            break

    await br.close()
    await pw.stop()
    if logged_in:
        click.echo(f"\n✅ 已保存 Cookie（{len(last_str)} 字符）到 .env 和 cookies.json。")
    else:
        click.echo("\n⚠️ 超时未检测到登录；已尽量保存，可重试。")


# --------------------------------------------------------------------------- #
# scrape-author — the one real command                                          #
# --------------------------------------------------------------------------- #
@cli.command(name="scrape-author")
@click.argument("url")
@click.option("--folder", help="飞书文件夹 token 或 URL（新建多维表格的位置；留空建在应用空间）")
@click.option("--name", default="抖音作者数据", help="新建多维表格的名称")
@click.option("--recent-count", type=int, default=5, help="保留的作品数量（默认最近 5 条）")
@click.option("--skip-top", type=int, default=3,
              help="检测不到置顶标记(is_top)时回退跳过的前 N 条置顶视频（默认 3，即取第4-8条）")
@click.option("--no-comments", is_flag=True, help="只抓作者信息+作品，跳过一/二级评论")
@click.option("--headed", is_flag=True, help="用可见的本地浏览器代替无头浏览器（无头总崩溃时用）")
@click.option("--ui-comments", is_flag=True, help="用模拟点击(真人登录态)抓评论，含二级评论(自动启用 headed)")
@click.option("--structure-only", is_flag=True, help="只新建 5 张表结构，不抓数据")
def scrape_author(url, folder, name, recent_count, skip_top,
                  no_comments, headed, ui_comments, structure_only):
    """从作者主页链接抓取：作者信息(粉丝量) + 选定作品(封面/视频/图片/点赞评论收藏) + 评论，写入 5 表多维表格。

    默认跳过置顶视频后取最近 5 条（检测到 is_top 则丢弃置顶，否则回退为跳过前 --skip-top 条，
    即第4-8条）。新建 5 张表：作者信息 / 视频作品 / 图文作品 / 一级评论 / 二级评论。
    """
    if not DOUYIN_COOKIE and not structure_only:
        click.echo("⚠️ 未检测到 DOUYIN_COOKIE，请先在 .env 配置或运行 `python main.py login`。")
        click.echo("如果只需创建表结构，请加 --structure-only。")
        return
    asyncio.run(_scrape_author(
        url, _folder_token(folder), name, recent_count, skip_top,
        no_comments, headed, ui_comments, structure_only,
    ))


async def _scrape_author(url, folder_token, name, recent_count, skip_top,
                         no_comments, headed, ui_comments, structure_only):
    # 1. Create the 5-table bitable (作者信息 + 视频作品 + 图文作品 + 一级评论 + 二级评论).
    feishu = FeishuBitable()
    try:
        ids = feishu.create_author_bitable(name, folder_token)
    except RuntimeError as e:
        msg = str(e)
        click.echo(f"新建多维表格失败: {msg}")
        if "DriveNodePermNotAllow" in msg or "1254701" in msg:
            click.echo("原因: 自建应用对该文件夹没有写入权限。")
            click.echo("请在飞书里把该文件夹共享给你的自建应用并授予「可编辑」，")
            click.echo("并确认应用已开通 drive:drive 与 bitable:app 权限且已发布。")
        feishu.close()
        return
    feishu.close()

    # 2. Run the author pipeline into those tables (unless structure-only).
    if not structure_only:
        import pipeline
        pipeline.APP_TOKEN = ids["app_token"]
        pipeline.AUTHOR_URL = url
        pipeline.AUTHOR_TABLE_ID = ids["author_table_id"]
        pipeline.VIDEO_TABLE_ID = ids["video_table_id"]
        pipeline.IMAGE_TABLE_ID = ids["image_table_id"]
        pipeline.COMMENT_L1_TABLE_ID = ids["comment_l1_table_id"]
        pipeline.COMMENT_L2_TABLE_ID = ids["comment_l2_table_id"]
        pipeline.AUTHOR_RECENT_COUNT = recent_count
        pipeline.AUTHOR_TOP_SKIP = skip_top
        pipeline.KEYWORD = url          # 来源 tag on every record
        pipeline.SKIP_COMMENTS = no_comments
        if headed or ui_comments:
            pipeline.HEADLESS = False
        if ui_comments:
            pipeline.USE_UI_COMMENTS = True
        await pipeline.main_author()

    click.echo("\n=== 作者多维表格已就绪 ===")
    click.echo(f"链接      : {ids['url']}")
    click.echo(f"app_token : {ids['app_token']}")
    click.echo(f"作者信息  : {ids['author_table_id']}")
    click.echo(f"视频作品  : {ids['video_table_id']}")
    click.echo(f"图文作品  : {ids['image_table_id']}")
    click.echo(f"一级评论  : {ids['comment_l1_table_id']}")
    click.echo(f"二级评论  : {ids['comment_l2_table_id']}")
    click.echo("复用方法  : 在 .env 设置 FEISHU_APP_TOKEN / AUTHOR_TABLE_ID / VIDEO_TABLE_ID / "
               "IMAGE_TABLE_ID / COMMENT_L1_TABLE_ID / COMMENT_L2_TABLE_ID + DOUYIN_AUTHOR_URL，再跑 python pipeline.py。")


@cli.command(name="setup-feishu")
@click.option("--type", "table_type",
              type=click.Choice(["author", "video", "image", "comment-l1", "comment-l2"]),
              required=True)
@click.option("--table-id", required=True, help="飞书表格 table_id")
def setup_feishu(table_type, table_id):
    """初始化某张飞书表的字段结构（一般用 scrape-author 自动建表即可）。"""
    feishu = FeishuBitable()
    setup_map = {
        "author": feishu.setup_author_table,
        "video": feishu.setup_video_table,
        "image": feishu.setup_image_table,
        "comment-l1": feishu.setup_comment_l1_table,
        "comment-l2": feishu.setup_comment_l2_table,
    }
    setup_map[table_type](table_id)
    click.echo(f"已初始化 {table_type} 表字段结构")
    feishu.close()


if __name__ == "__main__":
    cli()
