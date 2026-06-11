import requests
import re
import os

# ================= 配置区 =================
CONFIG_FILE = "py/config/iptv_m3u.txt"      # 存放 M3U 链接的配置文件
OUTPUT_FILE = "lib/cctv.m3u"        # 最终合并规范化后的 M3U 文件import requests

def parse_config():
    """
    解析高级配置文件
    返回结构: [{'urls': [...], 'groups': {...}}, ...]
    """
    tasks = []
    if not os.path.exists(CONFIG_FILE):
        print(f"错误: 找不到配置文件 {CONFIG_FILE}")
        return tasks

    current_groups = set() # 当前正在生效的分组集合

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            # 忽略空行和注释
            if not line or line.startswith('#'):
                continue
            
            # 1. 匹配分组定义，例如 [央视, 卫视] 或 [ALL]
            if line.startswith('[') and line.endswith(']'):
                group_content = line[1:-1].strip()
                if group_content.upper() == 'ALL':
                    current_groups = {'ALL'}
                else:
                    # 分割多个分组，去空格并转换为小写，方便后续不区分大小写匹配
                    current_groups = {g.strip().lower() for g in group_content.split(',') if g.strip()}
                continue
            
            # 2. 匹配 URL 链接 (简单的 URL 判定)
            if line.startswith('http://') or line.startswith('https://'):
                if not current_groups:
                    print(f"警告: 链接 {line} 之前未指定 [分组]，默认将过滤为空，跳过此链接。")
                    continue
                
                # 将链接与它当前所属的分组规则绑定为一个任务
                tasks.append({
                    'url': line,
                    'groups': current_groups.copy()
                })
                
    return tasks

def fetch_and_filter_m3u():
    tasks = parse_config()
    if not tasks:
        print("未找到任何有效的抓取任务，程序退出。")
        return

    print(f"已成功解析配置文件，共计包含 {len(tasks)} 个网络抓取任务。")
    total_saved_count = 0

    # 开始写入最终的合并 M3U 文件
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f_out:
        f_out.write("#EXTM3U\n")
        
        for index, task in enumerate(tasks, 1):
            url = task['url']
            groups = task['groups']
            
            group_desc = "所有频道" if 'ALL' in groups else ", ".join(groups)
            print(f"[{index}/{len(tasks)}] 正在抓取: {url}\n    -> 目标过滤分组: [{group_desc}]")
            
            try:
                response = requests.get(url, timeout=15)
                response.encoding = 'utf-8'
                lines = response.text.splitlines()
            except Exception as e:
                print(f"    ❌ 抓取失败，跳过该源。原因: {e}")
                continue

            i = 0
            url_saved_count = 0
            while i < len(lines):
                line = lines[i].strip()
                
                # 识别频道信息行
                if line.startswith("#EXTINF"):
                    is_match = False
                    
                    # 如果配置为 [ALL]，则直接判定匹配成功
                    if 'ALL' in groups:
                        is_match = True
                    else:
                        # 正则表达式精准匹配 group-title="..."
                        group_match = re.search(r'group-title="([^"]+)"', line)
                        if group_match:
                            current_group = group_match.group(1).strip().lower()
                            if current_group in groups:
                                is_match = True
                    
                    # 如果匹配成功，保存该频道信息及其链接
                    if is_match:
                        f_out.write(line + "\n")
                        if i + 1 < len(lines):
                            play_url = lines[i+1].strip()
                            f_out.write(play_url + "\n")
                            url_saved_count += 1
                            total_saved_count += 1
                i += 1
            print(f"    ➔ 完成！该源成功提取出 {url_saved_count} 个频道。")
            
    print(f"\n🎉 自动化合并完成！总体共规范化保存了 {total_saved_count} 个频道，已写入 {OUTPUT_FILE}")

if __name__ == "__main__":
    fetch_and_filter_m3u()
