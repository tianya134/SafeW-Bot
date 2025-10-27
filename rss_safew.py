import feedparser
import logging
import asyncio
import json
import os
import aiohttp
import uuid
import re
from bs4 import BeautifulSoup

# ====================== 环境配置（核心：使用绝对路径）======================
SAFEW_BOT_TOKEN = os.getenv("SAFEW_BOT_TOKEN")
SAFEW_CHAT_ID = os.getenv("SAFEW_CHAT_ID")
RSS_FEED_URL = os.getenv("RSS_FEED_URL")
# 强制绝对路径：当前脚本所在目录 + 文件名
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))  # 脚本所在目录
SENT_POSTS_FILE = os.path.join(SCRIPT_DIR, "sent_posts.json")       # 已推送TID（绝对路径）
PENDING_POSTS_FILE = os.path.join(SCRIPT_DIR, "pending_tids.json")  # 待审核TID（绝对路径）
MAX_PUSH_PER_RUN = 5
FIXED_PROJECT_URL = "https://tyw29.cc/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
MAX_IMAGES_PER_MSG = 10
IMAGE_DOWNLOAD_TIMEOUT = 15
MSG_SEND_TIMEOUT = 30

# ====================== 日志配置（新增文件路径日志）======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logging.info(f"脚本目录：{SCRIPT_DIR}")
logging.info(f"已推送文件路径：{SENT_POSTS_FILE}")
logging.info(f"待审核文件路径：{PENDING_POSTS_FILE}")

# ====================== 工具函数 =======================
def get_image_content_type(filename):
    ext = filename.lower().split(".")[-1]
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp"
    }
    return mime_map.get(ext, "image/jpeg")

def is_valid_image(data):
    if not data:
        return False
    signatures = {b"\xff\xd8\xff": "jpeg", b"\x89\x50\x4e\x47": "png", b"\x47\x49\x46\x38": "gif", b"\x52\x49\x46\x46": "webp"}
    for sig, _ in signatures.items():
        if data.startswith(sig):
            return True
    logging.warning(f"无效图片文件头：{data[:8].hex()}")
    return False

# ====================== TID管理（核心：强化待审核文件读写）======================
# 已推送TID
def load_sent_tids():
    try:
        if not os.path.exists(SENT_POSTS_FILE):
            with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
                json.dump([], f)
            logging.info(f"初始化已推送文件：{SENT_POSTS_FILE}")
            return []
        with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:
            tids = json.loads(f.read().strip() or "[]")
            return [int(t) for t in tids if isinstance(t, int)]
    except Exception as e:
        logging.error(f"读取已推送TID失败（路径：{SENT_POSTS_FILE}）：{str(e)}")
        return []

def save_sent_tids(new_tids, existing_tids):
    try:
        all_tids = sorted(list(set(existing_tids + new_tids)))
        with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_tids, f, ensure_ascii=False, indent=2)
        logging.info(f"已推送TID更新（路径：{SENT_POSTS_FILE}）：新增{len(new_tids)}条，总计{len(all_tids)}条 → {all_tids[:5]}...")
    except Exception as e:
        logging.error(f"保存已推送TID失败（路径：{SENT_POSTS_FILE}）：{str(e)}")

# 待审核TID（核心修复）
def load_pending_tids():
    try:
        if not os.path.exists(PENDING_POSTS_FILE):
            with open(PENDING_POSTS_FILE, "w", encoding="utf-8") as f:
                json.dump([], f)
            logging.info(f"初始化待审核文件：{PENDING_POSTS_FILE}")
            return []
        # 读取前先确认文件存在且可访问
        if not os.access(PENDING_POSTS_FILE, os.R_OK):
            raise PermissionError(f"无读取权限：{PENDING_POSTS_FILE}")
        with open(PENDING_POSTS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip() or "[]"
            tids = json.loads(content)
            valid_tids = [int(t) for t in tids if isinstance(t, (int, str)) and str(t).isdigit()]  # 严格校验TID格式
            logging.info(f"读取待审核TID（路径：{PENDING_POSTS_FILE}）：共{len(valid_tids)}条 → {valid_tids}")
            return valid_tids
    except Exception as e:
        logging.error(f"读取待审核TID失败（路径：{PENDING_POSTS_FILE}）：{str(e)}")
        return []

def save_pending_tids(tids):
    try:
        # 强制去重、排序、格式校验
        valid_tids = sorted(list(set([int(t) for t in tids if isinstance(t, (int, str)) and str(t).isdigit()])))
        # 写入前确认目录可写
        if not os.access(os.path.dirname(PENDING_POSTS_FILE), os.W_OK):
            raise PermissionError(f"目录无写入权限：{os.path.dirname(PENDING_POSTS_FILE)}")
        # 先写入临时文件，再替换原文件（避免写入中断导致文件损坏）
        temp_file = f"{PENDING_POSTS_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(valid_tids, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, PENDING_POSTS_FILE)  # 原子操作替换文件
        logging.info(f"待审核TID更新（路径：{PENDING_POSTS_FILE}）：当前共{len(valid_tids)}条 → {valid_tids}")
    except Exception as e:
        logging.error(f"保存待审核TID失败（路径：{PENDING_POSTS_FILE}）：{str(e)}")
        # 尝试直接写入原文件（备用方案）
        try:
            with open(PENDING_POSTS_FILE, "w", encoding="utf-8") as f:
                json.dump(valid_tids, f, ensure_ascii=False, indent=2)
            logging.warning(f"备用方案：待审核TID已写入（可能不完整）")
        except:
            pass

# ====================== TID提取/RSS获取 =======================
def extract_tid_from_url(url):
    try:
        match = re.search(r'thread-(\d+)\.htm', url)
        return int(match.group(1)) if match else None
    except Exception as e:
        logging.error(f"提取TID失败：{str(e)}")
        return None

def fetch_updates(sent_tids, pending_tids):
    try:
        logging.info(f"筛选RSS新帖：排除已推送{len(sent_tids)}条 + 待审核{len(pending_tids)}条")
        feed = feedparser.parse(RSS_FEED_URL)
        if feed.bozo:
            logging.error(f"RSS解析失败：{feed.bozo_exception}")
            return None
        
        valid_entries = []
        for entry in feed.entries:
            link = entry.get("link", "").strip()
            if not link:
                continue
            tid = extract_tid_from_url(link)
            if not tid:
                continue
            if tid not in sent_tids and tid not in pending_tids:
                entry["tid"] = tid
                valid_entries.append(entry)
                logging.debug(f"新增待处理TID：{tid}（标题：{entry.get('title', '无标题')[:20]}...）")
        
        logging.info(f"RSS筛选完成：共{len(valid_entries)}条全新待推送帖")
        return sorted(valid_entries, key=lambda x: x["tid"])
    except Exception as e:
        logging.error(f"获取RSS异常：{str(e)}")
        return None

# ====================== 帖子信息获取 ========================
async def get_post_info(session, webpage_url, tid):
    """返回：(images: 图片列表, is_pending: 是否待审核, status_code: 请求状态码)"""
    status_code = 200
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": FIXED_PROJECT_URL,
            "Accept": "text/html,application/xhtml+xml"
        }
        async with session.get(webpage_url, headers=headers, timeout=20) as resp:
            status_code = resp.status
            if resp.status != 200:
                logging.warning(f"TID={tid} 帖子请求失败（状态码：{resp.status}）")
                return [], False, status_code
            html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        is_pending = False

        audit_h4_tags = soup.find_all("h4", class_=re.compile(r"card-title"))
        audit_pattern = re.compile(r"本帖正在审核中.*您无权查看", re.DOTALL | re.UNICODE)
        for h4_tag in audit_h4_tags:
            tag_text = h4_tag.get_text(strip=True)
            if audit_pattern.search(tag_text):
                is_pending = True
                break
        if not is_pending and audit_pattern.search(html):
            is_pending = True

        if is_pending:
            logging.info(f"TID={tid} 确认待审核状态（状态码：{status_code}）")
            return [], True, status_code

        target_divs = soup.find_all("div", class_="message break-all", isfirst="1") or soup.find_all("div", class_="message break-all")
        if not target_divs:
            logging.warning(f"TID={tid} 未找到正文div（状态码：{status_code}），无图片")
            return [], False, status_code

        images = []
        base_domain = "/".join(webpage_url.split("/")[:3])
        for div in target_divs:
            for img in div.find_all("img"):
                img_url = img.get("data-src", "").strip() or img.get("src", "").strip()
                if not img_url or img_url.startswith(("data:image/", "javascript:")):
                    continue
                if img_url.startswith("/"):
                    img_url = f"{base_domain}{img_url}"
                elif not img_url.startswith(("http", "https")):
                    img_url = f"{base_domain}/{img_url}"
                if img_url not in images and img_url.startswith(("http", "https")):
                    images.append(img_url)

        final_images = images[:MAX_IMAGES_PER_MSG]
        logging.info(f"TID={tid} 图片提取完成（状态码：{status_code}）：共{len(images)}张，保留前{len(final_images)}张")
        return final_images, False, status_code
    except Exception as e:
        logging.error(f"TID={tid} 帖子信息获取异常（状态码：{status_code}）：{str(e)}")
        return [], False, status_code

# ====================== Markdown转义/消息构造 =======================
def escape_markdown(text):
    special_chars = r"_*~`>#+!()"
    for char in special_chars:
        if char in text:
            text = text.replace(char, f"\{char}")
    return text

def build_caption(title, author, link):
    footer = """
论坛最新地址:
tyw29.cc  tyw30.cc  tyw33.cc

点击前往福利通知群: https://www.safew.vc/tyw777

点击前往聊天群组: https://www.sfw.vc/tyw666

天涯论坛（唯一联系）方式：
沈复：＠tywcc
沐泽：＠ssss001
怡怡：＠yiyi3
    """.strip()
    return (
        f"{escape_markdown(title)}\n"
        f"由 ＠{escape_markdown(author)} 发起的话题讨论\n"
        f"链接：{link}\n\n"
        f"{footer}"
    )

# ====================== 消息发送函数 ========================
async def send_single_photo(session, image_url, caption, tid, delay=5):
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendPhoto"
        async with session.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=15) as resp:
            img_data = await resp.read()
            if not is_valid_image(img_data):
                return False
            content_type = resp.headers.get("Content-Type") or get_image_content_type(image_url)
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
        filename = f"single_{tid}_{uuid.uuid4().hex[:8]}.jpg"
        body = b"\r\n".join([
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="chat_id"',
            b'',
            str(SAFEW_CHAT_ID).encode("utf-8"),
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="caption"',
            b'',
            caption.encode("utf-8"),
            f"--{boundary}".encode("utf-8"),
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"'.encode("utf-8"),
            f"Content-Type: {content_type}".encode("utf-8"),
            b'',
            img_data,
            f"--{boundary}--".encode("utf-8")
        ])
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        async with session.post(api_url, data=body, headers=headers, timeout=30) as resp:
            if resp.status == 200:
                logging.info(f"TID={tid} ✅ 单图消息发送成功")
                return True
            logging.error(f"TID={tid} ❌ 单图失败：{await resp.text()[:200]}")
            return False
    except Exception as e:
        logging.error(f"TID={tid} 单图发送异常：{str(e)}")
        return False

async def send_media_group(session, image_urls, caption, tid, delay=5):
    if len(image_urls) < 2 or len(image_urls) > MAX_IMAGES_PER_MSG:
        return False
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendMediaGroup"
        media_data = []
        for idx, img_url in enumerate(image_urls, 1):
            filename = f"media_{tid}_{idx}_{uuid.uuid4().hex[:8]}.jpg"
            async with session.get(img_url, headers={"User-Agent": USER_AGENT}, timeout=15) as resp:
                img_data = await resp.read()
                if not is_valid_image(img_data):
                    return False
                content_type = resp.headers.get("Content-Type") or get_image_content_type(img_url)
            media_data.append((img_data, content_type, filename))
        
        media_array = []
        for idx, (_, ct, fn) in enumerate(media_data):
            item = {"type": "photo", "media": f"attach://{fn}", "parse_mode": "Markdown"}
            if idx == 0:
                item["caption"] = caption
            media_array.append(item)
        
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
        body_parts = [
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="chat_id"',
            b'',
            str(SAFEW_CHAT_ID).encode("utf-8"),
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="media"',
            b'Content-Type: application/json',
            b'',
            json.dumps(media_array, ensure_ascii=False).encode("utf-8")
        ]
        for img_data, ct, fn in media_data:
            body_parts.extend([
                f"--{boundary}".encode("utf-8"),
                f'Content-Disposition: form-data; name="{fn}"; filename="{fn}"'.encode("utf-8"),
                f"Content-Type: {ct}".encode("utf-8"),
                b'',
                img_data
            ])
        body_parts.append(f"--{boundary}--".encode("utf-8"))
        body = b"\r\n".join(body_parts)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        async with session.post(api_url, data=body, headers=headers, timeout=30) as resp:
            if resp.status == 200:
                logging.info(f"TID={tid} ✅ 多图消息发送成功")
                return True
            logging.error(f"TID={tid} ❌ 多图失败：{await resp.text()[:200]}")
            return False
    except Exception as e:
        logging.error(f"TID={tid} 多图发送异常：{str(e)}")
        return False

async def send_text_msg(session, caption, tid, delay=5):
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": SAFEW_CHAT_ID,
            "text": caption,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        async with session.post(api_url, json=payload, timeout=15) as resp:
            if resp.status == 200:
                logging.info(f"TID={tid} ✅ 纯文本发送成功")
                return True
            logging.error(f"TID={tid} ❌ 文本失败：{await resp.text()[:200]}")
            return False
    except Exception as e:
        logging.error(f"TID={tid} 文本发送异常：{str(e)}")
        return False

# ====================== 待审核TID检查 =======================
async def check_pending_tids(session):
    pending_tids = load_pending_tids()  # 读取时已校验格式
    if not pending_tids:
        logging.info("无待审核TID，跳过检查")
        return

    logging.info(f"\n=== 开始检查待审核TID（共{len(pending_tids)}条 → {pending_tids}）===")
    sent_tids = load_sent_tids()
    passed_tids = []
    still_pending = []
    deleted_tids = []

    for tid in pending_tids:
        link = f"{FIXED_PROJECT_URL}thread-{tid}.htm"
        logging.info(f"检查TID={tid} 审核状态：{link[:50]}...")
        
        images, is_pending, status_code = await get_post_info(session, link, tid)

        if status_code == 404:
            deleted_tids.append(tid)
            logging.warning(f"TID={tid} 帖子返回404（已删除），不推送，从待审核列表移除")
            continue

        if status_code != 200:
            still_pending.append(tid)
            logging.warning(f"TID={tid} 帖子请求异常（状态码：{status_code}），保留待下次检查")
            continue

        if is_pending:
            still_pending.append(tid)
            logging.info(f"TID={tid} 仍在审核中，保留待下次检查")
            continue

        title = f"待审核通过帖（TID：{tid}）"
        author = "未知用户"
        caption = build_caption(title, author, link)
        
        success = False
        if len(images) == 1:
            success = await send_single_photo(session, images[0], caption, tid, delay=3)
        elif 2 <= len(images) <= MAX_IMAGES_PER_MSG:
            success = await send_media_group(session, images, caption, tid, delay=3)
        else:
            success = await send_text_msg(session, caption, tid, delay=3)

        if success:
            passed_tids.append(tid)
            sent_tids.append(tid)
            logging.info(f"TID={tid} 审核通过并推送成功")
        else:
            still_pending.append(tid)
            logging.warning(f"TID={tid} 审核通过但推送失败，保留待下次重试")

    # 强制去重后再保存（双重保障）
    final_still_pending = sorted(list(set(still_pending)))
    save_pending_tids(final_still_pending)
    if passed_tids:
        save_sent_tids(passed_tids, sent_tids)
    logging.info(f"待审核TID检查完成：{len(passed_tids)}条通过，{len(final_still_pending)}条仍待审核，{len(deleted_tids)}条已删除")

# ====================== 全新帖子推送 =======================
async def push_new_posts(session, new_entries):
    if not new_entries:
        logging.info("无全新帖子待推送")
        return

    logging.info(f"\n=== 开始推送全新帖子（共{len(new_entries)}条）===")
    sent_tids = load_sent_tids()
    pending_tids = load_pending_tids()  # 读取时已校验
    success_pushed = []

    for i, entry in enumerate(new_entries):
        tid = entry["tid"]
        link = entry["link"]
        title = entry.get("title", "无标题").strip()
        author = entry.get("author", "未知用户").strip()
        delay = 5 if i > 0 else 0

        images, is_pending, status_code = await get_post_info(session, link, tid)
        
        if status_code == 404:
            logging.warning(f"TID={tid} 全新帖子返回404（已删除），跳过推送")
            continue
        
        if status_code != 200:
            logging.warning(f"TID={tid} 全新帖子请求异常（{status_code}），跳过推送")
            continue

        if is_pending:
            # 新增待审核：先去重再保存
            pending_tids.append(tid)
            final_pending = sorted(list(set(pending_tids)))
            save_pending_tids(final_pending)
            logging.info(f"TID={tid} 新增待审核 → 待审核列表：{final_pending}")
            continue

        caption = build_caption(title, author, link)
        success = False
        if len(images) == 1:
            success = await send_single_photo(session, images[0], caption, tid, delay)
        elif 2 <= len(images) <= MAX_IMAGES_PER_MSG:
            success = await send_media_group(session, images, caption, tid, delay)
        else:
            success = await send_text_msg(session, caption, tid, delay)

        if success:
            success_pushed.append(tid)
            sent_tids.append(tid)
            logging.info(f"TID={tid} 全新帖子推送成功 → 已推送列表待更新：{success_pushed}")

    if success_pushed:
        save_sent_tids(success_pushed, sent_tids)
    else:
        logging.info("无全新帖子推送成功，不更新已推送列表")

# ====================== 主逻辑整合 =======================
async def check_for_updates():
    async with aiohttp.ClientSession() as session:
        await check_pending_tids(session)
        sent_tids = load_sent_tids()
        pending_tids = load_pending_tids()
        new_entries = fetch_updates(sent_tids, pending_tids)
        if new_entries:
            await push_new_posts(session, new_entries[:MAX_PUSH_PER_RUN])

# ====================== 主函数 =======================
async def main():
    logging.info("===== SafeW RSS推送脚本启动 =====")
    if not all([SAFEW_BOT_TOKEN, SAFEW_CHAT_ID, RSS_FEED_URL]):
        logging.error("❌ 缺少必要环境配置，终止运行")
        return

    # 再次确认待审核文件存在
    if not os.path.exists(PENDING_POSTS_FILE):
        save_pending_tids([])
        logging.info(f"初始化待审核文件：{PENDING_POSTS_FILE}")

    try:
        await check_for_updates()
    except Exception as e:
        logging.error(f"❌ 核心逻辑异常：{str(e)}")
    logging.info("===== 脚本运行结束 =====")

if __name__ == "__main__":
    asyncio.run(main())
