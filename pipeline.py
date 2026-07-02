"""Author-homepage pipeline: scrape ONE Douyin author's homepage — profile
(follower count / 粉丝量), a selected slice of posts (media + engagement) and
their comments — and write everything to a 5-table Feishu bitable.

Single purpose: author-page scraping only. Keyword search lives in the separate
douyin-scraper skill; this package deliberately does not include it.
"""
import sys
sys.stdout.reconfigure(errors='replace')
import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    _original_print(*args, **kwargs)

import asyncio
import os
import re
import time
from datetime import datetime
from playwright.async_api import async_playwright
from core.client import DouyinClient, BrowserClient
from core.throttle import fetch_json, polite_sleep
from config.settings import DOUYIN_COOKIE, DOUYIN_API_BASE, REQUEST_DELAY, EMPTY_RETRY, MAX_PAGES, CDP_ENDPOINT
from scrapers.user import UserScraper
from models.data import UserInfo
from storage.feishu import FeishuBitable, url_field, author_to_feishu_record
from storage.downloader import download_file, cleanup_downloads, DOWNLOAD_DIR

VIDEO_TABLE_ID = os.environ.get("VIDEO_TABLE_ID", "YOUR_VIDEO_TABLE_ID")
IMAGE_TABLE_ID = os.environ.get("IMAGE_TABLE_ID", "YOUR_IMAGE_TABLE_ID")
COMMENT_L1_TABLE_ID = os.environ.get("COMMENT_L1_TABLE_ID") or os.environ.get("COMMENT_TABLE_ID", "YOUR_COMMENT_L1_TABLE_ID")
COMMENT_L2_TABLE_ID = os.environ.get("COMMENT_L2_TABLE_ID", "YOUR_COMMENT_L2_TABLE_ID")
KEYWORD = os.environ.get("DOUYIN_AUTHOR_URL", "")

AUTHOR_URL = os.environ.get("DOUYIN_AUTHOR_URL", "")
AUTHOR_TABLE_ID = os.environ.get("AUTHOR_TABLE_ID") or os.environ.get("USER_TABLE_ID", "")
AUTHOR_TOP_SKIP = int(os.environ.get("AUTHOR_TOP_SKIP", "3"))
AUTHOR_RECENT_COUNT = int(os.environ.get("AUTHOR_RECENT_COUNT", "5"))

SKIP_COMMENTS = os.environ.get("SKIP_COMMENTS", "").lower() in ("1", "true", "yes")
APP_TOKEN = os.environ.get("FEISHU_APP_TOKEN", "")

DATE_FROM = 0
DATE_TO = 0

MAX_COMMENTS_PER_POST = int(os.environ.get("MAX_COMMENTS_PER_POST", "100"))
MAX_REPLIES_PER_L1 = int(os.environ.get("MAX_REPLIES_PER_L1", "20"))
MAX_REPLIES_PER_POST = int(os.environ.get("MAX_REPLIES_PER_POST", "60"))
SKIP_L2 = os.environ.get("SKIP_L2", "").lower() in ("1", "true", "yes")

HEADLESS = os.environ.get("DOUYIN_HEADLESS", "0").lower() in ("1", "true", "yes")
USE_UI_COMMENTS = os.environ.get("USE_UI_COMMENTS", "1").lower() not in ("0", "false", "no")
APPEND = os.environ.get("APPEND", "").lower() in ("1", "true", "yes")
L2_REPLY_TIMEOUT = int(os.environ.get("L2_REPLY_TIMEOUT", "8"))
L2_DRY_GIVEUP = int(os.environ.get("L2_DRY_GIVEUP", "3"))

# CDP endpoint — when set, connect to an already-logged-in Chrome instead of
# launching a new browser. main.py may override this from --cdp flag.
USE_CDP = os.environ.get("CDP_ENDPOINT", "") or CDP_ENDPOINT


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
"""


def parse_post(aweme):
    """Parse a post (video or image/note) from the API response."""
    stats = aweme.get('statistics') or {}
    author = aweme.get('author') or {}
    video = aweme.get('video') or {}
    images = aweme.get('images') or []
    create_ts = aweme.get('create_time', 0)
    create_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(create_ts)) if create_ts else ''
    aweme_id = aweme.get('aweme_id', '')

    has_images = len(images) > 0
    aweme_type = aweme.get('aweme_type', 0)
    media_type = aweme.get('media_type', -1)
    post_type = 'image' if has_images or aweme_type in (68, 150) or media_type == 2 else 'video'

    cover_url = ''
    if video:
        cover_obj = video.get('cover') or {}
        cover_list = cover_obj.get('url_list') or []
        cover_url = cover_list[0] if cover_list else ''

    video_url = ''
    play_addr = video.get('play_addr') or {}
    if play_addr:
        url_list = play_addr.get('url_list') or []
        video_url = url_list[0] if url_list else ''

    image_urls = []
    for img in images:
        if img:
            url_list = img.get('url_list') or []
            if url_list:
                image_urls.append(url_list[0])

    hashtags = []
    for tag in (aweme.get('text_extra') or []):
        if tag and tag.get('hashtag_name'):
            hashtags.append(tag['hashtag_name'])

    if post_type == 'image':
        post_url = f'https://www.douyin.com/note/{aweme_id}'
    else:
        post_url = f'https://www.douyin.com/video/{aweme_id}'

    sec_uid = author.get('sec_uid', '')

    return {
        'aweme_id': aweme_id,
        'type': post_type,
        'is_top': aweme.get('is_top', 0),
        'desc': aweme.get('desc', ''),
        'author_nickname': author.get('nickname', ''),
        'author_sec_uid': sec_uid,
        'digg_count': stats.get('digg_count', 0),
        'comment_count': stats.get('comment_count', 0),
        'collect_count': stats.get('collect_count', 0),
        'share_count': stats.get('share_count', 0),
        'create_time': create_str,
        'cover_url': cover_url,
        'video_url': video_url,
        'image_urls': image_urls,
        'post_url': post_url,
        'author_homepage': f'https://www.douyin.com/user/{sec_uid}' if sec_uid else '',
        'hashtags': ', '.join(hashtags),
    }


def download_and_upload_media(post, feishu):
    """Download media and upload to Feishu. Returns file tokens dict."""
    aweme_id = post['aweme_id']
    tokens = {'cover': '', 'video': '', 'images': []}
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if post['cover_url']:
        path = download_file(post['cover_url'], f'{aweme_id}_cover.jpg')
        if path:
            token = feishu.upload_file(path, 'bitable_image')
            if token:
                tokens['cover'] = token

    if post['type'] == 'video' and post['video_url']:
        path = download_file(post['video_url'], f'{aweme_id}_video.mp4', timeout=180)
        if path:
            token = feishu.upload_file(path, 'bitable_file')
            if token:
                tokens['video'] = token

    for i, url in enumerate(post['image_urls']):
        path = download_file(url, f'{aweme_id}_img_{i}.jpg')
        if path:
            token = feishu.upload_file(path, 'bitable_image')
            if token:
                tokens['images'].append(token)

    return tokens


def build_record(post, tokens):
    """Build a Feishu record from post data and uploaded file tokens."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    record = {
        '作者': post['author_nickname'],
        '作品正文': post['desc'],
        '作品链接': url_field(post['post_url']),
        '作者主页': url_field(post['author_homepage']),
        '点赞数': post['digg_count'],
        '评论数': post['comment_count'],
        '收藏数': post['collect_count'],
        '分享数': post['share_count'],
        '发布时间': post['create_time'],
        '话题标签': post['hashtags'],
        '来源': KEYWORD,
        '爬取时间': now,
    }

    if tokens['cover']:
        record['作品封面'] = [{'file_token': tokens['cover']}]
    if tokens['video']:
        record['作品视频'] = [{'file_token': tokens['video']}]
    if tokens['images']:
        record['作品图片'] = [{'file_token': t} for t in tokens['images']]

    return record


async def _browser_get_json(page, url, timeout=30):
    """Run a credentialed fetch inside the browser page and return parsed JSON."""
    try:
        return await asyncio.wait_for(page.evaluate(
            """async (url) => {
                try {
                    const resp = await fetch(url, {
                        headers: {'Accept': 'application/json', 'Referer': 'https://www.douyin.com/'},
                        credentials: 'include',
                    });
                    return await resp.json();
                } catch (e) { return {status_code: -1, error: e.message}; }
            }""", url
        ), timeout=timeout)
    except Exception:
        return {}


async def _fetch_replies(page, aweme_id, comment_id, parent_text_user, max_replies=50):
    """Fetch second-level (reply) comments for one first-level comment."""
    out = []
    cursor = 0
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    while len(out) < max_replies:
        url = (
            f'{DOUYIN_API_BASE}/comment/list/reply/'
            f'?item_id={aweme_id}&comment_id={comment_id}'
            f'&cursor={cursor}&count=20&item_type=0'
            f'&device_platform=webapp&aid=6383&cookie_enabled=true&platform=PC'
        )
        data = await _browser_get_json(page, url, timeout=L2_REPLY_TIMEOUT)
        if not data or data.get('status_code') != 0:
            break
        replies = data.get('comments') or []
        if not replies:
            break
        for r in replies:
            user = r.get('user', {})
            ct = r.get('create_time', 0)
            reply_to = ''
            rt = r.get('reply_to_username') or (r.get('reply_to_reply') or {}).get('user', {}).get('nickname', '')
            reply_to = rt or parent_text_user
            out.append({
                '评论ID': r.get('cid', ''),
                '评论内容': r.get('text', ''),
                '评论者昵称': user.get('nickname', ''),
                '评论者ID': user.get('uid', ''),
                '父评论ID': comment_id,
                '回复对象': reply_to,
                '所属一级评论作者': parent_text_user,
                '所属作品ID': aweme_id,
                '点赞数': r.get('digg_count', 0),
                '评论时间': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ct)) if ct else '',
                '来源': KEYWORD,
                '爬取时间': now,
            })
            if len(out) >= max_replies:
                break
        if not data.get('has_more', 0):
            break
        cursor = data.get('cursor', 0)
        await polite_sleep()
    return out


async def fetch_comments_ui(posts, page=None):
    """Fetch L1 + L2 comments by driving the page UI (simulated clicks).

    When ``page`` is provided (CDP mode), reuses it directly.
    Otherwise launches a new headed browser with cookie injection.
    """
    from core.comment_ui import scrape_comments_ui
    own_browser = page is None
    pw = br = None
    if own_browser:
        pw = await async_playwright().start()
        br = await pw.chromium.launch(
            headless=HEADLESS,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
        )
        context = await br.new_context(
            viewport={'width': 1400, 'height': 950},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            locale='zh-CN', timezone_id='Asia/Shanghai',
        )
        await context.add_init_script(STEALTH_JS)
        cookies = []
        for item in DOUYIN_COOKIE.split(';'):
            item = item.strip()
            if '=' in item:
                n, v = item.split('=', 1)
                cookies.append({'name': n.strip(), 'value': v.strip(), 'domain': '.douyin.com', 'path': '/'})
        await context.add_cookies(cookies)
        page = await context.new_page()

    l1_all, l2_all = [], []
    for vi, post in enumerate(posts):
        if post.get('comment_count', 0) == 0:
            print(f'  [{vi+1}/{len(posts)}] Skip {post["aweme_id"]} (0 comments)')
            continue
        print(f'  [{vi+1}/{len(posts)}] {post["aweme_id"]} (comments: {post["comment_count"]}) [UI]')
        try:
            l1, l2 = await scrape_comments_ui(
                page, post['aweme_id'], keyword=KEYWORD, desc=post.get('desc', ''),
                max_l1=MAX_COMMENTS_PER_POST,
            )
            l1_all.extend(l1)
            l2_all.extend(l2)
        except Exception as e:
            print(f'    UI comment scrape failed: {e}')
        await polite_sleep()

    if own_browser:
        try:
            await br.close()
            await pw.stop()
        except Exception:
            pass
    return l1_all, l2_all


async def fetch_comments_for_posts(posts, page=None):
    """Fetch first- AND second-level comments for all posts using the browser.

    When ``page`` is provided (CDP mode), reuses it directly.
    Otherwise launches a new browser with cookie injection.
    """
    own_browser = page is None
    pw = br = None
    if own_browser:
        pw = await async_playwright().start()
        br = await pw.chromium.launch(
            headless=HEADLESS,
            args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
        )
        context = await br.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
        )
        await context.add_init_script(STEALTH_JS)
        cookies = []
        for item in DOUYIN_COOKIE.split(';'):
            item = item.strip()
            if '=' in item:
                n, v = item.split('=', 1)
                cookies.append({'name': n.strip(), 'value': v.strip(), 'domain': '.douyin.com', 'path': '/'})
        await context.add_cookies(cookies)
        page = await context.new_page()

    l1_records = []
    l2_records = []

    for vi, post in enumerate(posts):
        aweme_id = post['aweme_id']
        comment_count = post['comment_count']
        if comment_count == 0:
            print(f'  [{vi+1}/{len(posts)}] Skip {aweme_id} (0 comments)')
            continue

        print(f'  [{vi+1}/{len(posts)}] {aweme_id} (comments: {comment_count})')

        try:
            await page.goto(
                f'https://www.douyin.com/video/{aweme_id}',
                wait_until='domcontentloaded', timeout=30000
            )
        except:
            pass
        await asyncio.sleep(4)

        cursor = 0
        video_comments = 0
        video_replies = 0
        l2_dry = 0
        max_comments = min(comment_count, MAX_COMMENTS_PER_POST)

        while video_comments < max_comments:
            url = (
                f'{DOUYIN_API_BASE}/comment/list/'
                f'?aweme_id={aweme_id}'
                f'&cursor={cursor}&count=20'
                f'&item_type=0'
                f'&device_platform=webapp&aid=6383'
                f'&cookie_enabled=true&platform=PC'
            )
            data = await _browser_get_json(page, url)
            if not data or data.get('status_code') != 0:
                break
            comments = data.get('comments', [])
            if not comments:
                break

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for c in comments:
                user = c.get('user', {})
                ct = c.get('create_time', 0)
                cid = c.get('cid', '')
                reply_total = c.get('reply_comment_total', 0)
                l1_records.append({
                    '评论ID': cid,
                    '评论内容': c.get('text', ''),
                    '评论者昵称': user.get('nickname', ''),
                    '评论者ID': user.get('uid', ''),
                    '所属作品ID': aweme_id,
                    '所属作品描述': (post['desc'] or '')[:100],
                    '点赞数': c.get('digg_count', 0),
                    '回复数': reply_total,
                    '评论时间': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ct)) if ct else '',
                    '来源': KEYWORD,
                    '爬取时间': now,
                })
                video_comments += 1
                post_budget_left = MAX_REPLIES_PER_POST - video_replies
                if (not SKIP_L2 and reply_total and cid
                        and post_budget_left > 0 and l2_dry < L2_DRY_GIVEUP):
                    cap = min(MAX_REPLIES_PER_L1, post_budget_left)
                    replies = await _fetch_replies(page, aweme_id, cid, user.get('nickname', ''), max_replies=cap)
                    if replies:
                        l2_records.extend(replies)
                        video_replies += len(replies)
                        l2_dry = 0
                    else:
                        l2_dry += 1
                if video_comments >= max_comments:
                    break

            if not data.get('has_more', 0):
                break
            cursor = data.get('cursor', 0)
            await polite_sleep()

        print(f'    Got {video_comments} L1 comments, {video_replies} L2 replies')

    if own_browser:
        await br.close()
        await pw.stop()
    return l1_records, l2_records


def write_posts_to_feishu(posts, feishu):
    """Download each post's media, upload to Feishu, and write the post records."""
    if not APPEND:
        feishu.delete_all_records(VIDEO_TABLE_ID)
        feishu.delete_all_records(IMAGE_TABLE_ID)
    else:
        print('  [APPEND] keeping existing records in video/image tables')

    video_records = []
    image_records = []
    for i, post in enumerate(posts):
        print(f'  [{i+1}/{len(posts)}] {post["type"]} - {post["aweme_id"]}')
        tokens = download_and_upload_media(post, feishu)
        record = build_record(post, tokens)
        if post['type'] == 'video':
            video_records.append(record)
        else:
            image_records.append(record)
        cleanup_downloads()

    if video_records:
        written = feishu.write_records(video_records, VIDEO_TABLE_ID)
        print(f'Written {written}/{len(video_records)} video records')
    if image_records:
        written = feishu.write_records(image_records, IMAGE_TABLE_ID)
        print(f'Written {written}/{len(image_records)} image records')
    return video_records, image_records


async def scrape_and_write_comments(posts, page=None):
    """Fetch L1 + L2 comments and write to Feishu. Passes ``page`` through
    to the comment scrapers for CDP reuse."""
    mode = 'UI clicks' if USE_UI_COMMENTS else 'API'
    print(f'\n=== Step 3: Fetch L1 + L2 comments for all {len(posts)} posts ({mode}) ===')
    if USE_UI_COMMENTS:
        l1_records, l2_records = await fetch_comments_ui(posts, page=page)
    else:
        l1_records, l2_records = await fetch_comments_for_posts(posts, page=page)
    print(f'L1 comments: {len(l1_records)}, L2 replies: {len(l2_records)}')

    if l1_records or l2_records:
        print('\n=== Step 4: Write comments to Feishu (separate tables) ===')
        feishu = FeishuBitable(app_token=APP_TOKEN)
        if l1_records:
            if not APPEND:
                feishu.delete_all_records(COMMENT_L1_TABLE_ID)
            w1 = feishu.write_records(l1_records, COMMENT_L1_TABLE_ID)
            print(f'Written {w1} 一级评论 records')
        if l2_records:
            if not APPEND:
                feishu.delete_all_records(COMMENT_L2_TABLE_ID)
            w2 = feishu.write_records(l2_records, COMMENT_L2_TABLE_ID)
            print(f'Written {w2} 二级评论 records')
        feishu.close()
    return l1_records, l2_records


def _extract_sec_uid(url: str) -> str:
    """Pull the sec_uid out of a douyin author-homepage URL."""
    m = re.search(r'/user/([A-Za-z0-9_-]+)', url or '')
    return m.group(1) if m else ''


async def _fetch_author_awemes(client, sec_uid, need):
    """Page an author's post-list (/aweme/post/) over HTTP, preserving order and
    the is_top pinned flag, until we have at least `need` posts or run out."""
    out = []
    cursor = 0
    for page in range(MAX_PAGES):
        if len(out) >= need:
            break
        params = {
            'device_platform': 'webapp', 'aid': '6383', 'sec_user_id': sec_uid,
            'max_cursor': cursor, 'count': 20, 'cookie_enabled': 'true', 'platform': 'PC',
        }
        data = await fetch_json(client, f'{DOUYIN_API_BASE}/aweme/post/', params,
                                item_keys=('aweme_list',), label=f'author posts p{page}')
        lst = data.get('aweme_list') or []
        if not lst:
            break
        out.extend(lst)
        if not data.get('has_more', 0):
            break
        cursor = data.get('max_cursor', 0)
        await polite_sleep()
    return out


def _select_author_posts(awemes):
    """From an author's post-list pick the target slice, skipping pinned videos
    and optionally filtering by date range (DATE_FROM / DATE_TO)."""
    pinned = [a for a in awemes if a.get('is_top')]
    if pinned:
        base = [a for a in awemes if not a.get('is_top')]
        note = f'detected {len(pinned)} pinned (is_top) post(s), dropped them'
    else:
        base = awemes[AUTHOR_TOP_SKIP:]
        note = f'no is_top flag returned; skipped first {AUTHOR_TOP_SKIP} as pinned'
    if DATE_FROM or DATE_TO:
        before = len(base)
        base = [a for a in base
                if (not DATE_FROM or a.get('create_time', 0) >= DATE_FROM)
                and (not DATE_TO or a.get('create_time', 0) <= DATE_TO)]
        note += f'; date filter: {before} -> {len(base)} post(s)'
    return base[:AUTHOR_RECENT_COUNT], note


async def _browser_user_info(homepage_url, page=None):
    """Fetch the author profile from a browser context. When ``page`` is
    provided (CDP mode), reuses it; otherwise launches a throwaway browser."""
    sec_uid = _extract_sec_uid(homepage_url)
    if not sec_uid:
        return None
    url = (
        f'{DOUYIN_API_BASE}/user/profile/other/'
        f'?sec_user_id={sec_uid}&device_platform=webapp&aid=6383'
        f'&cookie_enabled=true&platform=PC'
    )
    own_browser = page is None
    pw = br = None
    try:
        if own_browser:
            pw = await async_playwright().start()
            br = await pw.chromium.launch(
                headless=HEADLESS,
                args=['--disable-blink-features=AutomationControlled', '--no-sandbox'],
            )
            ctx = await br.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
                locale='zh-CN', timezone_id='Asia/Shanghai',
            )
            await ctx.add_init_script(STEALTH_JS)
            ck = []
            for c in DOUYIN_COOKIE.split(';'):
                c = c.strip()
                if '=' in c:
                    n, v = c.split('=', 1)
                    ck.append({'name': n.strip(), 'value': v.strip(), 'domain': '.douyin.com', 'path': '/'})
            await ctx.add_cookies(ck)
            page = await ctx.new_page()

        try:
            await page.goto(homepage_url, wait_until='domcontentloaded', timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(5)
        data = await _browser_get_json(page, url)
        user = (data or {}).get('user') or {}
        if not user:
            return None
        avatar = user.get('avatar_larger', {})
        return UserInfo(
            uid=user.get('uid', ''),
            sec_uid=user.get('sec_uid', sec_uid),
            nickname=user.get('nickname', ''),
            signature=user.get('signature', ''),
            follower_count=user.get('follower_count', 0),
            following_count=user.get('following_count', 0),
            total_favorited=user.get('total_favorited', 0),
            aweme_count=user.get('aweme_count', 0),
            avatar_url=avatar.get('url_list', [''])[0] if isinstance(avatar, dict) else '',
            homepage_url=f'https://www.douyin.com/user/{sec_uid}',
        )
    except Exception as e:
        print(f'  [Author] browser profile fetch failed: {e}')
        return None
    finally:
        if own_browser:
            try:
                if br:
                    await br.close()
                if pw:
                    await pw.stop()
            except Exception:
                pass


async def scrape_author(page=None):
    """Scrape one author homepage. When ``page`` is provided (CDP mode), all
    API calls go through the browser's fetch() — no cookie export needed."""
    sec_uid = _extract_sec_uid(AUTHOR_URL)
    if not sec_uid:
        print(f'[Author] cannot parse sec_uid from {AUTHOR_URL}')
        return None, []

    if page:
        # CDP mode: route all API calls through the browser
        client = BrowserClient(page)
        # Navigate to douyin.com so fetch() sends cookies for this origin
        try:
            await page.goto('https://www.douyin.com', wait_until='domcontentloaded', timeout=30000)
        except Exception:
            pass
        await asyncio.sleep(3)
        async with client:
            user_info = await UserScraper(client).get_user_info(AUTHOR_URL)
            need = AUTHOR_TOP_SKIP + AUTHOR_RECENT_COUNT + 15
            raw = await _fetch_author_awemes(client, sec_uid, need)
    else:
        # Legacy: HTTP client with exported cookie
        async with DouyinClient(cookies=DOUYIN_COOKIE) as client:
            user_info = await UserScraper(client).get_user_info(AUTHOR_URL)
            need = AUTHOR_TOP_SKIP + AUTHOR_RECENT_COUNT + 15
            raw = await _fetch_author_awemes(client, sec_uid, need)

    if not user_info or not user_info.follower_count:
        fb = await _browser_user_info(AUTHOR_URL, page=page)
        if fb and fb.follower_count:
            if user_info:
                user_info.follower_count = fb.follower_count
                user_info.nickname = user_info.nickname or fb.nickname
                user_info.aweme_count = user_info.aweme_count or fb.aweme_count
                user_info.total_favorited = user_info.total_favorited or fb.total_favorited
                user_info.following_count = user_info.following_count or fb.following_count
                user_info.signature = user_info.signature or fb.signature
            else:
                user_info = fb
    if user_info and not user_info.follower_count and raw:
        fc = (raw[0].get('author') or {}).get('follower_count', 0)
        if fc:
            user_info.follower_count = fc

    print(f'[Author] fetched {len(raw)} posts from homepage')
    selected, note = _select_author_posts(raw)
    print(f'[Author] {note}; selected {len(selected)} post(s)')
    posts = [parse_post(a) for a in selected]
    return user_info, posts


async def main_author():
    """Author-homepage pipeline entry point. When CDP_ENDPOINT is set,
    connects to the user's logged-in Chrome and reuses that session for
    all API calls and comment scraping — no cookie export needed."""
    print('=== Author mode: scrape author homepage ===')
    print(f'  Homepage: {AUTHOR_URL}')

    cdp = USE_CDP
    browser = None
    page = None

    if cdp:
        print(f'  [CDP] Connecting to Chrome at {cdp} ...')
        from core.browser import DouyinBrowser
        browser = DouyinBrowser()
        await browser.start_cdp(cdp)
        page = browser.page

    try:
        user_info, posts = await scrape_author(page=page)

        if user_info:
            print(f'作者: {user_info.nickname} | 粉丝量: {user_info.follower_count} | '
                  f'获赞: {user_info.total_favorited} | 作品: {user_info.aweme_count}')
        else:
            print('[Author] WARNING: could not fetch author profile (fan count).')

        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        feishu = FeishuBitable(app_token=APP_TOKEN)

        if user_info and AUTHOR_TABLE_ID and 'YOUR_' not in AUTHOR_TABLE_ID:
            if not APPEND:
                feishu.delete_all_records(AUTHOR_TABLE_ID)
            feishu.write_records([author_to_feishu_record(user_info, now)], AUTHOR_TABLE_ID)
            print('Written author profile (粉丝量) to 作者信息 table')
        elif user_info:
            print('[Author] No AUTHOR_TABLE_ID configured; skipped writing author profile table.')

        video_records, image_records = [], []
        if posts:
            print(f'\n=== Step 2: Download media and upload to Feishu ({len(posts)} posts) ===')
            video_records, image_records = write_posts_to_feishu(posts, feishu)
        else:
            print('[Author] No posts selected — nothing to download.')
        feishu.close()

        l1_records, l2_records = [], []
        if posts and not SKIP_COMMENTS:
            l1_records, l2_records = await scrape_and_write_comments(posts, page=page)
        elif SKIP_COMMENTS:
            print('\n=== Skipping comments (SKIP_COMMENTS set) ===')

        print('\n=== Done (author mode) ===')
        if user_info:
            print(f'作者: {user_info.nickname} | 粉丝量: {user_info.follower_count}')
        print(f'Video records: {len(video_records)}, Image records: {len(image_records)}')
        print(f'L1 comments: {len(l1_records)}, L2 replies: {len(l2_records)}')

    finally:
        if browser:
            await browser.close()


async def main_batch(urls):
    """Batch mode: scrape multiple author homepages into one shared bitable."""
    global AUTHOR_URL, KEYWORD, APPEND

    print(f'=== Batch mode: {len(urls)} authors ===')

    cdp = USE_CDP
    browser = None
    page = None

    if cdp:
        print(f'  [CDP] Connecting to Chrome at {cdp} ...')
        from core.browser import DouyinBrowser
        browser = DouyinBrowser()
        await browser.start_cdp(cdp)
        page = browser.page

    try:
        total_authors = 0
        total_videos = 0
        total_images = 0
        total_l1 = 0
        total_l2 = 0

        for i, url in enumerate(urls):
            AUTHOR_URL = url
            KEYWORD = url

            print(f'\n{"="*60}')
            print(f'[{i+1}/{len(urls)}] {url}')
            print(f'{"="*60}')

            user_info, posts = await scrape_author(page=page)

            if user_info:
                print(f'  作者: {user_info.nickname} | 粉丝量: {user_info.follower_count} | '
                      f'获赞: {user_info.total_favorited} | 作品: {user_info.aweme_count}')
            else:
                print(f'  WARNING: could not fetch author profile')

            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            feishu = FeishuBitable(app_token=APP_TOKEN)

            if user_info and AUTHOR_TABLE_ID and 'YOUR_' not in AUTHOR_TABLE_ID:
                if i == 0 and not APPEND:
                    feishu.delete_all_records(AUTHOR_TABLE_ID)
                feishu.write_records([author_to_feishu_record(user_info, now)], AUTHOR_TABLE_ID)
                total_authors += 1

            if posts:
                print(f'  Downloading media for {len(posts)} posts...')
                video_records, image_records = write_posts_to_feishu(posts, feishu)
                total_videos += len(video_records)
                total_images += len(image_records)
            else:
                print(f'  No posts selected.')

            feishu.close()

            if posts and not SKIP_COMMENTS:
                l1, l2 = await scrape_and_write_comments(posts, page=page)
                total_l1 += len(l1)
                total_l2 += len(l2)

            # After first author, switch to append mode so we don't clear
            # previously written records for subsequent authors
            APPEND = True

        print(f'\n=== Batch done ({len(urls)} authors) ===')
        print(f'Authors: {total_authors}, Videos: {total_videos}, Images: {total_images}')
        print(f'L1 comments: {total_l1}, L2 replies: {total_l2}')

    finally:
        if browser:
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main_author())
