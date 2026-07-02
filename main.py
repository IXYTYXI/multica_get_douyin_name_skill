"""CLI for the Douyin author-homepage scraper (single purpose).

Only scrapes an author's homepage: profile (粉丝量) + selected posts + comments,
into a new 5-table Feishu bitable. Keyword search is NOT part of this tool — use
the separate douyin-scraper skill for that.
"""
import asyncio
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import click

from storage.feishu import FeishuBitable
from config.settings import DOUYIN_COOKIE, CDP_ENDPOINT


def _parse_date(value: str) -> int:
    """Parse a date string (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS) to Unix timestamp."""
    if not value:
        return 0
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return int(datetime.strptime(value, fmt).timestamp())
        except ValueError:
            continue
    raise click.BadParameter(f"日期格式不对: {value}（请用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS）")


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
# search — find author homepage URLs by name                                    #
# --------------------------------------------------------------------------- #
@cli.command(name="search")
@click.argument("names", nargs=-1, required=True)
def search_cmd(names):
    """按作者昵称搜索抖音主页 URL。

    用法：python main.py search "清华凌霄学习舅" "于泽老师的思维课"
    """
    if not DOUYIN_COOKIE:
        click.echo("需要 DOUYIN_COOKIE 才能调用搜索接口。在 .env 配置或运行 python main.py login")
        return
    asyncio.run(_search_users(list(names)))


async def _search_users(names):
    from core.client import DouyinClient
    from config.settings import DOUYIN_API_BASE as api_base
    async with DouyinClient(cookies=DOUYIN_COOKIE) as client:
        for name in names:
            params = {
                'device_platform': 'webapp', 'aid': '6383',
                'keyword': name, 'search_channel': 'aweme_user_web',
                'search_source': 'normal_search', 'query_correct_type': '1',
                'is_filter_search': '0', 'offset': '0', 'count': '5',
                'cookie_enabled': 'true', 'platform': 'PC',
            }
            data = await client.get(f'{api_base}/general/search/single/', params)
            if data.get('status_code') != 0:
                click.echo(f'[{name}] search failed (status={data.get("status_code")})')
                continue
            found = False
            for item in (data.get('data') or []):
                for u in (item.get('user_list') or []):
                    info = u.get('user_info') or {}
                    nick = info.get('nickname', '')
                    sec_uid = info.get('sec_uid', '')
                    fans = info.get('follower_count', 0)
                    if sec_uid and not found:
                        url = f'https://www.douyin.com/user/{sec_uid}'
                        click.echo(f'{nick} | {fans} fans | {url}')
                        found = True
            if not found:
                click.echo(f'[{name}] no results')
            await asyncio.sleep(2)


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
@click.option("--headless", is_flag=True, help="使用无头浏览器（默认带界面，降低被识别封号风险）")
@click.option("--api-comments", is_flag=True, help="用 API 模式抓评论（默认模拟点击含二级评论，此参数切换为纯 API）")
@click.option("--structure-only", is_flag=True, help="只新建 5 张表结构，不抓数据")
@click.option("--cdp", "cdp_endpoint", default="",
              help="连接已登录 Chrome 的 CDP 端点（如 http://localhost:9222），免 Cookie")
@click.option("--date-from", default="", help="只保留该日期之后的作品（YYYY-MM-DD）")
@click.option("--date-to", default="", help="只保留该日期之前的作品（YYYY-MM-DD）")
def scrape_author(url, folder, name, recent_count, skip_top,
                  no_comments, headless, api_comments, structure_only, cdp_endpoint,
                  date_from, date_to):
    """从作者主页链接抓取：作者信息(粉丝量) + 选定作品(封面/视频/图片/点赞评论收藏) + 评论，写入 5 表多维表格。

    默认跳过置顶视频后取最近 5 条（检测到 is_top 则丢弃置顶，否则回退为跳过前 --skip-top 条，
    即第4-8条）。新建 5 张表：作者信息 / 视频作品 / 图文作品 / 一级评论 / 二级评论。

    使用 --cdp 连接已登录的 Chrome 浏览器（需先以 --remote-debugging-port 启动 Chrome），
    直接复用浏览器登录态，无需导出 Cookie。
    """
    cdp = cdp_endpoint or CDP_ENDPOINT
    if not DOUYIN_COOKIE and not cdp and not structure_only:
        click.echo("⚠️ 未检测到 DOUYIN_COOKIE 或 CDP 端点。")
        click.echo("方式一: 在 .env 配置 DOUYIN_COOKIE 或运行 `python main.py login`")
        click.echo("方式二: 启动 Chrome 时带 --remote-debugging-port=9222，登录抖音后用 --cdp http://localhost:9222")
        click.echo("如果只需创建表结构，请加 --structure-only。")
        return
    asyncio.run(_scrape_author(
        url, _folder_token(folder), name, recent_count, skip_top,
        no_comments, headless, api_comments, structure_only, cdp,
        _parse_date(date_from), _parse_date(date_to),
    ))


async def _scrape_author(url, folder_token, name, recent_count, skip_top,
                         no_comments, headless, api_comments, structure_only, cdp_endpoint="",
                         date_from=0, date_to=0):
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
        pipeline.DATE_FROM = date_from
        pipeline.DATE_TO = date_to
        if cdp_endpoint:
            pipeline.USE_CDP = cdp_endpoint
        if headless:
            pipeline.HEADLESS = True
        if api_comments:
            pipeline.USE_UI_COMMENTS = False
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


# --------------------------------------------------------------------------- #
# scrape-batch — batch mode for multiple authors                                #
# --------------------------------------------------------------------------- #
@cli.command(name="scrape-batch")
@click.argument("authors", nargs=-1, required=True)
@click.option("--folder", help="飞书文件夹 token 或 URL（新建多维表格的位置；留空建在应用空间）")
@click.option("--name", default="抖音作者数据-批量", help="新建多维表格的名称")
@click.option("--recent-count", type=int, default=5, help="保留的作品数量（默认最近 5 条）")
@click.option("--skip-top", type=int, default=3,
              help="检测不到置顶标记(is_top)时回退跳过的前 N 条置顶视频（默认 3，即取第4-8条）")
@click.option("--no-comments", is_flag=True, help="只抓作者信息+作品，跳过一/二级评论")
@click.option("--headless", is_flag=True, help="使用无头浏览器（默认带界面，降低被识别封号风险）")
@click.option("--api-comments", is_flag=True, help="用 API 模式抓评论（默认模拟点击含二级评论，此参数切换为纯 API）")
@click.option("--cdp", "cdp_endpoint", default="",
              help="连接已登录 Chrome 的 CDP 端点（如 http://localhost:9222），免 Cookie")
@click.option("--date-from", default="", help="只保留该日期之后的作品（YYYY-MM-DD）")
@click.option("--date-to", default="", help="只保留该日期之前的作品（YYYY-MM-DD）")
def scrape_batch(authors, folder, name, recent_count, skip_top,
                 no_comments, headless, api_comments, cdp_endpoint,
                 date_from, date_to):
    """批量采集多个作者主页，所有数据写入同一张多维表格。

    参数可以是作者主页 URL 或者作者昵称（自动搜索），也可以混合使用：

      python main.py scrape-batch "清华凌霄学习舅" "于泽老师的思维课" --folder <飞书文件夹>
      python main.py scrape-batch URL1 URL2 "作者昵称" --cdp http://localhost:9222
    """
    if not authors:
        click.echo("请至少提供一个作者主页 URL 或昵称。")
        return
    cdp = cdp_endpoint or CDP_ENDPOINT
    if not DOUYIN_COOKIE and not cdp:
        click.echo("⚠️ 未检测到 DOUYIN_COOKIE 或 CDP 端点。")
        click.echo("方式一: 在 .env 配置 DOUYIN_COOKIE 或运行 `python main.py login`")
        click.echo("方式二: 启动 Chrome 时带 --remote-debugging-port=9222，登录抖音后用 --cdp http://localhost:9222")
        return
    asyncio.run(_scrape_batch(
        list(authors), _folder_token(folder), name, recent_count, skip_top,
        no_comments, headless, api_comments, cdp,
        _parse_date(date_from), _parse_date(date_to),
    ))


async def _resolve_authors(authors):
    """Turn a mixed list of URLs and author names into a list of homepage URLs."""
    from core.client import DouyinClient
    from config.settings import DOUYIN_API_BASE as api_base
    urls = []
    names_to_search = []
    for a in authors:
        if 'douyin.com/user/' in a or a.startswith('http'):
            urls.append(a)
        else:
            names_to_search.append(a)
    if names_to_search:
        click.echo(f"搜索 {len(names_to_search)} 个作者昵称...")
        import asyncio as _aio
        async with DouyinClient(cookies=DOUYIN_COOKIE) as client:
            for n in names_to_search:
                params = {
                    'device_platform': 'webapp', 'aid': '6383',
                    'keyword': n, 'search_channel': 'aweme_user_web',
                    'search_source': 'normal_search', 'query_correct_type': '1',
                    'is_filter_search': '0', 'offset': '0', 'count': '5',
                    'cookie_enabled': 'true', 'platform': 'PC',
                }
                data = await client.get(f'{api_base}/general/search/single/', params)
                found = False
                if data.get('status_code') == 0:
                    for item in (data.get('data') or []):
                        for u in (item.get('user_list') or []):
                            info = u.get('user_info') or {}
                            sec_uid = info.get('sec_uid', '')
                            nick = info.get('nickname', '')
                            fans = info.get('follower_count', 0)
                            if sec_uid and not found:
                                url = f'https://www.douyin.com/user/{sec_uid}'
                                click.echo(f"  {nick} | {fans} fans -> {url}")
                                urls.append(url)
                                found = True
                if not found:
                    click.echo(f"  [!] 未找到「{n}」，跳过")
                await _aio.sleep(2)
    return urls


async def _scrape_batch(authors, folder_token, name, recent_count, skip_top,
                        no_comments, headless, api_comments, cdp_endpoint="",
                        date_from=0, date_to=0):
    urls = await _resolve_authors(authors)
    if not urls:
        click.echo("没有可处理的作者 URL，退出。")
        return
    click.echo(f"批量模式：{len(urls)} 个作者")

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

    import pipeline
    pipeline.APP_TOKEN = ids["app_token"]
    pipeline.AUTHOR_TABLE_ID = ids["author_table_id"]
    pipeline.VIDEO_TABLE_ID = ids["video_table_id"]
    pipeline.IMAGE_TABLE_ID = ids["image_table_id"]
    pipeline.COMMENT_L1_TABLE_ID = ids["comment_l1_table_id"]
    pipeline.COMMENT_L2_TABLE_ID = ids["comment_l2_table_id"]
    pipeline.AUTHOR_RECENT_COUNT = recent_count
    pipeline.AUTHOR_TOP_SKIP = skip_top
    pipeline.SKIP_COMMENTS = no_comments
    pipeline.DATE_FROM = date_from
    pipeline.DATE_TO = date_to
    if cdp_endpoint:
        pipeline.USE_CDP = cdp_endpoint
    if headless:
        pipeline.HEADLESS = True
    if api_comments:
        pipeline.USE_UI_COMMENTS = False
    await pipeline.main_batch(urls)

    click.echo(f"\n=== 批量多维表格已就绪 ===")
    click.echo(f"链接      : {ids['url']}")
    click.echo(f"app_token : {ids['app_token']}")
    click.echo(f"共 {len(urls)} 个作者的数据写入上述表格。")


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
