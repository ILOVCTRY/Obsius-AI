# modules/checker.py

import requests
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import threading

class ProxyChecker:
    """
    一个经过优化的多阶段代理验证器，结合TCP预检和完整质量验证。
    """
    def __init__(self, timeout: int = 5):
        self.timeout = timeout
        self._session_owner = threading.get_ident()
        self._thread_local = threading.local()
        self.session = self._create_session()
        self._thread_local.session = self.session
        self._location_cache_lock = threading.Lock()

        self.validation_targets = {
            'latency_check': 'https://www.baidu.com',
            'anonymity_check': 'http://httpbin.org/get?show_env=1',
            'speed_check': 'http://cachefly.cachefly.net/100kb.test',
        }
        
        # 国家名称中文映射
        self.COUNTRY_NAME_MAP = {
            'China': '中国',
            'Hong Kong': '香港',
            'Singapore': '新加坡',
            'United States': '美国',
            'Japan': '日本',
            'South Korea': '韩国',
            'Russia': '俄罗斯',
            'Germany': '德国',
            'United Kingdom': '英国',
            'France': '法国',
            'Canada': '加拿大',
            'Taiwan': '台湾',
            'Netherlands': '荷兰',
            'India': '印度',
            'Vietnam': '越南',
            'Thailand': '泰国',
        }
        self.location_cache = {}
        self.public_ip = None

    @staticmethod
    def _create_session():
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
        })
        return session

    def _get_session(self):
        """返回当前线程专用的 Session，避免共享连接池状态。"""
        if threading.get_ident() == self._session_owner:
            return self.session
        session = getattr(self._thread_local, 'session', None)
        if session is None:
            session = self._create_session()
            self._thread_local.session = session
        return session

    @staticmethod
    def _split_proxy_address(proxy: str):
        host, port_str = proxy.rsplit(':', 1)
        host = host.strip().strip('[]')
        if not host:
            raise ValueError("代理地址缺少主机名")
        port = int(port_str)
        if not 1 <= port <= 65535:
            raise ValueError("代理端口超出范围")
        return host, port

    def initialize_public_ip(self, log_queue=None):
        """通过调用系统 'curl' 命令获取本机公网IP，作为匿名度检测的基准。"""
        try:
            command = ['curl', 'ip.sb']
            result = subprocess.run(
                command, capture_output=True, text=True, check=True, timeout=10
            )
            ip_address = result.stdout.strip()
            
            if ip_address and '.' in ip_address:
                self.public_ip = ip_address
                if log_queue:
                    log_queue.put(f"[Checker] 成功获取本机公网IP: {self.public_ip} (通过 ip.sb)")
            else:
                 if log_queue:
                    log_queue.put(f"[Checker] [!] 调用curl ip.sb未能返回有效IP。响应: '{ip_address}'")

        except FileNotFoundError:
            if log_queue:
                log_queue.put("[Checker] [!] 'curl'命令未找到。请确保curl已安装并在系统PATH中。")
        except Exception as e:
            if log_queue:
                log_queue.put(f"[Checker] [!] 调用系统curl获取本机公网IP失败: {e}")

    # --- IP地理位置查询 (聚合多个API) ---
    def _get_proxy_location(self, ip: str):
        """
        查询IP的地理位置，聚合多个API源并带缓存，优先国内源，结果翻译为中文。
        """
        with self._location_cache_lock:
            cached_location = self.location_cache.get(ip)
        if cached_location is not None:
            return cached_location

        location = "未知"
        session = self._get_session()
        
        # API 1: ip-api.com (国际源, 覆盖广)
        try:
            url = f"http://ip-api.com/json/{ip}?lang=zh-CN&fields=status,message,country"
            res = session.get(url, timeout=self.timeout)
            res.raise_for_status()
            data = res.json()
            if data.get('status') == 'success':
                country = data.get('country', '')
                if country:
                    location = self.COUNTRY_NAME_MAP.get(country, country)
                    with self._location_cache_lock:
                        self.location_cache[ip] = location
                    return location
        except Exception:
            pass # 尝试下一个API

        # API 2: ip.taobao.com (国内源, 查国内IP快且准)
        try:
            url = f"https://ip.taobao.com/outGetIpInfo?ip={ip}&accessKey=alibaba-inc"
            res = session.get(url, timeout=self.timeout)
            res.raise_for_status()
            data = res.json()
            if data.get('code') == 0 and 'data' in data:
                d = data['data']
                country = d.get('country', '')
                if country:
                    location = self.COUNTRY_NAME_MAP.get(country, country)
                    with self._location_cache_lock:
                        self.location_cache[ip] = location
                    return location
        except Exception:
            pass # 尝试下一个API

        # API 3: ip.sb (备用源)
        try:
            url = f"https://api.ip.sb/geoip/{ip}"
            res = session.get(url, timeout=self.timeout)
            res.raise_for_status()
            data = res.json()
            country = data.get('country', '')
            if country:
                location = self.COUNTRY_NAME_MAP.get(country, country)
                with self._location_cache_lock:
                    self.location_cache[ip] = location
                return location
        except Exception:
            pass
            
        with self._location_cache_lock:
            self.location_cache[ip] = location
        return location

    def _pre_check_proxy(self, proxy: str):
        """TCP预检，快速判断端口是否开放。"""
        try:
            host, port = self._split_proxy_address(proxy)
            with socket.create_connection((host, port), timeout=min(1.5, self.timeout)):
                return True
        except Exception:
            return False

    def _full_check_proxy(self, proxy_info: dict, validation_mode: str = 'online', cancel_event=None):
        """
        对单个代理进行完整的质量验证，此过程可随时取消。
        在每个阻塞网络操作前后，都会检查 cancel_event。
        """
        proxy = proxy_info['proxy']
        protocol = proxy_info['protocol']
        proxy_url = f"{protocol.lower()}://{proxy}"
        proxies_dict = {'http': proxy_url, 'https': proxy_url}
        session = self._get_session()
        result = {
            'proxy': proxy, 'protocol': protocol.upper(), 'status': 'Failed',
            'latency': float('inf'), 'speed': 0, 'anonymity': 'Unknown', 'location': 'N/A'
        }

        try:
            if cancel_event and cancel_event.is_set(): return None

            start_time = time.time()
            session.head(self.validation_targets['latency_check'], proxies=proxies_dict, timeout=self.timeout).raise_for_status()
            result['latency'] = time.time() - start_time

            if cancel_event and cancel_event.is_set(): return None

            res_anon = session.get(self.validation_targets['anonymity_check'], proxies=proxies_dict, timeout=self.timeout)
            res_anon.raise_for_status()
            data = res_anon.json()
            origin_ips_str = data.get('headers', {}).get('X-Forwarded-For', data.get('origin', ''))
            origin_ips = [ip.strip() for ip in origin_ips_str.split(',')]
            
            if self.public_ip and any(self.public_ip in ip for ip in origin_ips):
                result['anonymity'] = 'Transparent'
                result['location'] = self._get_proxy_location(self._split_proxy_address(proxy)[0])
                result['status'] = 'Working'
                return result # 透明代理不测速，但仍是可连接代理
            elif len(origin_ips) > 1 or 'Via' in data.get('headers', {}):
                result['anonymity'] = 'Anonymous'
            else:
                result['anonymity'] = 'Elite'

            if cancel_event and cancel_event.is_set(): return None

            # 延迟低于7秒的才进行测速
            if result['latency'] <= 7.0:
                speed_check_url = self.validation_targets['speed_check']
                try:
                    start_speed = time.time()
                    speed_response = session.get(speed_check_url, proxies=proxies_dict, timeout=self.timeout, stream=True)
                    speed_response.raise_for_status()
                    
                    content_size = 0
                    for chunk in speed_response.iter_content(chunk_size=8192):
                        if cancel_event and cancel_event.is_set():
                            speed_response.close() # 及时关闭连接
                            return None
                        content_size += len(chunk)

                    speed_duration = time.time() - start_speed
                    if speed_duration > 0 and content_size > 0:
                        # 计算速度，单位 Mbps
                        result['speed'] = (content_size / speed_duration) * 8 / (1000**2)
                except Exception:
                    pass # 测速失败不影响整体结果

            if cancel_event and cancel_event.is_set(): return None
            
            # 查询地理位置
            result['location'] = self._get_proxy_location(self._split_proxy_address(proxy)[0])
            
            result['status'] = 'Working'
            return result

        except requests.RequestException as exc:
            result['error'] = str(exc)
            return result
        except Exception as exc:
            result['error'] = str(exc)
            return result

    def check_proxy_url(self, proxy_info: dict, url: str, timeout: int = 15):
        """通过指定代理访问健康检查URL，返回成功、状态码和错误信息。"""
        proxy = proxy_info['proxy']
        protocol = proxy_info['protocol'].lower()
        proxy_url = f"{protocol}://{proxy}"
        proxies_dict = {'http': proxy_url, 'https': proxy_url}
        session = self._get_session()
        try:
            response = session.get(url, proxies=proxies_dict,
                                   timeout=timeout, allow_redirects=True,
                                   stream=True)
            status_code = response.status_code
            response.close()
            return {
                'ok': 200 <= status_code < 400,
                'status_code': status_code,
                'error': '' if 200 <= status_code < 400 else f'HTTP {status_code}',
            }
        except requests.RequestException as exc:
            return {'ok': False, 'status_code': None, 'error': str(exc)}
        except Exception as exc:
            return {'ok': False, 'status_code': None, 'error': str(exc)}

    @staticmethod
    def _precheck_failure_result(proxy_info: dict):
        return {
            'proxy': proxy_info['proxy'],
            'protocol': proxy_info['protocol'].upper(),
            'status': 'Failed',
            'latency': float('inf'),
            'speed': 0,
            'anonymity': 'Unknown',
            'location': 'N/A',
            'failure_stage': 'tcp_precheck',
            'error': 'TCP 预检失败',
        }

    def validate_all(self, proxies_by_protocol: dict, result_queue, log_queue, validation_mode='online', max_workers=100, cancel_event=None):
        all_proxies_flat = [{'proxy': p, 'protocol': proto} for proto, proxies in proxies_by_protocol.items() for p in proxies]
        total_proxies = len(all_proxies_flat)
        
        survivors = []
        # 代理数量太多时，跳过TCP预检，避免开销过大
        if total_proxies > 10000:
            log_queue.put(f"[!] 代理总数 ({total_proxies}) 超过10000，跳过TCP预检。")
            survivors = all_proxies_flat
        else:
            log_queue.put(f"[*] 阶段一：TCP预检开始，总数: {total_proxies}...")
            executor = ThreadPoolExecutor(max_workers=max(1, min(500, total_proxies)))
            try:
                future_to_proxy = {executor.submit(self._pre_check_proxy, p['proxy']): p for p in all_proxies_flat}
                for future in as_completed(future_to_proxy):
                    if cancel_event and cancel_event.is_set(): break
                    proxy_info = future_to_proxy[future]
                    try:
                        is_reachable = future.result()
                    except Exception as exc:
                        log_queue.put(f"[!] TCP预检线程出现异常: {exc}")
                        is_reachable = False
                    if is_reachable:
                        survivors.append(proxy_info)
                    else:
                        result_queue.put(self._precheck_failure_result(proxy_info))
            finally:
                executor.shutdown(
                    wait=not (cancel_event and cancel_event.is_set()),
                    cancel_futures=bool(cancel_event and cancel_event.is_set()),
                )
            log_queue.put(f"[+] 阶段一：TCP预检完成，幸存者: {len(survivors)} / {total_proxies}。")

        if cancel_event and cancel_event.is_set():
            log_queue.put("[Checker] 任务在TCP预检后被用户取消。")
            return # 直接返回，不往队列放任何东西

        log_queue.put("\n" + "="*20 + f" 阶段二：开始完整质量验证 " + "="*20)
        
        if not survivors:
            result_queue.put(None) # 正常结束
            return

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = [executor.submit(self._full_check_proxy, p, validation_mode, cancel_event) for p in survivors]
            for future in as_completed(futures):
                if cancel_event and cancel_event.is_set():
                    break
                try:
                    result = future.result()
                    if result:
                        result_queue.put(result)
                except Exception as e:
                    log_queue.put(f"[!] 验证器线程出现异常: {e}")
        finally:
            executor.shutdown(
                wait=not (cancel_event and cancel_event.is_set()),
                cancel_futures=bool(cancel_event and cancel_event.is_set()),
            )

        # 只有在任务未被取消的情况下，才发送结束信号(None)
        if not (cancel_event and cancel_event.is_set()):
            result_queue.put(None)
        else:
            log_queue.put("[Checker] 任务在完整验证阶段被用户取消。")
