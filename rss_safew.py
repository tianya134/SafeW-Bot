import feedparser
import logging
import asyncio
import json
import os
import aiohttp
import uuid
import re
from bs4 import BeautifulSoup

# ====================== 环境配置 ======================
SAFEW_BOT_TOKEN = os.getenv("SAFEW_BOT_TOKEN")
SAFEW_CHAT_ID = os.getenv("SAFEW_CHAT_ID")
RSS_FEED_URL = os.getenv("RSS_FEED_URL")
SENT_POSTS_FILE = "sent_posts.json"
MAX_PUSH_PER_RUN = 5
FIXED_PROJECT_URL = "https://tyw29.cc/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
MAX_IMAGES_PER_MSG = 10
IMAGE_DOWNLOAD_TIMEOUT = 15
MSG_SEND_TIMEOUT = 30

# ====================== 日志配置 =======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ====================== 1. 工具函数：获取图片Content-Type =======================
def get_image_content_type(filename):
    """根据文件名后缀推测Content-Type（防止响应头无该字段）"""
    ext = filename.lower().split(".")[-1]
    mime_map = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp"
    }
    return mime_map.get(ext, "image/jpeg")

# ====================== 2. TID提取 =======================
def extract_tid_from_url(url):
    try:
        match = re.search(r'thread-(\d+)\.htm', url)
        if match:
            tid = int(match.group(1))
            logging.debug(f"提取TID：{url[:50]}... → {tid}")
            return tid
        logging.warning(f"无法提取TID（URL格式异常）：{url[:50]}...")
        return None
    except Exception as e:
        logging.error(f"提取TID失败：{str(e)}，URL：{url[:50]}...")
        return None

# ====================== 3. 已推送TID存储/读取 ======================
def load_sent_tids():
    try:
        if not os.path.exists(SENT_POSTS_FILE):
            logging.info(f"{SENT_POSTS_FILE}不存在，初始化空列表")
            with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
                json.dump([], f)
            return []
        
        with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                return []
            tids = json.loads(content)
            if not isinstance(tids, list) or not all(isinstance(t, int) for t in tids):
                logging.error(f"{SENT_POSTS_FILE}格式错误，重置为空")
                with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
                    json.dump([], f)
                return []
            logging.info(f"读取到{len(tids)}条已推送TID：{tids[:5]}...")
            return tids
    except json.JSONDecodeError:
        logging.error(f"{SENT_POSTS_FILE}解析失败，重置为空")
        with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
        return []
    except Exception as e:
        logging.error(f"读取TID异常：{str(e)}，返回空列表")
        return []

def save_sent_tids(new_tids, existing_tids):
    try:
        all_tids = list(set(existing_tids + new_tids))
        all_tids_sorted = sorted(all_tids)
        with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_tids_sorted, f, ensure_ascii=False, indent=2)
        logging.info(f"更新推送记录：新增{len(new_tids)}条，总{len(all_tids_sorted)}条")
    except Exception as e:
        logging.error(f"保存TID失败：{str(e)}")

# ====================== 4. RSS获取与筛选 ======================
def fetch_updates():
    try:
        sent_tids = load_sent_tids()
        logging.info(f"筛选新帖（排除{len(sent_tids)}个TID）")
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
            if not tid or tid in sent_tids:
                continue
            entry["tid"] = tid
            valid_entries.append(entry)
            logging.debug(f"待推送：TID={tid}，标题：{entry.get('title', '无标题')[:30]}...")
        logging.info(f"筛选完成：{len(valid_entries)}条新帖")
        return valid_entries
    except Exception as e:
        logging.error(f"获取RSS异常：{str(e)}")
        return None

# ====================== 5. 图片提取 =======================
async def get_images_from_webpage(session, webpage_url):
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": FIXED_PROJECT_URL,
            "Accept": "image/avif,image/webp,*/*"
        }
        async with session.get(webpage_url, headers=headers, timeout=20) as resp:
            if resp.status != 200:
                logging.warning(f"帖子请求失败（{resp.status}）：{webpage_url[:50]}...")
                return []
            html = await resp.text()
        
        soup = BeautifulSoup(html, "html.parser")
        target_divs = soup.find_all("div", class_="message break-all", isfirst="1")
        if not target_divs:
            return []
        
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
                    logging.info(f"提取图片{len(images)}：{img_url[:60]}...")
        
        final_images = images[:MAX_IMAGES_PER_MSG]
        logging.info(f"保留{len(final_images)}张图片（共提取{len(images)}张）")
        return final_images
    except Exception as e:
        logging.error(f"提取图片异常：{str(e)}")
        return []

# ====================== 6. Markdown转义 ======================
def escape_markdown(text):
    special_chars = r"_*~`>#+!()"
    for char in special_chars:
        if char in text:
            text = text.replace(char, f"\{char}")
    return text

# ====================== 7. 单图发送（sendPhoto）=======================
async def send_single_photo_with_caption(session, image_url, caption, delay=5):
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendPhoto"
        logging.info(f"\n=== 处理单图消息 ===")
        logging.info(f"图片：{image_url[:60]}...，文字：{caption[:50]}...")

        # 下载图片
        img_headers = {"User-Agent": USER_AGENT, "Referer": FIXED_PROJECT_URL}
        async with session.get(image_url, headers=img_headers, timeout=IMAGE_DOWNLOAD_TIMEOUT, ssl=False) as img_resp:
            if img_resp.status != 200:
                logging.error(f"图片下载失败（{img_resp.status}）")
                return False
            img_data = await img_resp.read()
            # 优先用响应头Content-Type，其次按文件名推测
            img_content_type = img_resp.headers.get("Content-Type") or get_image_content_type(image_url)
            logging.info(f"图片信息：{len(img_data)}字节，类型：{img_content_type}")

        # 构造请求体（字节流）
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
        filename = f"single_{uuid.uuid4().hex[:8]}.jpg"
        body_parts = [
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
            f"Content-Type: {img_content_type}".encode("utf-8"),
            b'',
            img_data,
            f"--{boundary}--".encode("utf-8")
        ]
        body = b'\r\n'.join(body_parts)

        # 发送
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": USER_AGENT}
        async with session.post(api_url, data=body, headers=headers, timeout=MSG_SEND_TIMEOUT, ssl=False) as resp:
            resp_text = await resp.text()
            if resp.status == 200:
                logging.info("✅ 单图消息发送成功")
                return True
            logging.error(f"❌ 单图失败（{resp.status}）：{resp_text[:150]}...")
            return False
    except Exception as e:
        logging.error(f"❌ 单图发送异常：{str(e)}")
        return False

# ====================== 8. 多图发送（sendMediaGroup，修复FILE_INVALID）=======================
async def send_media_group(session, image_urls, caption, delay=5):
    if len(image_urls) < 2 or len(image_urls) > MAX_IMAGES_PER_MSG:
        logging.error(f"❌ 多图数量无效（需2-{MAX_IMAGES_PER_MSG}张）")
        return False
    
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendMediaGroup"
        logging.info(f"\n=== 处理多图消息（{len(image_urls)}张）===")
        logging.info(f"文字说明：{caption[:50]}...")

        # 1. 下载图片并整理信息（含Content-Type）
        media_items = []  # 存储（img_data, content_type, filename）
        for idx, img_url in enumerate(image_urls, 1):
            try:
                img_headers = {"User-Agent": USER_AGENT, "Referer": FIXED_PROJECT_URL}
                async with session.get(img_url, headers=img_headers, timeout=IMAGE_DOWNLOAD_TIMEOUT, ssl=False) as img_resp:
                    if img_resp.status != 200:
                        logging.error(f"图片{idx}下载失败（{img_resp.status}）：{img_url[:50]}...")
                        return False
                    img_data = await img_resp.read()
                # 确定Content-Type（优先响应头，其次文件名推测）
                content_type = img_resp.headers.get("Content-Type") or get_image_content_type(img_url)
                filename = f"media_{idx}_{uuid.uuid4().hex[:8]}.jpg"  # 统一文件名格式
                media_items.append((img_data, content_type, filename))
                logging.info(f"图片{idx}：{len(img_data)}字节，类型{content_type}，文件名{filename}")
            except Exception as e:
                logging.error(f"图片{idx}处理异常：{str(e)}")
                return False

        # 2. 构造media数组（API要求的结构）
        media_array = []
        for img_data, content_type, filename in media_items:
            media_array.append({
                "type": "photo",  # 固定为图片类型
                "media": f"attach://{filename}"  # 关联后续文件字段
            })

        # 3. 生成multipart请求体（关键修复：调整字段顺序，确保media数组在文件前）
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
        body_parts = []

        # 3.1 基础字段：chat_id（必填）
        body_parts.extend([
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="chat_id"',
            b'',
            str(SAFEW_CHAT_ID).encode("utf-8")
        ])

        # 3.2 全局caption（可选，放在media前）
        if caption:
            body_parts.extend([
                f"--{boundary}".encode("utf-8"),
                b'Content-Disposition: form-data; name="caption"',
                b'',
                caption.encode("utf-8")
            ])

        # 3.3 media数组（核心：必须在图片文件字段之前，API先解析结构）
        body_parts.extend([
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="media"',
            b'Content-Type: application/json',
            b'',
            json.dumps(media_array, ensure_ascii=False).encode("utf-8")
        ])

        # 3.4 图片文件字段（与media数组一一对应）
        for idx, (img_data, content_type, filename) in enumerate(media_items, 1):
            body_parts.extend([
                f"--{boundary}".encode("utf-8"),
                # 字段名用"photo"（兼容多数API，无需索引）
                f'Content-Disposition: form-data; name="photo"; filename="{filename}"'.encode("utf-8"),
                f"Content-Type: {content_type}".encode("utf-8"),
                b'',
                img_data  # 原始图片二进制数据
            ])

        # 3.5 结束符
        body_parts.append(f"--{boundary}--".encode("utf-8"))

        # 4. 拼接请求体（字节流，避免编码问题）
        body = b'\r\n'.join(body_parts)
        logging.info(f"请求体构造完成：大小{len(body)}字节，包含{len(media_items)}张图片")

        # 5. 发送请求
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
            "Content-Length": str(len(body))  # 明确请求体大小
        }
        async with session.post(api_url, data=body, headers=headers, timeout=MSG_SEND_TIMEOUT, ssl=False) as resp:
            resp_text = await resp.text(encoding="utf-8", errors="replace")
            resp_summary = resp_text[:200] + "..." if len(resp_text) > 200 else resp_text
            if resp.status == 200:
                logging.info(f"✅ 多图消息发送成功（{len(media_items)}张）")
                return True
            logging.error(f"❌ 多图失败（{resp.status}）：{resp_summary}")
            return False
    except Exception as e:
        logging.error(f"❌ 多图发送总异常：{str(e)}")
        return False

# ====================== 9. 纯文本发送 ========================
async def send_text(session, caption, delay=5):
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendMessage"
        logging.info(f"\n=== 处理纯文本消息 ===")
        payload = {
            "chat_id": SAFEW_CHAT_ID,
            "text": caption,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        async with session.post(api_url, json=payload, timeout=MSG_SEND_TIMEOUT, ssl=False) as resp:
            resp_text = await resp.text()
            if resp.status == 200:
                logging.info("✅ 纯文本发送成功")
                return True
            logging.error(f"❌ 文本失败（{resp.status}）：{resp_text[:150]}...")
            return False
    except Exception as e:
        logging.error(f"❌ 文本发送异常：{str(e)}")
        return False

# ====================== 10. 核心推送逻辑 =======================
async def check_for_updates():
    rss_entries = fetch_updates()
    if not rss_entries:
        logging.info("无新帖，结束")
        return

    rss_entries_sorted = sorted(rss_entries, key=lambda x: x["tid"])
    logging.info(f"新帖按TID升序：{[e['tid'] for e in rss_entries_sorted]}")
    push_entries = rss_entries_sorted[:MAX_PUSH_PER_RUN]
    logging.info(f"本次推送{len(push_entries)}条：{[e['tid'] for e in push_entries]}")

    async with aiohttp.ClientSession() as session:
        existing_tids = load_sent_tids()
        newly_pushed_tids = []
        
        for i, entry in enumerate(push_entries):
            link = entry["link"]
            tid = entry["tid"]
            title = entry.get("title", "无标题").strip()
            author = entry.get("author", "未知用户").strip()
            post_delay = 5 if i > 0 else 0

            # 构造文字（全角@无跳转）
            caption = (
                f"{escape_markdown(title)}\n"
                f"由 ＠{escape_markdown(author)} 发起的话题讨论\n"
                f"链接：{link}\n\n"
                f"项目地址：{FIXED_PROJECT_URL}"
            )

            # 提取图片+分支发送
            images = await get_images_from_webpage(session, link)
            send_success = False
            if len(images) == 1:
                send_success = await send_single_photo_with_caption(session, images[0], caption, post_delay)
            elif 2 <= len(images) <= MAX_IMAGES_PER_MSG:
                send_success = await send_media_group(session, images, caption, post_delay)
            elif len(images) > MAX_IMAGES_PER_MSG:
                send_success = await send_media_group(session, images[:10], caption, post_delay)
            else:
                send_success = await send_text(session, caption, post_delay)

            if send_success:
                newly_pushed_tids.append(tid)
                logging.info(f"✅ 推送完成（TID：{tid}）")
            else:
                logging.warning(f"❌ 推送失败（TID：{tid}）")

    if newly_pushed_tids:
        save_sent_tids(newly_pushed_tids, existing_tids)
    else:
        logging.info("无成功推送，不更新记录")

# ====================== 11. 主函数 =======================
async def main():
    logging.info("===== SafeW RSS推送脚本启动 =====")
    config_check = True
    if not SAFEW_BOT_TOKEN or ":" not in SAFEW_BOT_TOKEN:
        logging.error("❌ BOT_TOKEN格式无效")
        config_check = False
    if not SAFEW_CHAT_ID:
        logging.error("❌ 未配置CHAT_ID")
        config_check = False
    if not RSS_FEED_URL:
        logging.error("❌ 未配置RSS_URL")
        config_check = False
    if not config_check:
        logging.error("❌ 配置错误，终止")
        return

    logging.info(f"aiohttp版本：{aiohttp.__version__}（推荐≥3.8.0）")
    try:
        await check_for_updates()
    except Exception as e:
        logging.error(f"❌ 核心逻辑异常：{str(e)}")
    logging.info("===== 脚本结束 =====")

if __name__ == "__main__":
    asyncio.run(main())
