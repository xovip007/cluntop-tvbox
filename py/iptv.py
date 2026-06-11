import requests
import re
import os

# ================= 配置区 =================
CONFIG_FILE = "py/config/iptv_m3u.txt"      # 存放 M3U 链接的配置文件
OUTPUT_FILE = "lib/cctv.m3u"        # 最终合并规范化后的 M3U 文件import requestsimport requests

def parse_config():
    """
    解析高级配置文件
    返回: tasks 列表和 global_blocks 屏蔽词集合
    """
    tasks = []
    global_blocks = set()
    
    if not os.path.exists(CONFIG_FILE):
        print(f"错误: 找不到配置文件 {CONFIG_FILE}")
        return tasks, global_blocks

    current_groups = set()

    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            # 1. 匹配屏蔽词定义，例如 [BLOCK: 购物, 测试]
            if line.startswith('[BLOCK:') and line.endswith(']'):
                block_content = line[7:-1].strip()
                # 提取屏蔽词，转换为小写以实现不区分大小写拦截
                new_blocks = {b.strip().lower() for b in block_content.split(',') if b.strip()}
                global_blocks.update(new_blocks)
                continue

            # 2. 匹配分组定义，例如 [央视, 卫视] 或 [ALL]
            if line.startswith('[') and line.endswith(']'):
                group_content = line[1:-1].strip()
                if group_content.upper() == 'ALL':
                    current_groups = {'ALL'}
                else:
                    current_groups = {g.strip().lower() for g in group_content.split(',') if g.strip()}
                continue
            
            # 3. 匹配 URL 链接
            if line.startswith('http://') or line.startswith('https://'):
                if not current_groups:
                    print(f"警告: 链接 {line} 之前未指定 [分组]，跳过。")
                    continue
                
                tasks.append({
                    'url': line,
                    'groups': current_groups.copy()
                })
                
    return tasks, global_blocks

def fetch_and_filter_m3u():
    tasks, global_blocks = parse_config()
    if not tasks:
        print("未找到任何有效的抓取任务，程序退出。")
        return

    print(f"已解析任务: {len(tasks)} 个抓取源。")
    if global_blocks:
        print(f"已加载全局屏蔽词: {', '.join(global_blocks)}")
        
    total_saved_count = 0
    total_blocked_count = 0

    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f_out:
        f_out.write("#EXTM3U\n")
        
        for index, task in enumerate(tasks, 1):
            url = task['url']
            groups = task['groups']
            
            group_desc = "所有频道" if 'ALL' in groups else ", ".join(groups)
            print(f"\n[{index}/{len(tasks)}] 抓取: {url}\n    -> 目标分组: [{group_desc}]")
            
            try:
                response = requests.get(url, timeout=15)
                response.encoding = 'utf-8'
                lines = response.text.splitlines()
            except Exception as e:
                print(f"    ❌ 抓取失败: {e}")
                continue

            i = 0
            url_saved_count = 0
            url_blocked_count = 0
            
            while i < len(lines):
                line = lines[i].strip()
                
                if line.startswith("#EXTINF"):
                    # ----- 步骤 A：提取频道的各项关键属性 -----
                    # 提取 tvg-name
                    tvg_name_match = re.search(r'tvg-name="([^"]+)"', line)
                    tvg_name = tvg_name_match.group(1).lower() if tvg_name_match else ""
                    
                    # 提取 group-title
                    group_match = re.search(r'group-title="([^"]+)"', line)
                    group_title = group_match.group(1).lower() if group_match else ""
                    
                    # 提取频道名称 (逗号后面的部分)
                    channel_name_match = re.search(r',(.+)$', line)
                    channel_name = channel_name_match.group(1).lower() if channel_name_match else ""
                    
                    # ----- 步骤 B：黑名单拦截校验 -----
                    is_blocked = False
                    if global_blocks:
                        for block_word in global_blocks:
                            # 只要任意一个属性包含屏蔽词，就触发拦截
                            if (block_word in tvg_name) or (block_word in group_title) or (block_word in channel_name):
                                is_blocked = True
                                break
                    
                    # 如果触发屏蔽，跳过该频道及下一行的 URL
                    if is_blocked:
                        url_blocked_count += 1
                        total_blocked_count += 1
                        i += 1
                        continue

                    # ----- 步骤 C：白名单分组匹配 -----
                    is_match = False
                    if 'ALL' in groups:
                        is_match = True
                    elif group_title in groups: # 匹配已转小写的 group_title
                        is_match = True
                    
                    # 如果匹配成功，保存频道
                    if is_match:
                        f_out.write(line + "\n")
                        if i + 1 < len(lines):
                            play_url = lines[i+1].strip()
                            f_out.write(play_url + "\n")
                            url_saved_count += 1
                            total_saved_count += 1
                i += 1
                
            print(f"    ➔ 成功保存: {url_saved_count} 个频道，成功拦截屏蔽: {url_blocked_count} 个频道。")
            
    print(f"\n🎉 自动化合并完成！")
    print(f"    ✅ 规范化保存频道总数: {total_saved_count}")
    print(f"    🛑 触发屏蔽词丢弃总数: {total_blocked_count}")
    print(f"    📁 文件已写入: {OUTPUT_FILE}")

if __name__ == "__main__":
    fetch_and_filter_m3u()
