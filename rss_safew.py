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
    ext = filename.lower().split(".")[-1]
    mime_map = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "png": "image/png", "gif": "image/gif", "webp": "image/webp"
    }
    return mime_map.get(ext, "image/jpeg")

def is_valid_image(data):
    if not data:
        return False
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

# ====================== TID提取/存储 =======================
def extract_tid_from_url(url):
    try:
        match = re.search(r'thread-(\d+)\.htm', url)
        if match:
            tid = int(match.group(1))
            logging.debug(f"提取TID：{url[:50]}... → {tid}")
            return tid
        logging.warning(f"无法提取TID：{url[:50]}...")
        return None
    except Exception as e:
        logging.error(f"提取TID失败：{str(e)}")
        return None

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
                logging.info(f"{SENT_POSTS_FILE}为空，返回空列表")
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
        logging.error(f"读取TID异常：{str(e)}")
        return []

def save_sent_tids(new_tids, existing_tids):
    try:
        all_tids = sorted(list(set(existing_tids + new_tids)))
        with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_tids, f, ensure_ascii=False, indent=2)
        logging.info(f"更新推送记录：新增{len(new_tids)}条，总{len(all_tids)}条")
    except Exception as e:
        logging.error(f"保存TID失败：{str(e)}")

# ====================== RSS获取与筛选 ======================
def fetch_updates():
    try:
        sent_tids = load_sent_tids()
        logging.info(f"开始筛选新帖（排除{len(sent_tids)}个TID）")
        
        feed = feedparser.parse(RSS_FEED_URL)
        if feed.bozo:
            logging.error(f"RSS解析失败：{feed.bozo_exception}")
            return None
        
        valid_entries = []
        for entry in feed.entries:
            link = entry.get("link", "").strip()
            if not link:
                logging.debug("跳过无链接的RSS条目")
                continue
            
            tid = extract_tid_from_url(link)
            if not tid:
                continue
            
            if tid not in sent_tids:
                entry["tid"] = tid
                valid_entries.append(entry)
                logging.debug(f"待推送：TID={tid}，标题：{entry.get('title', '无标题')[:30]}...")
            else:
                logging.debug(f"跳过已推送：TID={tid}")
        
        logging.info(f"筛选完成：{len(valid_entries)}条新帖待推送")
        # 按TID升序并限制数量
        sorted_entries = sorted(valid_entries, key=lambda x: x["tid"])[:MAX_PUSH_PER_RUN]
        logging.info(f"本次推送候选：{[e['tid'] for e in sorted_entries]}")
        return sorted_entries
    except Exception as e:
        logging.error(f"获取RSS异常：{str(e)}")
        return None

# ====================== 核心修复：恢复精准图片提取 =======================
async def get_images_from_webpage(session, webpage_url, tid):
    """恢复精准提取：只取帖子正文（class=message break-all isfirst=1）的图片"""
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": FIXED_PROJECT_URL,
            "Accept": "image/avif,image/webp,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9"
        }
        
        # 1. 请求帖子页面
        logging.info(f"开始提取TID={tid}的图片：{webpage_url[:50]}...")
        async with session.get(webpage_url, headers=headers, timeout=20) as resp:
            if resp.status != 200:
                logging.warning(f"TID={tid} 帖子请求失败（{resp.status}）")
                return []
            html = await resp.text()
        logging.debug(f"TID={tid} 成功获取帖子HTML")

        # 2. 精准定位帖子正文div（关键：只取首条消息的图片）
        soup = BeautifulSoup(html, "html.parser")
        # 恢复原筛选条件：class=message break-all 且 isfirst=1（首条消息）
        target_divs = soup.find_all("div", class_="message break-all", isfirst="1")
        if not target_divs:
            logging.warning(f"TID={tid} 未找到正文div（class=message break-all isfirst=1）")
            # 降级：尝试无isfirst属性的div（防止页面结构微调）
            target_divs = soup.find_all("div", class_="message break-all")
            if not target_divs:
                logging.warning(f"TID={tid} 无任何正文div")
                return []
            logging.warning(f"TID={tid} 降级提取：找到{len(target_divs)}个无isfirst的正文div")

        logging.info(f"TID={tid} 找到{len(target_divs)}个正文div，开始提取图片")
        images = []
        base_domain = "/".join(webpage_url.split("/")[:3])  # 提取域名（如https://tyw29.cc）

        # 3. 遍历div提取图片
        for div_idx, div in enumerate(target_divs):
            img_tags = div.find_all("img")
            logging.info(f"TID={tid} 第{div_idx+1}个正文div：找到{len(img_tags)}个img标签")
            
            for img in img_tags:
                # 优先取懒加载地址（data-src），再取src
                img_url = img.get("data-src", "").strip() or img.get("src", "").strip()
                
                # 过滤无效链接
                if not img_url:
                    logging.debug(f"TID={tid} 跳过空图片URL")
                    continue
                if img_url.startswith(("data:image/", "javascript:")):
                    logging.debug(f"TID={tid} 跳过无效图片URL：{img_url[:30]}...")
                    continue
                
                # 处理相对路径→绝对路径
                if img_url.startswith("/"):
                    img_url = f"{base_domain}{img_url}"
                    logging.debug(f"TID={tid} 相对路径转绝对路径：{img_url[:60]}...")
                elif not img_url.startswith(("http://", "https://")):
                    img_url = f"{base_domain}/{img_url}"
                    logging.debug(f"TID={tid} 补全域名：{img_url[:60]}...")
                
                # 去重并添加有效图片
                if img_url.startswith(("http://", "https://")) and img_url not in images:
                    images.append(img_url)
                    logging.info(f"TID={tid} 提取到有效图片{len(images)}：{img_url[:60]}...")

        # 4. 结果处理
        if not images:
            logging.warning(f"TID={tid} 未提取到任何有效图片")
            return []
        
        # 限制最多10张图
        final_images = images[:MAX_IMAGES_PER_MSG]
        logging.info(f"TID={tid} 图片提取完成：共{len(images)}张，保留前{len(final_images)}张")
        return final_images
    except Exception as e:
        logging.error(f"TID={tid} 图片提取异常：{str(e)}")
        return []

# ====================== Markdown转义 =======================
def escape_markdown(text):
    special_chars = r"_*~`>#+!()"
    for char in special_chars:
        if char in text:
            text = text.replace(char, f"\{char}")
    return text

# ====================== 单图发送 ========================
async def send_single_photo(session, image_url, caption, tid, delay=5):
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendPhoto"
        logging.info(f"\n=== TID={tid} 处理单图消息 ===")
        logging.info(f"图片URL：{image_url[:60]}...，文字：{caption[:50]}...")

        # 下载图片
        img_headers = {"User-Agent": USER_AGENT, "Referer": FIXED_PROJECT_URL}
        async with session.get(image_url, headers=img_headers, timeout=IMAGE_DOWNLOAD_TIMEOUT) as img_resp:
            if img_resp.status != 200:
                logging.error(f"TID={tid} 图片下载失败（{img_resp.status}）")
                return False
            img_data = await img_resp.read()
            if not is_valid_image(img_data):
                logging.error(f"TID={tid} 下载的不是有效图片")
                return False
            content_type = img_resp.headers.get("Content-Type") or get_image_content_type(image_url)
        logging.info(f"TID={tid} 图片信息：{len(img_data)}字节，类型{content_type}")

        # 构造请求体
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

        # 发送
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": USER_AGENT}
        async with session.post(api_url, data=body, headers=headers, timeout=MSG_SEND_TIMEOUT) as resp:
            resp_text = await resp.text()
            if resp.status == 200:
                logging.info(f"TID={tid} ✅ 单图消息发送成功")
                return True
            logging.error(f"TID={tid} ❌ 单图失败（{resp.status}）：{resp_text[:200]}...")
            return False
    except Exception as e:
        logging.error(f"TID={tid} 单图发送异常：{str(e)}")
        return False

# ====================== 多图发送（保持之前的修复）=======================
async def send_media_group(session, image_urls, caption, tid, delay=5):
    if len(image_urls) < 2 or len(image_urls) > MAX_IMAGES_PER_MSG:
        logging.error(f"TID={tid} 多图数量无效（需2-{MAX_IMAGES_PER_MSG}张）")
        return False
    
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendMediaGroup"
        logging.info(f"\n=== TID={tid} 处理多图消息（{len(image_urls)}张）===")
        logging.info(f"文字说明：{caption[:50]}...")

        # 下载图片并验证
        media_data = []
        for idx, img_url in enumerate(image_urls, 1):
            filename = f"media_{tid}_{idx}_{uuid.uuid4().hex[:8]}.jpg"
            try:
                headers = {"User-Agent": USER_AGENT, "Referer": FIXED_PROJECT_URL}
                async with session.get(img_url, headers=headers, timeout=IMAGE_DOWNLOAD_TIMEOUT) as resp:
                    if resp.status != 200:
                        logging.error(f"TID={tid} 图片{idx}下载失败（{resp.status}）")
                        return False
                    img_data = await resp.read()
                if not is_valid_image(img_data):
                    logging.error(f"TID={tid} 图片{idx}无效")
                    return False
                content_type = resp.headers.get("Content-Type") or get_image_content_type(img_url)
                media_data.append((img_data, content_type, filename))
                logging.info(f"TID={tid} 图片{idx}：{len(img_data)}字节，类型{content_type}")
            except Exception as e:
                logging.error(f"TID={tid} 图片{idx}处理失败：{str(e)}")
                return False

        # 构造media数组
        media_array = []
        for idx, (_, content_type, filename) in enumerate(media_data):
            media_item = {
                "type": "photo",
                "media": f"attach://{filename}",
                "parse_mode": "Markdown",
                "disable_web_page_preview": True
            }
            if idx == 0:
                media_item["caption"] = caption
            media_array.append(media_item)

        # 构造请求体
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
        for img_data, content_type, filename in media_data:
            body_parts.extend([
                f"--{boundary}".encode("utf-8"),
                f'Content-Disposition: form-data; name="{filename}"; filename="{filename}"'.encode("utf-8"),
                f"Content-Type: {content_type}".encode("utf-8"),
                b'',
                img_data
            ])
        body_parts.append(f"--{boundary}--".encode("utf-8"))
        body = b"\r\n".join(body_parts)
        logging.info(f"TID={tid} 请求体构造完成：{len(body)}字节")

        # 发送
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
            "Content-Length": str(len(body))
        }
        async with session.post(api_url, data=body, headers=headers, timeout=MSG_SEND_TIMEOUT) as resp:
            resp_text = await resp.text()
            if resp.status == 200:
                logging.info(f"TID={tid} ✅ 多图消息发送成功")
                return True
            logging.error(f"TID={tid} ❌ 多图失败（{resp.status}）：{resp_text[:200]}...")
            return False
    except Exception as e:
        logging.error(f"TID={tid} 多图发送异常：{str(e)}")
        return False

# ====================== 纯文本发送 ========================
async def send_text_msg(session, caption, tid, delay=5):
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendMessage"
        logging.info(f"\n=== TID={tid} 处理纯文本消息 ===")
        logging.info(f"文本内容：{caption[:50]}...")

        payload = {
            "chat_id": SAFEW_CHAT_ID,
            "text": caption,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        async with session.post(api_url, json=payload, timeout=MSG_SEND_TIMEOUT) as resp:
            resp_text = await resp.text()
            if resp.status == 200:
                logging.info(f"TID={tid} ✅ 纯文本发送成功")
                return True
            logging.error(f"TID={tid} ❌ 文本失败（{resp.status}）：{resp_text[:200]}...")
            return False
    except Exception as e:
        logging.error(f"TID={tid} 文本发送异常：{str(e)}")
        return False

# ====================== 核心推送逻辑（增加图片提取日志）======================
async def check_for_updates():
    rss_entries = fetch_updates()
    if not rss_entries:
        logging.info("无新帖待推送，结束")
        return

    async with aiohttp.ClientSession() as session:
        existing_tids = load_sent_tids()
        newly_pushed = []
        
        for i, entry in enumerate(rss_entries):
            link = entry["link"]
            tid = entry["tid"]
            title = entry.get("title", "无标题").strip()
            author = entry.get("author", "未知用户").strip()
            post_delay = 5 if i > 0 else 0  # 帖子间间隔

            # 构造文字内容
            caption = (
                f"{escape_markdown(title)}\n"
                f"由 ＠{escape_markdown(author)} 发起的话题讨论\n"
                f"链接：{link}\n\n"
                f"项目地址：{FIXED_PROJECT_URL}"
            )

            # 提取图片（传入TID，便于日志定位）
            images = await get_images_from_webpage(session, link, tid)
            # 关键日志：显示提取到的图片数量
            logging.info(f"TID={tid} 图片提取结果：共{len(images)}张有效图片")

            # 分支发送
            success = False
            if len(images) == 1:
                success = await send_single_photo(session, images[0], caption, tid, post_delay)
            elif 2 <= len(images) <= MAX_IMAGES_PER_MSG:
                success = await send_media_group(session, images, caption, tid, post_delay)
            else:
                success = await send_text_msg(session, caption, tid, post_delay)

            if success:
                newly_pushed.append(tid)
                logging.info(f"TID={tid} ✅ 推送完成")
            else:
                logging.warning(f"TID={tid} ❌ 推送失败")

    if newly_pushed:
        save_sent_tids(newly_pushed, existing_tids)
    else:
        logging.info("无成功推送的帖子，不更新记录")

# ====================== 主函数 =======================
async def main():
    logging.info("===== SafeW RSS推送脚本启动 =====")
    # 配置校验
    config_check = True
    if not SAFEW_BOT_TOKEN or ":" not in SAFEW_BOT_TOKEN:
        logging.error("❌ SAFEW_BOT_TOKEN格式无效")
        config_check = False
    if not SAFEW_CHAT_ID:
        logging.error("❌ 未配置SAFEW_CHAT_ID")
        config_check = False
    if not RSS_FEED_URL:
        logging.error("❌ 未配置RSS_FEED_URL")
        config_check = False
    if not config_check:
        logging.error("❌ 基础配置错误，终止")
        return

    try:
        await check_for_updates()
    except Exception as e:
        logging.error(f"❌ 核心逻辑异常：{str(e)}")
    logging.info("===== 脚本结束 =====")

if __name__ == "__main__":
    asyncio.run(main())
