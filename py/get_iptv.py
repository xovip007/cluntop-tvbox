import re
import os
import unicodedata
import requests
import logging
import shutil
import threading
from collections import OrderedDict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# === 配置日志 ===
def setup_logger():
    # 确保日志目录存在
    os.makedirs("logs", exist_ok=True)
    
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.handlers.clear() # 清除已有 handler 避免重复
    
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件输出
    file_handler = logging.FileHandler("logs/iptv_update.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger

logger = setup_logger()

# 全局锁，用于文件写入
write_lock = threading.Lock()

def ensure_dir(file_path):
    """确保文件所在的目录存在"""
    dirname = os.path.dirname(file_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)

_DASH_VARIANTS_RE = re.compile(r'[－—﹣–─]')
_MULTI_SPACE_RE = re.compile(r'\s+')

def normalize_channel_name(name):
    """
    频道名称归一化，用于匹配前的预处理（不用于最终显示名）：
    - 全角字符转半角（如 ＣＣＴＶ１ -> CCTV1）
    - 统一各类破折号/连字符为标准 "-"
    - 合并连续空白并去除首尾空格
    """
    if not name:
        return ""
    name = unicodedata.normalize('NFKC', name)
    name = _DASH_VARIANTS_RE.sub('-', name)
    name = _MULTI_SPACE_RE.sub(' ', name).strip()
    return name

def get_session():
    """创建一个带有重试机制的requests Session"""
    session = requests.Session()
    # 增强: 增加 total 上限，并对常见的服务端临时错误状态码也进行重试
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    # 增强: 部分源站会拒绝无 User-Agent 的请求，补充常见浏览器 UA 提升成功率
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    })
    return session

def load_urls_from_file(file_path):
    """从文本文件加载URL列表"""
    urls = []
    if not os.path.exists(file_path):
        logger.warning(f"URL配置文件未找到: {file_path}")
        return urls

    try:
        # 使用 utf-8-sig 安全过滤由于记事本编辑可能产生的 \ufeff BOM 头
        with open(file_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        logger.info(f"从 {file_path} 加载了 {len(urls)} 个源")
    except Exception as e:
        logger.error(f"读取URL文件失败: {e}")
    return urls

def parse_template(template_file):
    """解析模板文件"""
    template_channels = OrderedDict()
    current_category = None

    try:
        # 使用 utf-8-sig 避免首行解析出错
        with open(template_file, "r", encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                if "#genre#" in line:
                    current_category = line.split(",")[0].strip()
                    # 修复: 若同一分类在模板中重复出现，不应清空之前已收集的频道
                    if current_category not in template_channels:
                        template_channels[current_category] = []
                elif current_category:
                    channel_name = line.split(",")[0].strip()
                    # 修复(崩溃预防): 跳过空白频道名，避免后续 match_channels 中
                    # variants 列表为空导致 IndexError
                    if channel_name:
                        template_channels[current_category].append(channel_name)
    except FileNotFoundError:
        logger.warning(f"模板文件未找到: {template_file}")
        return None 

    return template_channels

def fetch_channels(url):
    """从URL获取频道列表"""
    channels = OrderedDict()

    # 使用上下文管理器确保 socket 资源正确释放
    with get_session() as session:
        try:
            # 增强: 拆分连接/读取超时，连接更快失败，读取给足时间
            with session.get(url, timeout=(10, 30)) as response:
                response.raise_for_status()
                raw_bytes = response.content

        except Exception as e:
            logger.error(f"处理 {url} 时出错: {e}")
            return channels

    # 修复(编码): 国内 IPTV 源大量使用 GBK/GB2312 编码，直接强制 utf-8 会导致乱码。
    # 跳过缓慢的 chardet(apparent_encoding) 探测，改用 utf-8 优先、GBK 兜底的快速解码策略。
    try:
        text_content = raw_bytes.decode('utf-8')
    except UnicodeDecodeError:
        try:
            text_content = raw_bytes.decode('gbk')
        except UnicodeDecodeError:
            text_content = raw_bytes.decode('utf-8', errors='ignore')

    lines = [line.strip() for line in text_content.splitlines() if line.strip()]
    if not lines:
        return channels

    # 修复(健壮性): 部分 M3U 源在 #EXTINF 之前有较多头部/注释行，仅扫描前10行可能误判为 txt 格式；
    # 优先判断标准 #EXTM3U 头，并放宽扫描窗口
    is_m3u = lines[0].strip().upper().startswith("#EXTM3U") or any(
        "#EXTINF" in line for line in lines[:30]
    )

    if is_m3u:
        DEFAULT_CATEGORY = "默认分类"
        DEFAULT_NAME = "未知频道"
        current_category = DEFAULT_CATEGORY
        current_name = DEFAULT_NAME

        re_group = re.compile(r'group-title="([^"]*)"')
        # 强化正则: 优先取最后一个带引号属性之后的逗号分隔内容，兼容频道名本身含逗号的情况；
        # 若没有任何带引号属性，则回退为 EXTINF: 后第一个逗号之后的内容
        re_name_after_quote = re.compile(r'"\s*,(.*)$')
        re_name_fallback = re.compile(r'#EXTINF:[^,]*,(.*)$')

        for line in lines:
            if line.startswith("#EXTINF"):
                # 修复(分类状态重置): 每条 EXTINF 若未显式声明 group-title，
                # 不应继续沿用上一条频道残留的分类，而应重置为默认分类
                group_match = re_group.search(line)
                current_category = group_match.group(1).strip() if group_match else DEFAULT_CATEGORY

                name_match = re_name_after_quote.search(line) or re_name_fallback.search(line)
                # 修复(状态重置): 未匹配到名称时重置为默认值，避免沿用上一条频道的名称
                current_name = name_match.group(1).strip() if name_match else DEFAULT_NAME
            elif not line.startswith("#") and "://" in line:
                if current_category not in channels:
                    channels[current_category] = []
                if current_name and current_name != DEFAULT_NAME:
                    channels[current_category].append((current_name, line))
                current_name = DEFAULT_NAME
    else:
        current_category = None
        for line in lines:
            if "#genre#" in line:
                current_category = line.split(",")[0].strip()
                if current_category not in channels:
                    channels[current_category] = []
            # 修复: 分类名为空字符串时属于合法但异常的边界情况，用 is not None 判断
            # 避免因空字符串的假值特性而错误丢弃整段内容
            elif current_category is not None and "," in line:
                parts = line.split(",", 1)
                if len(parts) == 2:
                    name, url_part = parts
                    if name.strip() and url_part.strip():
                        channels[current_category].append((name.strip(), url_part.strip()))

    return channels

def match_channels(template_channels, all_channels):
    matched = OrderedDict()
    unmatched_template = OrderedDict()

    # 1. 数据扁平化
    flattened_source_channels = []
    for cat, chans in all_channels.items():
        for name, url in chans:
            flattened_source_channels.append({
                # 增强(频道名归一化): 匹配前统一全角/半角、破折号变体、多余空白，
                # 提升如 ＣＣＴＶ１ / CCTV－1 / CCTV 1 等变体的识别率
                'norm_name': normalize_channel_name(name).lower(),
                'name': name,
                'url': url,
                'cat': cat,
                'key': f"{name}_{url}"
            })

    used_channel_keys = set()

    # 初始化
    for cat in template_channels:
        matched[cat] = OrderedDict()
        unmatched_template[cat] = []

    # 2. 匹配逻辑
    for category, tmpl_names in template_channels.items():
        for tmpl_name in tmpl_names:
            
            # 去重并解析变体
            variants_raw = [n.strip() for n in tmpl_name.split("|") if n.strip()]
            variants = list(OrderedDict.fromkeys(variants_raw))

            # 修复(崩溃预防): 理论上 parse_template 已过滤空名称，这里再做一层防御，
            # 避免 variants 为空时 variants[0] 抛出 IndexError
            if not variants:
                continue

            primary_name = variants[0]
            found_for_this_template = False

            for variant in variants:
                variant_lower = normalize_channel_name(variant).lower()
                if not variant_lower:
                    continue

                # 强化正则: 两端都加边界限制
                # 结尾: 匹配到字符串末尾($) 或 非字母数字且非加号([^a-z0-9\+])，防止 CCTV5 匹配 CCTV5+
                # 开头: 匹配字符串开头(^) 或 非字母数字([^a-z0-9])，防止变体作为子串被更长的名称
                #       误匹配（例如变体 "5" 不应命中 "CCTV15" 中间的 "5"）
                pattern = re.compile(
                    r'(?:^|[^a-z0-9])' + re.escape(variant_lower) + r'(?:$|[^a-z0-9\+])'
                )

                for src in flattened_source_channels:
                    if src['key'] in used_channel_keys:
                        continue

                    # 使用正则搜索
                    if pattern.search(src['norm_name']):
                        if primary_name not in matched[category]:
                            matched[category][primary_name] = []

                        matched[category][primary_name].append((src['name'], src['url']))

                        used_channel_keys.add(src['key'])
                        found_for_this_template = True

            if not found_for_this_template:
                unmatched_template[category].append(tmpl_name)

    # 3. 找出源中未使用的频道
    unmatched_source = OrderedDict()
    for src in flattened_source_channels:
        if src['key'] not in used_channel_keys:
            if src['cat'] not in unmatched_source:
                unmatched_source[src['cat']] = []
            unmatched_source[src['cat']].append((src['name'], src['url']))

    return matched, unmatched_template, unmatched_source

def is_ipv6(url):
    return "://[" in url

def generate_outputs(channels, template_channels, m3u_path, txt_path):
    """生成文件 - 路径参数化"""
    written_urls = set()

    # 安全地确保输出目录存在
    ensure_dir(m3u_path)
    ensure_dir(txt_path)

    try:
        with write_lock:
            with open(m3u_path, "w", encoding="utf-8") as m3u, \
                 open(txt_path, "w", encoding="utf-8") as txt:

                m3u.write("#EXTM3U\n")

                for category in template_channels:
                    if category not in channels or not channels[category]:
                        continue

                    txt.write(f"\n{category},#genre#\n")

                    for channel_key_name, channel_list in channels[category].items():

                        unique_urls = []
                        seen_base_urls = set()

                        for _, url in channel_list:
                            url = url.strip()
                            if not url:
                                continue
                            # 修复(URL去重): 去重应基于 "$" 分隔符前的真实播放地址，
                            # 而非原始字符串。不同源可能给同一条播放地址附加不同的
                            # 追踪后缀(如 $token=xxx)，原先按原始字符串去重会导致
                            # 同一条真实流地址被重复写入多次。
                            base_for_dedup = url.split("$")[0].strip()
                            if not base_for_dedup:
                                continue
                            if base_for_dedup not in seen_base_urls and base_for_dedup not in written_urls:
                                unique_urls.append(url)
                                seen_base_urls.add(base_for_dedup)
                                written_urls.add(base_for_dedup)

                        total_lines = len(unique_urls)
                        for idx, url in enumerate(unique_urls, 1):
                            base_url = url.split("$")[0].strip()
                            suffix_name = "IPV6" if is_ipv6(url) else "IPV4"

                            display_name = channel_key_name

                            meta_suffix = f"$LR•{suffix_name}"
                            if total_lines > 1:
                                meta_suffix += f"•{total_lines}『线路{idx}』"

                            final_url = f"{base_url}{meta_suffix}"

                            m3u.write(f'#EXTINF:-1 tvg-name="{display_name}" group-title="{category}",{display_name}\n')
                            m3u.write(f"{final_url}\n")

                            txt.write(f"{display_name},{final_url}\n")

        logger.info(f"输出完成: {m3u_path}, {txt_path}")
    except Exception as e:
        logger.error(f"写入输出文件失败: {e}")

def generate_unmatched_report(unmatched_template, unmatched_source, report_file):
    """生成未匹配报告"""
    total_template_lost = sum(len(v) for v in unmatched_template.values())
    
    # 如果未指定报告文件路径，则仅计算丢失数量，不执行文件写入
    if not report_file:
        return total_template_lost

    ensure_dir(report_file)

    try:
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(f"# 未匹配报告 {datetime.now()}\n")
            f.write(f"# 模板未匹配数: {total_template_lost}\n\n")
            f.write("## 模板中有但源中无\n")
            for cat, names in unmatched_template.items():
                if names:
                    f.write(f"\n{cat},#genre#\n")
                    for name in list(OrderedDict.fromkeys(names)):
                        f.write(f"{name},\n")

            f.write("\n\n## 源中有但模板无\n")
            for cat, chans in unmatched_source.items():
                if chans:
                    f.write(f"\n{cat},#genre#\n")
                    unique_names = list(OrderedDict.fromkeys([c[0] for c in chans]))
                    for name in unique_names:
                        f.write(f"{name},\n")
        logger.info(f"报告已生成: {report_file}")
        return total_template_lost
    except Exception as e:
        logger.error(f"生成报告失败: {e}")
        return 0

def remove_unmatched_from_template(template_file, unmatched_template):
    backup_file = template_file + ".backup"
    try:
        shutil.copy2(template_file, backup_file)
        with open(template_file, "r", encoding="utf-8-sig") as f:
            lines = f.readlines()

        new_lines = []
        current_cat = None
        to_remove = {cat: set(names) for cat, names in unmatched_template.items()}

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            if "#genre#" in stripped:
                current_cat = stripped.split(",")[0].strip()
                new_lines.append(line)
                continue
            if current_cat:
                name = stripped.split(",")[0].strip()
                if current_cat in to_remove and name in to_remove[current_cat]:
                    continue
                new_lines.append(line)
            else:
                # 修复: 若不在任何 category 内的内容（如异常格式），不应被错误丢弃
                new_lines.append(line)

        with open(template_file, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        logger.info(f"已从模板 {template_file} 移除无效频道")
    except Exception as e:
        logger.error(f"更新模板失败: {e}")

def process_iptv_task(template_file, tv_urls, output_m3u, output_txt, report_file, auto_clean=True):
    """
    处理单个IPTV任务的封装函数
    """
    logger.info(f"=== 开始处理任务: {template_file} ===")
    
    template = parse_template(template_file)
    if not template:
        return

    logger.info(f"开始从 {len(tv_urls)} 个源获取数据...")
    all_channels = OrderedDict()

    success_count = 0
    fail_count = 0

    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_url = {executor.submit(fetch_channels, url): url for url in tv_urls}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                data = future.result()
                if data:
                    success_count += 1
                    for cat, chans in data.items():
                        if cat not in all_channels:
                            all_channels[cat] = []
                        all_channels[cat].extend(chans)
                else:
                    fail_count += 1
            except Exception as e:
                fail_count += 1
                logger.error(f"源 {url} 异常: {e}")

    logger.info(f"数据获取完毕: 成功解析 {success_count} 个源，失败/空数据 {fail_count} 个源。")
    logger.info("开始匹配频道...")
    
    matched, unmatched_tmpl, unmatched_src = match_channels(template, all_channels)

    generate_outputs(matched, template, output_m3u, output_txt)
    lost_count = generate_unmatched_report(unmatched_tmpl, unmatched_src, report_file)

    if auto_clean and lost_count > 0:
        logger.info(f"清理 {lost_count} 个无效频道...")
        remove_unmatched_from_template(template_file, unmatched_tmpl)
    
    logger.info(f"=== 任务完成: {template_file} ===\n")

if __name__ == "__main__":
    # === 配置区 ===
    URLS_FILE = "py/config/urls.txt"
    
    # 1. 加载源
    TV_URLS = load_urls_from_file(URLS_FILE)
    if not TV_URLS:
        logger.warning("未从文件中加载到URL，使用空列表")
        TV_URLS = [] 

    # === 任务1: 主列表 ===
    process_iptv_task(
        template_file="py/config/iptv.txt",
        tv_urls=TV_URLS,
        output_m3u="lib/iptv.m3u",
        output_txt="lib/iptv.txt",
        report_file="py/config/iptv.log",
        auto_clean=False
    )

    # === 任务2: 测试列表 (如果配置文件存在) ===
    TEST_TEMPLATE_FILE = "py/config/iptv_test.txt"
    if os.path.exists(TEST_TEMPLATE_FILE):
        process_iptv_task(
            template_file=TEST_TEMPLATE_FILE,
            tv_urls=TV_URLS,
            output_m3u="lib/iptv_test.m3u",
            output_txt="lib/iptv_test.txt",
            report_file=None,
            auto_clean=False 
        )
    else:
        logger.info(f"未检测到测试配置 {TEST_TEMPLATE_FILE}，跳过测试生成。")

