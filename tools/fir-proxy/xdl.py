import requests
import json
import os

# Define the list of all proxy sources, including their URL, parser type, and protocol
SOURCES = [
    {
        "name": "TheSpeedX/PROXY-List",
        "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
        "parser": "text",
        "protocol": "socks5"  # Protocol is SOCKS5
    },
    {
        "name": "hookzof/socks5_list",
        "url": "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
        "parser": "text",
        "protocol": "socks5"  # Protocol is SOCKS5
    },
    {
        "name": "fate0/proxylist",
        "url": "https://raw.githubusercontent.com/fate0/proxylist/master/proxy.list",
        "parser": "json",
        "protocol": "dynamic"  # Protocol type is within the JSON data
    },
    {
        "name": "ProxyScraper/ProxyScraper (SOCKS5)",
        "url": "https://raw.githubusercontent.com/ProxyScraper/ProxyScraper/main/socks5.txt",
        "parser": "text",
        "protocol": "socks5"  # Protocol is SOCKS5
    }
]


def save_proxies_to_file(proxy_set, file_name, output_dir):
    """
    Saves a given set of proxies to a specified file.
    """
    if not proxy_set:
        print(f"\n[-] 代理列表为空，跳过保存 {file_name}。")
        return

    file_path = os.path.join(output_dir, file_name)
    try:
        # Create the directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

        # Sort the list for a clean, consistent output
        sorted_proxies = sorted(list(proxy_set))

        # Write the list to the file
        with open(file_path, 'w', encoding='utf-8') as f:
            for proxy in sorted_proxies:
                f.write(f"{proxy}\n")

        print(f"\n[SUCCESS] {file_name} 文件已成功保存 (共 {len(sorted_proxies)} 个代理)。")
        print(f"  -> {file_path}")

    except Exception as e:
        print(f"\n[ERROR] 保存文件 {file_name} 时出错: {e}")


def fetch_and_save_proxies():
    """
    Fetches proxies from a list of sources, classifies them by protocol,
    prepends the protocol to the address, and saves them to separate files.
    """
    # Use two sets to store proxies based on protocol
    http_proxies = set()
    other_proxies = set()  # For SOCKS4, SOCKS5, etc.

    # Iterate over each defined source
    for source in SOURCES:
        print(f"[*] 正在从 {source['name']} 获取代理列表...")
        try:
            response = requests.get(source['url'], timeout=10)
            response.raise_for_status()

            lines = response.text.strip().split('\n')
            print(f"[+] 成功获取 {len(lines)} 条数据，正在处理...")

            initial_count = len(http_proxies) + len(other_proxies)

            # Process content based on the defined parser type
            if source['parser'] == 'json':
                for line in lines:
                    if not line.strip(): continue
                    try:
                        proxy_info = json.loads(line)
                        host = proxy_info.get("host")
                        port = proxy_info.get("port")
                        # Default to 'http' if type is missing, and convert to lowercase
                        proxy_type = proxy_info.get("type", "http").lower()

                        if host and port:
                            proxy_address = f"{host}:{port}"
                            # Classify based on the 'type' field in the JSON
                            if 'http' in proxy_type:  # Handles 'http' and 'https'
                                http_proxies.add(f"http://{proxy_address}")
                            else:  # Handles 'socks4', 'socks5', etc.
                                other_proxies.add(f"{proxy_type}://{proxy_address}")
                    except json.JSONDecodeError:
                        print(f"[!] 忽略来自 {source['name']} 的格式错误行: {line}")

            elif source['parser'] == 'text':
                # Classify based on the 'protocol' field in the source definition
                for line in lines:
                    line = line.strip()
                    if line:
                        protocol = source['protocol']
                        if protocol == 'http':
                            http_proxies.add(f"http://{line}")
                        else:
                            other_proxies.add(f"{protocol}://{line}")

            new_proxies_count = (len(http_proxies) + len(other_proxies)) - initial_count
            print(f"[+] 从此来源添加了 {new_proxies_count} 个新代理。")

        except requests.exceptions.RequestException as e:
            print(f"[!] 从 {source['name']} 获取代理时出错: {e}")

        print("-" * 20)

    # --- Final Output and Save to Files ---
    if not http_proxies and not other_proxies:
        print("\n[-] 未能从任何来源成功获取代理。")
        return

    # [!] 修改：将输出目录设置为当前脚本所在的目录
    output_dir = os.getcwd()

    # Save each category of proxies to its respective file
    save_proxies_to_file(http_proxies, "http.txt", output_dir)
    save_proxies_to_file(other_proxies, "git.txt", output_dir)


if __name__ == "__main__":
    fetch_and_save_proxies()