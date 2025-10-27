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
SAFEW_BOT_TOKEN = os.getenv("SAFEW_BOT_TOKEN")       # 机器人令牌（格式：数字:字符）
SAFEW_CHAT_ID = os.getenv("SAFEW_CHAT_ID")           # 目标群组ID（整数/字符串）
RSS_FEED_URL = os.getenv("RSS_FEED_URL")             # RSS源地址
SENT_POSTS_FILE = "sent_posts.json"                  # 已推送TID存储文件
MAX_PUSH_PER_RUN = 5                                 # 单次最多推送帖子数
FIXED_PROJECT_URL = "https://tyw29.cc/"              # 项目固定域名
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
MAX_IMAGES_PER_MSG = 10                              # 单条消息最多图片数（API限制2-10）
IMAGE_DOWNLOAD_TIMEOUT = 15                          # 图片下载超时（秒）
MSG_SEND_TIMEOUT = 30                                # 消息发送超时（秒）

# ====================== 日志配置 =======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ====================== 1. TID提取 =======================
def extract_tid_from_url(url):
    """从帖子URL提取TID（thread-xxx.htm格式）"""
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

# ====================== 2. 已推送TID存储/读取 ======================
def load_sent_tids():
    """读取已推送的TID列表（sent_posts.json）"""
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
            # 校验格式（整数列表）
            if not isinstance(tids, list) or not all(isinstance(t, int) for t in tids):
                logging.error(f"{SENT_POSTS_FILE}格式错误（非整数列表），重置为空")
                with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
                    json.dump([], f)
                return []
            
            logging.info(f"读取到{len(tids)}条已推送TID：{tids[:5]}...")
            return tids
    except json.JSONDecodeError:
        logging.error(f"{SENT_POSTS_FILE}解析失败（JSON格式错误），重置为空")
        with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
            json.dump([], f)
        return []
    except Exception as e:
        logging.error(f"读取已推送TID异常：{str(e)}，返回空列表")
        return []

def save_sent_tids(new_tids, existing_tids):
    """合并新推送TID到已有列表（去重后保存）"""
    try:
        # 去重+排序
        all_tids = list(set(existing_tids + new_tids))
        all_tids_sorted = sorted(all_tids)
        
        with open(SENT_POSTS_FILE, "w", encoding="utf-8") as f:
            json.dump(all_tids_sorted, f, ensure_ascii=False, indent=2)
        
        logging.info(f"更新推送记录：新增{len(new_tids)}条，总记录{len(all_tids_sorted)}条")
    except Exception as e:
        logging.error(f"保存推送记录失败：{str(e)}")

# ====================== 3. RSS获取与新帖筛选 ======================
def fetch_updates():
    """获取RSS源，筛选未推送的新帖（TID不在已推送列表）"""
    try:
        sent_tids = load_sent_tids()
        logging.info(f"开始筛选新帖（排除{len(sent_tids)}个已推送TID）")
        
        # 解析RSS
        feed = feedparser.parse(RSS_FEED_URL)
        if feed.bozo:
            logging.error(f"RSS解析失败：{feed.bozo_exception}")
            return None
        
        # 筛选有效新帖
        valid_entries = []
        for entry in feed.entries:
            link = entry.get("link", "").strip()
            if not link:
                logging.debug("跳过无链接的RSS条目")
                continue
            
            tid = extract_tid_from_url(link)
            if not tid:
                continue
            
            # 仅保留未推送的TID
            if tid not in sent_tids:
                entry["tid"] = tid
                valid_entries.append(entry)
                logging.debug(f"新增待推送：TID={tid}，标题：{entry.get('title', '无标题')[:30]}...")
            else:
                logging.debug(f"跳过已推送：TID={tid}")
        
        logging.info(f"筛选完成：共{len(valid_entries)}条新帖待推送")
        return valid_entries
    except Exception as e:
        logging.error(f"获取RSS异常：{str(e)}")
        return None

# ====================== 4. 网页图片提取（返回全部有效图片）======================
async def get_images_from_webpage(session, webpage_url):
    """从帖子页面提取所有有效图片（处理懒加载/相对路径）"""
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": FIXED_PROJECT_URL,
            "Accept": "image/avif,image/webp,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9"
        }
        
        # 请求帖子页面
        async with session.get(webpage_url, headers=headers, timeout=20) as resp:
            if resp.status != 200:
                logging.warning(f"帖子请求失败（状态码：{resp.status}）：{webpage_url[:50]}...")
                return []
            html = await resp.text()
        
        # 解析图片标签
        soup = BeautifulSoup(html, "html.parser")
        target_divs = soup.find_all("div", class_="message break-all", isfirst="1")
        if not target_divs:
            logging.warning(f"未找到图片所在的目标div：{webpage_url[:50]}...")
            return []
        
        images = []
        base_domain = "/".join(webpage_url.split("/")[:3])  # 提取域名（如https://tyw29.cc）
        
        for div in target_divs:
            img_tags = div.find_all("img")
            logging.info(f"目标div找到{len(img_tags)}个img标签（TID：{extract_tid_from_url(webpage_url)}）")
            
            for img in img_tags:
                # 优先取懒加载地址（data-src），再取src
                img_url = img.get("data-src", "").strip() or img.get("src", "").strip()
                
                # 过滤无效链接（base64/JS链接）
                if not img_url or img_url.startswith(("data:image/", "javascript:")):
                    continue
                
                # 处理相对路径
                if img_url.startswith("/"):
                    img_url = f"{base_domain}{img_url}"
                elif not img_url.startswith(("http://", "https://")):
                    img_url = f"{base_domain}/{img_url}"
                
                # 去重并添加有效URL
                if img_url.startswith(("http://", "https://")) and img_url not in images:
                    images.append(img_url)
                    logging.info(f"提取到图片{len(images)}：{img_url[:60]}...")
        
        # 返回所有图片（最多MAX_IMAGES_PER_MSG张）
        final_images = images[:MAX_IMAGES_PER_MSG]
        logging.info(f"从{webpage_url[:50]}...提取{len(images)}张图片，最终保留{len(final_images)}张")
        return final_images
    except Exception as e:
        logging.error(f"提取图片异常：{str(e)}，URL：{webpage_url[:50]}...")
        return []

# ====================== 5. Markdown特殊字符转义 ======================
def escape_markdown(text):
    """转义Markdown格式字符（避免文本错乱，不转义@）"""
    special_chars = r"_*~`>#+!()"
    for char in special_chars:
        if char in text:
            text = text.replace(char, f"\{char}")
    return text

# ====================== 6. 消息发送：单图+文字（sendPhoto）=======================
async def send_single_photo_with_caption(session, image_url, caption, delay=5):
    """单张图片时用sendPhoto发送（带文字说明）"""
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendPhoto"
        logging.info(f"\n=== 处理单图消息 ===")
        logging.info(f"图片URL：{image_url[:60]}...")
        logging.info(f"文字说明：{caption[:50]}...")

        # 1. 下载图片二进制数据
        img_headers = {
            "User-Agent": USER_AGENT,
            "Referer": FIXED_PROJECT_URL
        }
        async with session.get(
            image_url,
            headers=img_headers,
            timeout=IMAGE_DOWNLOAD_TIMEOUT,
            ssl=False
        ) as img_resp:
            if img_resp.status != 200:
                logging.error(f"图片下载失败（状态码：{img_resp.status}）")
                return False
            img_data = await img_resp.read()
            img_content_type = img_resp.headers.get("Content-Type", "image/jpeg")
            logging.info(f"图片下载成功：大小{len(img_data)}字节，类型{img_content_type}")

        # 2. 构造multipart/form-data请求体（改用字节流拼接，避免编码冲突）
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
        chat_id_str = str(SAFEW_CHAT_ID)
        # 生成纯英文文件名（避免中文编码问题）
        filename = f"single_img_{uuid.uuid4().hex[:8]}.jpg"

        # 文本部分（utf-8编码为字节流）
        text_parts = [
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="chat_id"',
            b'',
            chat_id_str.encode("utf-8"),
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="caption"',
            b'',
            caption.encode("utf-8"),
            f"--{boundary}".encode("utf-8"),
            f'Content-Disposition: form-data; name="photo"; filename="{filename}"'.encode("utf-8"),
            f"Content-Type: {img_content_type}".encode("utf-8"),
            b'',
            img_data,  # 图片二进制数据直接加入
            f"--{boundary}--".encode("utf-8")
        ]

        # 拼接请求体（用\r\n分隔字节流）
        body = b'\r\n'.join(text_parts)

        # 3. 发送请求
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
            "Content-Length": str(len(body))
        }
        async with session.post(
            api_url,
            data=body,
            headers=headers,
            timeout=MSG_SEND_TIMEOUT,
            ssl=False
        ) as response:
            response_text = await response.text(encoding="utf-8", errors="replace")
            if response.status == 200:
                logging.info("✅ 单图+文字消息发送成功")
                return True
            logging.error(f"❌ 单图消息发送失败（{response.status}）：{response_text[:150]}...")
            return False
    except Exception as e:
        logging.error(f"❌ 单图消息发送异常：{str(e)}")
        return False

# ====================== 7. 消息发送：多图+文字（sendMediaGroup，2-10张）=======================
async def send_media_group(session, image_urls, caption, delay=5):
    """多图时用sendMediaGroup发送（单条消息，2-10张图+全局文字）"""
    # 校验图片数量（符合官方API要求）
    if len(image_urls) < 2 or len(image_urls) > MAX_IMAGES_PER_MSG:
        logging.error(f"❌ 多图数量无效：需2-{MAX_IMAGES_PER_MSG}张，当前{len(image_urls)}张")
        return False
    
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendMediaGroup"
        logging.info(f"\n=== 处理多图消息（共{len(image_urls)}张）===")
        logging.info(f"文字说明：{caption[:50]}...")

        # 1. 批量下载所有图片
        img_datas = []
        for idx, img_url in enumerate(image_urls, 1):
            try:
                img_headers = {
                    "User-Agent": USER_AGENT,
                    "Referer": FIXED_PROJECT_URL
                }
                async with session.get(
                    img_url,
                    headers=img_headers,
                    timeout=IMAGE_DOWNLOAD_TIMEOUT,
                    ssl=False
                ) as img_resp:
                    if img_resp.status != 200:
                        logging.error(f"图片{idx}下载失败（{img_resp.status}）：{img_url[:50]}...")
                        return False
                    img_data = await img_resp.read()
                    img_datas.append({
                        "data": img_data,
                        "content_type": img_resp.headers.get("Content-Type", "image/jpeg"),
                        "filename": f"multi_img_{idx}_{uuid.uuid4().hex[:8]}.jpg"  # 纯英文文件名
                    })
                logging.info(f"图片{idx}下载成功：大小{len(img_data)}字节")
            except Exception as e:
                logging.error(f"图片{idx}下载异常：{str(e)}，URL：{img_url[:50]}...")
                return False

        # 2. 生成multipart分隔符
        boundary = f"----WebKitFormBoundary{uuid.uuid4().hex[:16]}"
        chat_id_str = str(SAFEW_CHAT_ID)
        body_parts = []  # 存储字节流片段

        # 3. 添加必填字段（chat_id + caption）- 字节流格式（utf-8编码）
        body_parts.extend([
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="chat_id"',
            b'',
            chat_id_str.encode("utf-8"),
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="caption"',
            b'',
            caption.encode("utf-8")  # 中文caption用utf-8编码
        ])

        # 4. 构造media数组（InputMediaPhoto）+ 图片文件字段（字节流拼接）
        media_array = []
        for idx, img in enumerate(img_datas, 1):
            # media数组元素：关联图片文件（attach://文件名）
            media_array.append({
                "type": "photo",
                "media": f"attach://{img['filename']}"
            })
            # 添加图片文件字段（文本部分utf-8编码，二进制数据直接加入）
            body_parts.extend([
                f"--{boundary}".encode("utf-8"),
                f'Content-Disposition: form-data; name="photo{idx}"; filename="{img["filename"]}"'.encode("utf-8"),
                f"Content-Type: {img['content_type']}".encode("utf-8"),
                b'',
                img["data"]  # 图片二进制数据直接作为字节流加入
            ])

        # 5. 添加media数组JSON字段（utf-8编码）
        body_parts.extend([
            f"--{boundary}".encode("utf-8"),
            b'Content-Disposition: form-data; name="media"',
            b'Content-Type: application/json',
            b'',
            json.dumps(media_array, ensure_ascii=False).encode("utf-8")
        ])

        # 6. 结束符（字节流）
        body_parts.append(f"--{boundary}--".encode("utf-8"))

        # 7. 拼接请求体（用\r\n分隔所有字节流片段，彻底避免编码转换）
        body = b'\r\n'.join(body_parts)
        logging.info(f"请求体构造完成：总大小{len(body)}字节")

        # 8. 发送请求
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": USER_AGENT,
            "Content-Length": str(len(body))
        }
        async with session.post(
            api_url,
            data=body,
            headers=headers,
            timeout=MSG_SEND_TIMEOUT,
            ssl=False
        ) as response:
            response_text = await response.text(encoding="utf-8", errors="replace")
            response_summary = response_text[:150] + "..." if len(response_text) > 150 else response_text
            if response.status == 200:
                logging.info(f"✅ 多图消息发送成功（{len(image_urls)}张图）")
                return True
            logging.error(f"❌ 多图消息发送失败（{response.status}）：{response_summary}")
            return False
    except Exception as e:
        logging.error(f"❌ 多图消息发送总异常：{str(e)}")
        return False

# ====================== 8. 消息发送：纯文本（无图时）=======================
async def send_text(session, caption, delay=5):
    """无图片时发送纯文本消息"""
    try:
        await asyncio.sleep(delay)
        api_url = f"https://api.safew.org/bot{SAFEW_BOT_TOKEN}/sendMessage"
        logging.info(f"\n=== 处理纯文本消息 ===")
        logging.info(f"文本内容：{caption[:50]}...")

        # 构造请求体
        payload = {
            "chat_id": SAFEW_CHAT_ID,
            "text": caption,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,  # 禁用链接预览
            "disable_notification": False     # 启用消息通知
        }

        # 发送请求
        async with session.post(
            api_url,
            json=payload,
            timeout=MSG_SEND_TIMEOUT,
            ssl=False
        ) as response:
            response_text = await response.text(encoding="utf-8", errors="replace")
            if response.status == 200:
                logging.info("✅ 纯文本消息发送成功")
                return True
            logging.error(f"❌ 纯文本消息发送失败（{response.status}）：{response_text[:150]}...")
            return False
    except Exception as e:
        logging.error(f"❌ 纯文本消息发送异常：{str(e)}")
        return False

# ====================== 9. 核心推送逻辑（按图片数量分支处理）======================
async def check_for_updates():
    """检查新帖、分支发送消息、更新推送记录"""
    # 获取待推送新帖
    rss_entries = fetch_updates()
    if not rss_entries:
        logging.info("无新帖待推送，脚本结束")
        return

    # 按TID升序排序（顺序浏览）
    rss_entries_sorted = sorted(rss_entries, key=lambda x: x["tid"])
    logging.info(f"新帖按TID升序：{[e['tid'] for e in rss_entries_sorted]}")

    # 限制单次推送数量
    push_entries = rss_entries_sorted[:MAX_PUSH_PER_RUN]
    logging.info(f"本次推送{len(push_entries)}条帖子：{[e['tid'] for e in push_entries]}")

    # 异步发送
    async with aiohttp.ClientSession() as session:
        existing_tids = load_sent_tids()
        newly_pushed_tids = []  # 记录本次推送成功的TID
        
        for i, entry in enumerate(push_entries):
            link = entry.get("link", "").strip()
            tid = entry["tid"]
            title = entry.get("title", "无标题").strip()
            author = entry.get("author", entry.get("dc_author", "未知用户")).strip()
            # 不同帖子间的发送间隔（避免频率限制）
            post_delay = 5 if i > 0 else 0

            # 1. 构造文字内容（全角＠避免跳转）
            title_escaped = escape_markdown(title)
            author_escaped = escape_markdown(author)
            caption = (
                f"{title_escaped}\n"
                f"由 ＠{author_escaped} 发起的话题讨论\n"
                f"链接：{link}\n\n"
                f"项目地址：{FIXED_PROJECT_URL}"
            )

            # 2. 提取图片
            images = await get_images_from_webpage(session, link)
            send_success = False

            # 3. 按图片数量分支发送
            if len(images) == 1:
                # 单图：sendPhoto
                send_success = await send_single_photo_with_caption(session, images[0], caption, post_delay)
            elif 2 <= len(images) <= MAX_IMAGES_PER_MSG:
                # 多图（2-10张）：sendMediaGroup
                send_success = await send_media_group(session, images, caption, post_delay)
            elif len(images) > MAX_IMAGES_PER_MSG:
                # 超10张：取前10张用多图接口
                send_success = await send_media_group(session, images[:MAX_IMAGES_PER_MSG], caption, post_delay)
            else:
                # 无图：纯文本
                send_success = await send_text(session, caption, post_delay)

            # 4. 记录成功推送的TID
            if send_success:
                newly_pushed_tids.append(tid)
                logging.info(f"✅ 帖子推送完成（TID：{tid}）")
            else:
                logging.warning(f"❌ 帖子推送失败（TID：{tid}）")

    # 5. 更新推送记录
    if newly_pushed_tids:
        save_sent_tids(newly_pushed_tids, existing_tids)
    else:
        logging.info("无成功推送的帖子，不更新记录")

# ====================== 10. 主函数（脚本入口）======================
async def main():
    logging.info("===== SafeW RSS推送脚本启动 =====")
    
    # 基础配置校验
    config_check = True
    if not SAFEW_BOT_TOKEN or ":" not in SAFEW_BOT_TOKEN:
        logging.error("❌ 错误：SAFEW_BOT_TOKEN格式无效（应为「数字:字符」）")
        config_check = False
    if not SAFEW_CHAT_ID:
        logging.error("❌ 错误：未配置SAFEW_CHAT_ID（目标群组ID）")
        config_check = False
    if not RSS_FEED_URL:
        logging.error("❌ 错误：未配置RSS_FEED_URL（RSS源地址）")
        config_check = False
    if not config_check:
        logging.error("❌ 基础配置错误，脚本终止")
        return

    # 依赖版本提示
    logging.info(f"当前aiohttp版本：{aiohttp.__version__}（推荐≥3.8.0）")
    if aiohttp.__version__ < "3.8.0":
        logging.warning("⚠️ 警告：aiohttp版本过低，可能存在兼容问题")

    # 执行推送逻辑
    try:
        await check_for_updates()
    except Exception as e:
        logging.error(f"❌ 核心推送逻辑异常：{str(e)}")
    
    logging.info("===== SafeW RSS推送脚本结束 =====")

if __name__ == "__main__":
    asyncio.run(main())
