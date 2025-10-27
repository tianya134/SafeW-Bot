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

# ====================== 工具函数 =======================
def get_image_content_type(filename):
    """根据文件名推测Content-Type"""
    ext = filename.lower().split(".")[-1]
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp"
    }
    return mime_map.get(ext, "image/jpeg")

def is_valid_image(data):
    """验证二进制数据是否为有效图片（检查文件头）"""
    if not data:
        return False
    # 常见图片文件头签名
    signatures = {
        b"\xff\xd8\xff": "jpeg",
        b"\x89\x50\x4e\x47": "png",
        b"\x47\x49\x46\x38": "gif",
        b"\x52\x49\x46\x46": "webp"
    }
    for sig, _ in signatures.items():
        if data.startswith(sig):
            return True
    logging.warning(f"无效图片文件头：{data[:8].hex()}")
    return False

# ====================== TID提取/存储/RSS等基础函数（不变）======================
def extract_tid_from_url(url):
    try:
        match = re.search(r'thread-(\d+)\.htm', url)
        return int(match.group(1)) if match else None
    except Exception as e:
        logging.error(f"提取TID失败：{str(e)}")
        return None

def load_sent_tids():
    try:
        if not os.path.exists(SENT_POSTS_FILE):
            with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
                json.dump([], f)
            return []
        with open(SENT_POSTS_FILE, "r", encoding="utf-8") as f:
            tids = json.loads(f.read().strip() or "[]")
            return [int(t) for t in tids if isinstance(t, int)]
    except Exception as e:
        logging.error(f"读取TID失败：{str(e)}")
        return []

def save_sent_tids(new_tids, existing_tids):
    try:
        all_tids = sorted(list(set(existing_tids + new_tids)))
        with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_tids, f, ensure_ascii=False, indent=2)
        logging.info(f"更新推送记录：新增{len(new_tids)}条，总{len(all_tids)}条")
    except Exception as e:
        logging.error(f"保存TID失败：{str(e)}")

def fetch_updates():
    try:
        sent_tids = load_sent_tids()
        feed = feedparser.parse(RSS_FEED_URL)
        if feed.bozo:
            logging.error(f"RSS解析失败：{feed.bozo_exception}")
            return None
        valid = [e for e in feed.entries if extract_tid_from_url(e.get("link", "")) not in sent_tids]
        for e in valid:
            e["tid"] = extract_tid_from_url(e["link"])
        logging.info(f"筛选新帖：{len(valid)}条")
        return sorted(valid, key=lambda x: x["tid"])[:MAX_PUSH_PER_RUN]
    except Exception as e:
        logging.error(f"获取RSS异常：{str(e)}")
        return None

async def get_images_from_webpage(session, webpage_url):
    try:
        headers = {"User-Agent": USER_AGENT, "Referer": FIXED_PROJECT_URL}
        async with session.get(webpage_url, headers=headers, timeout=20) as resp:
            if resp.status != 200:
                return []
            html = await resp.text()
        soup = BeautifulSoup(html, "html.parser")
        imgs = [img.get("data-src") or img.get("src") for img in soup.find_all("img")]
        imgs = [img for img in imgs if img and not img.startswith(("data:image/", "javascript:"))]
        base = "/".join(webpage_url.split("/")[:3])
        imgs = [f"{base}{img}" if img.startswith("/") else img for img in imgs]
        return [img for img in imgs if img.startswith(("http", "https"))][:MAX_IMAGES_PER_MSG]
    except Exception as e:
        logging.error(f"提取图片异常：{str(e)}")
        return []

def escape_markdown(text):
    return re.sub(r'([_*~`>#+!()])', r'\\\1', text)

# ====================== 单图发送（不变，仅作兼容）=======================
async def send_single_photo(session, image_url, caption, delay=5):
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendPhoto"
        async with session.get(image_url, headers={"User-Agent": USER_AGENT}, timeout=15) as resp:
            img_data = await resp.read()
            if not is_valid_image(img_data):
                logging.error("下载的不是有效图片")
                return False
            content_type = resp.headers.get("Content-Type") or get_image_content_type(image_url)
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
        filename = f"single_{uuid.uuid4().hex[:8]}.jpg"
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
                logging.info("✅ 单图发送成功")
                return True
            logging.error(f"❌ 单图失败：{await resp.text()[:200]}")
            return False
    except Exception as e:
        logging.error(f"单图发送异常：{str(e)}")
        return False

# ====================== 多图发送（终极修复：对齐InputMediaPhoto规范）=======================
async def send_media_group(session, image_urls, caption, delay=5):
    if len(image_urls) < 2 or len(image_urls) > MAX_IMAGES_PER_MSG:
        logging.error(f"多图数量无效（需2-{MAX_IMAGES_PER_MSG}张）")
        return False
    
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendMediaGroup"
        logging.info(f"\n=== 处理多图消息（{len(image_urls)}张）===")

        # 1. 下载图片并验证有效性（关键：确保下载的是真实图片）
        media_data = []  # 存储 (img_data, content_type, filename)
        for idx, img_url in enumerate(image_urls, 1):
            filename = f"media_{idx}_{uuid.uuid4().hex[:8]}.jpg"
            try:
                # 下载图片（加Referer防防盗链）
                headers = {"User-Agent": USER_AGENT, "Referer": FIXED_PROJECT_URL}
                async with session.get(img_url, headers=headers, timeout=IMAGE_DOWNLOAD_TIMEOUT) as resp:
                    if resp.status != 200:
                        logging.error(f"图片{idx}下载失败（{resp.status}）")
                        return False
                    img_data = await resp.read()
                
                # 验证图片有效性（检查文件头）
                if not is_valid_image(img_data):
                    logging.error(f"图片{idx}无效（非图片格式）")
                    return False
                
                # 确定Content-Type
                content_type = resp.headers.get("Content-Type") or get_image_content_type(img_url)
                media_data.append((img_data, content_type, filename))
                logging.info(f"图片{idx}：{len(img_data)}字节，类型{content_type}，文件名{filename}")
            except Exception as e:
                logging.error(f"图片{idx}处理失败：{str(e)}")
                return False

        # 2. 构造media数组（严格对齐InputMediaPhoto规范）
        media_array = []
        for idx, (_, content_type, filename) in enumerate(media_data):
            media_item = {
                "type": "photo",  # 固定图片类型
                "media": f"attach://{filename}",  # 关联图片文件
                "parse_mode": "Markdown",  # 匹配caption的Markdown格式
                "disable_web_page_preview": True  # 禁用链接预览
            }
            # 仅在第一个元素添加caption（全局唯一caption）
            if idx == 0:
                media_item["caption"] = caption  # 把全局caption嵌入第一个元素
            media_array.append(media_item)

        # 3. 构造请求体（核心：图片字段name与attach后的filename完全一致）
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
        body_parts = []

        # 3.1 必选字段：chat_id
        body_parts.extend([
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="chat_id"',
            b'',
            str(SAFEW_CHAT_ID).encode("utf-8")
        ])

        # 3.2 核心：media数组（JSON格式）
        body_parts.extend([
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="media"',
            b'Content-Type: application/json',
            b'',
            json.dumps(media_array, ensure_ascii=False).encode("utf-8")
        ])

        # 3.3 图片文件字段（name=filename，与attach完全匹配）
        for img_data, content_type, filename in media_data:
            body_parts.extend([
                f"--{boundary}".encode("utf-8"),
                # 关键修复：name字段值 = attach后的filename
                f'Content-Disposition: form-data; name="{filename}"; filename="{filename}"'.encode("utf-8"),
                f"Content-Type: {content_type}".encode("utf-8"),
                b'',
                img_data  # 原始图片二进制数据
            ])

        # 3.4 结束符
        body_parts.append(f"--{boundary}--".encode("utf-8"))

        # 4. 拼接请求体（字节流，无编码转换）
        body = b"\r\n".join(body_parts)
        logging.info(f"请求体构造完成：大小{len(body)}字节，含{len(media_data)}张图片")

        # 5. 发送请求（加Content-Length确保完整传输）
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
            "Content-Length": str(len(body))
        }
        async with session.post(api_url, data=body, headers=headers, timeout=MSG_SEND_TIMEOUT) as resp:
            resp_text = await resp.text(encoding="utf-8", errors="replace")
            resp_summary = resp_text[:200] + "..." if len(resp_text) > 200 else resp_text
            if resp.status == 200:
                logging.info(f"✅ 多图消息发送成功（{len(media_data)}张）")
                return True
            logging.error(f"❌ 多图失败（{resp.status}）：{resp_summary}")
            return False
    except Exception as e:
        logging.error(f"多图发送总异常：{str(e)}")
        return False

# ====================== 纯文本发送（不变）=======================
async def send_text_msg(session, caption, delay=5):
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
                logging.info("✅ 纯文本发送成功")
                return True
            logging.error(f"❌ 文本失败：{await resp.text()[:200]}")
            return False
    except Exception as e:
        logging.error(f"文本发送异常：{str(e)}")
        return False

# ====================== 核心推送逻辑（不变）======================
async def check_for_updates():
    rss_entries = fetch_updates()
    if not rss_entries:
        logging.info("无新帖，结束")
        return

    async with aiohttp.ClientSession() as session:
        existing_tids = load_sent_tids()
        newly_pushed = []
        for i, entry in enumerate(rss_entries):
            link = entry["link"]
            tid = entry["tid"]
            title = escape_markdown(entry.get("title", "无标题"))
            author = escape_markdown(entry.get("author", "未知用户"))
            caption = (
                f"{title}\n"
                f"由 ＠{author} 发起的话题讨论\n"
                f"链接：{link}\n\n"
                f"项目地址：{FIXED_PROJECT_URL}"
            )

            images = await get_images_from_webpage(session, link)
            success = False
            if len(images) == 1:
                success = await send_single_photo(session, images[0], caption, 5 if i > 0 else 0)
            elif 2 <= len(images) <= MAX_IMAGES_PER_MSG:
                success = await send_media_group(session, images, caption, 5 if i > 0 else 0)
            else:
                success = await send_text_msg(session, caption, 5 if i > 0 else 0)

            if success:
                newly_pushed.append(tid)
                logging.info(f"✅ 推送完成（TID：{tid}）")
            else:
                logging.warning(f"❌ 推送失败（TID：{tid}）")

    if newly_pushed:
        save_sent_tids(newly_pushed, existing_tids)

# ====================== 主函数 =======================
async def main():
    logging.info("===== SafeW RSS推送脚本启动 =====")
    if not all([SAFEW_BOT_TOKEN, SAFEW_CHAT_ID, RSS_FEED_URL]):
        logging.error("❌ 缺少环境配置，终止")
        return
    try:
        await check_for_updates()
    except Exception as e:
        logging.error(f"❌ 核心异常：{str(e)}")
    logging.info("===== 脚本结束 =====")

if __name__ == "__main__":
    asyncio.run(main())
