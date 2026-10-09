# modules/server.py

import socket
import threading
import select
import struct
import socks 
from urllib.parse import urlparse

class ProxyServer:
    """本地代理服务，将进入的请求通过代理池转发。支持HTTP和SOCKS5。"""
    def __init__(self, http_host, http_port, socks5_host, socks5_port, rotator,
                 log_queue, traffic_queue=None):
        self._rotator = rotator
        self._log_queue = log_queue
        self._traffic_queue = traffic_queue
        self._running = False

        self._http_host = http_host
        self._http_port = http_port
        self._http_server_socket = None
        self._http_thread = None

        self._socks5_host = socks5_host
        self._socks5_port = socks5_port
        self._socks5_server_socket = None
        self._socks5_thread = None
        
        self._rotation_lock = threading.Lock()
        self._request_count_enabled = False
        self._request_count_limit = 10
        self._request_count = 0
        self._counted_proxy_address = None
        self._target_failure_enabled = False
        self._target_failure_threshold = 3
        self._target_failures = {}
        self._failover_lock = threading.Lock()
        self._failover_inflight = set()
        self._upstream_connect_timeout = 8
        self._client_read_timeout = 30
        self._handler_slots = threading.BoundedSemaphore(256)

    def log(self, message):
        self._log_queue.put(f"[Server] {message}")

    def log_traffic(self, message):
        if self._traffic_queue is not None:
            self._traffic_queue.put(f"[Proxy] {message}")

    def configure_rotation(self, request_count_enabled, request_count,
                           target_failure_enabled, target_failure_threshold):
        """Apply user-controlled request and target-failure rotation settings."""
        with self._rotation_lock:
            self._request_count_enabled = bool(request_count_enabled)
            self._request_count_limit = max(1, int(request_count))
            self._request_count = 0
            self._counted_proxy_address = None
            self._target_failure_enabled = bool(target_failure_enabled)
            self._target_failure_threshold = max(1, int(target_failure_threshold))
            self._target_failures.clear()

    @staticmethod
    def _target_key(target_host, target_port):
        return f"{str(target_host).lower()}:{int(target_port)}"

    def _record_successful_request(self, proxy_address, target_host, target_port):
        """Clear target failures and rotate the next request after its configured count."""
        next_proxy = None
        with self._rotation_lock:
            self._target_failures.pop(self._target_key(target_host, target_port), None)
            if not self._request_count_enabled:
                return
            if self._counted_proxy_address != proxy_address:
                self._counted_proxy_address = proxy_address
                self._request_count = 0
            self._request_count += 1
            if self._request_count < self._request_count_limit:
                return
            self._request_count = 0
            next_proxy = self._rotator.get_next_proxy()
            self._counted_proxy_address = next_proxy.get('proxy') if next_proxy else None

        if next_proxy:
            self.log(
                f"[按请求轮换] {proxy_address} 已转发 {self._request_count_limit} 个请求，"
                f"下一请求切换至 {next_proxy.get('proxy')}"
            )

    def _record_target_failure(self, target_host, target_port):
        """Return True only when this target reaches its configured failure threshold."""
        target_key = self._target_key(target_host, target_port)
        with self._rotation_lock:
            if not self._target_failure_enabled:
                return False, 0
            count = self._target_failures.get(target_key, 0) + 1
            if count >= self._target_failure_threshold:
                self._target_failures.pop(target_key, None)
                return True, count
            self._target_failures[target_key] = count
            return False, count

    def _open_upstream_socket(self, proxy_info, target_host, target_port):
        """通过指定代理建立到目标域名/端口的连接。"""
        addr = proxy_info.get('proxy')
        proto = proxy_info.get('protocol')
        if not addr or not proto:
            raise ValueError(f"代理信息格式不正确: {proxy_info}")

        upstream_addr, upstream_port_str = addr.rsplit(':', 1)
        proxy_type_map = {
            'HTTP': socks.HTTP,
            'SOCKS4': socks.SOCKS4,
            'SOCKS5': socks.SOCKS5,
        }
        upstream_protocol = proxy_type_map.get(proto.upper())
        if not upstream_protocol:
            raise ValueError(f"不支持的上游代理协议: {proto}")

        remote_socket = socks.socksocket()
        remote_socket.settimeout(self._upstream_connect_timeout)
        try:
            remote_socket.set_proxy(
                proxy_type=upstream_protocol,
                addr=upstream_addr,
                port=int(upstream_port_str),
            )
            remote_socket.connect((target_host, int(target_port)))
            return remote_socket
        except Exception:
            remote_socket.close()
            raise

    def _get_failover_candidates(self, failed_address):
        """按代理评分取得候选池，排除本次失败的代理。"""
        candidates = [
            p for p in self._rotator.get_all_proxies_for_revalidation()
            if p.get('status') == 'Working' and p.get('proxy') != failed_address
        ]
        candidates.sort(key=lambda p: p.get('score', 0), reverse=True)
        return candidates

    def _try_domain_failover(self, target_host, target_port, failed_proxy):
        """验证其它代理能否访问目标；成功后切换当前代理并返回连接。"""
        target_key = f"{target_host}:{target_port}"
        with self._failover_lock:
            if target_key in self._failover_inflight:
                self.log(f"[自动切换] {target_key} 已在验证中，跳过重复验证。")
                return None
            self._failover_inflight.add(target_key)

        try:
            candidates = self._get_failover_candidates(failed_proxy)
            if not candidates:
                self.log(f"[自动切换] {target_key} 没有其它可用代理可验证。")
                return None

            self.log(f"[自动切换] {target_key} 访问失败，开始验证其它代理（共{len(candidates)}个）。")
            for candidate in candidates:
                candidate_address = candidate.get('proxy')
                try:
                    remote_socket = self._open_upstream_socket(
                        candidate, target_host, target_port
                    )
                except Exception as exc:
                    self.log(f"[自动切换] 验证失败 {candidate_address}: {exc}")
                    continue

                if not self._rotator.set_current_proxy_by_address(candidate_address):
                    remote_socket.close()
                    continue

                self.log(
                    f"[自动切换] {target_key} 已切换: "
                    f"{failed_proxy} -> {candidate_address}"
                )
                remote_socket._proxy_label = (
                    f"{candidate.get('protocol', '').lower()}://{candidate_address}"
                )
                self._record_successful_request(
                    candidate_address, target_host, target_port
                )
                return remote_socket

            self.log(f"[自动切换] {target_key} 未找到可用代理。")
            return None
        finally:
            with self._failover_lock:
                self._failover_inflight.discard(target_key)

    def try_domain_failover(self, target_host, target_port, failed_proxy):
        """供健康检查调用：验证其它代理并切换当前代理，不保留测试连接。"""
        should_failover, failure_count = self._record_target_failure(
            target_host, target_port
        )
        if not should_failover:
            if self._target_failure_enabled:
                self.log(
                    f"[目标失败] {self._target_key(target_host, target_port)} "
                    f"连续失败 {failure_count}/{self._target_failure_threshold}，暂不切换。"
                )
            return False
        remote_socket = self._try_domain_failover(
            target_host, target_port, failed_proxy
        )
        if remote_socket:
            remote_socket.close()
            return True
        return False

    @staticmethod
    def _create_listening_socket(host, port):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((host, port))
            server_socket.listen(20)
            return server_socket
        except Exception:
            server_socket.close()
            raise

    def start_all(self):
        """启动所有代理服务，并在两个监听端口均绑定成功后返回 True。"""
        if self._running:
            return True
        try:
            http_socket = self._create_listening_socket(
                self._http_host, self._http_port
            )
            try:
                socks5_socket = self._create_listening_socket(
                    self._socks5_host, self._socks5_port
                )
            except Exception:
                http_socket.close()
                raise
        except Exception as exc:
            self.log(f"[!] 启动代理服务失败: {exc}")
            return False

        self._http_server_socket = http_socket
        self._socks5_server_socket = socks5_socket
        self._running = True

        self._http_thread = threading.Thread(target=self._run_http_server, daemon=True)
        self._http_thread.start()

        self._socks5_thread = threading.Thread(target=self._run_socks5_server, daemon=True)
        self._socks5_thread.start()
        return True

    def stop_all(self):
        """平滑地停止所有正在运行的代理服务。"""
        if not self._running:
            return
        self._running = False
        
        if self._http_server_socket:
            self._http_server_socket.close()
        if self._socks5_server_socket:
            self._socks5_server_socket.close()

        if self._http_thread and self._http_thread.is_alive():
            self._http_thread.join()
        if self._socks5_thread and self._socks5_thread.is_alive():
            self._socks5_thread.join()
        self._http_server_socket = None
        self._socks5_server_socket = None
        self.log("所有代理服务已停止。")

    def _run_http_server(self):
        """HTTP服务监听循环。"""
        server_socket = self._http_server_socket
        if not server_socket:
            return
        self.log(f"HTTP 代理服务接口已启动于 {self._http_host}:{self._http_port}")

        while self._running:
            try:
                client_socket, _ = server_socket.accept()
                client_socket.settimeout(self._client_read_timeout)
                self._start_client_handler(self._handle_http_client, client_socket)
            except OSError:
                break 
        self.log("HTTP 代理服务循环已退出。")

    def _run_socks5_server(self):
        """SOCKS5服务监听循环。"""
        server_socket = self._socks5_server_socket
        if not server_socket:
            return
        self.log(f"SOCKS5 代理服务接口已启动于 {self._socks5_host}:{self._socks5_port}")

        while self._running:
            try:
                client_socket, _ = server_socket.accept()
                client_socket.settimeout(self._client_read_timeout)
                self._start_client_handler(self._handle_socks5_client, client_socket)
            except OSError:
                break
        self.log("SOCKS5 代理服务循环已退出。")

    def _start_client_handler(self, handler, client_socket):
        """Start a bounded client handler and reject connections when saturated."""
        if not self._handler_slots.acquire(blocking=False):
            self.log("[!] 并发连接数已达上限，已拒绝新的客户端连接。")
            client_socket.close()
            return False

        def run_handler():
            try:
                handler(client_socket)
            finally:
                self._handler_slots.release()

        try:
            threading.Thread(target=run_handler, daemon=True).start()
            return True
        except Exception:
            self._handler_slots.release()
            client_socket.close()
            raise
        
    def _get_upstream_connection(self, target_host, target_port):
        """从轮换器获取一个上游代理，并用它来连接目标地址。"""
        upstream_proxy_info = self._rotator.get_current_proxy()

        if not upstream_proxy_info:
            self.log("[!] 代理池为空或无符合条件的代理，无法转发请求。")
            return None

        addr = upstream_proxy_info.get('proxy')
        proto = upstream_proxy_info.get('protocol')

        if not addr or not proto:
            self.log(f"[!] 代理信息格式不正确: {upstream_proxy_info}")
            return None

        try:
            remote_socket = self._open_upstream_socket(
                upstream_proxy_info, target_host, target_port
            )
            remote_socket._proxy_label = f"{proto.lower()}://{addr}"
            self.log_traffic(
                f"已连接 {target_host}:{target_port} | 上游: {proto.lower()}://{addr}"
            )
            self._record_successful_request(addr, target_host, target_port)
            return remote_socket
        except Exception as e:
            self.log(f"[!] 上游代理 {addr} 错误: {e}")
            should_failover, failure_count = self._record_target_failure(
                target_host, target_port
            )
            if not should_failover:
                if self._target_failure_enabled:
                    self.log(
                        f"[目标失败] {self._target_key(target_host, target_port)} "
                        f"连续失败 {failure_count}/{self._target_failure_threshold}，暂不切换。"
                    )
                return None
            return self._try_domain_failover(
                target_host, target_port, failed_proxy=addr
            )

    @staticmethod
    def _recv_exact(sock, size):
        """读取指定字节数，处理 TCP 分包和提前断开。"""
        data = bytearray()
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("客户端在发送完整请求前断开")
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _recv_until(sock, marker, max_size=65536):
        """读取到分隔符为止，同时保留已到达的后续请求体。"""
        data = bytearray()
        while marker not in data:
            if len(data) >= max_size:
                raise ValueError("请求头超过允许大小")
            chunk = sock.recv(min(4096, max_size - len(data)))
            if not chunk:
                raise ConnectionError("客户端在发送完整请求头前断开")
            data.extend(chunk)
        return bytes(data)

    @staticmethod
    def _parse_connect_target(authority):
        parsed = urlparse(f"//{authority}")
        if not parsed.hostname or parsed.port is None:
            raise ValueError("CONNECT 请求缺少有效目标地址")
        return parsed.hostname, parsed.port

    def _handle_http_client(self, client_socket):
        """处理单个HTTP客户端连接。"""
        remote_socket = None
        try:
            request_data = self._recv_until(client_socket, b'\r\n\r\n')

            first_line = request_data.split(b'\r\n')[0].decode('utf-8', 'ignore')
            method, url, _ = first_line.split()
            method = method.upper()

            if method == 'CONNECT':
                target_host, target_port = self._parse_connect_target(url)
            else:
                parsed_url = urlparse(url)
                if not parsed_url.hostname:
                    host_header = next(
                        (
                            line.split(b':', 1)[1].strip().decode('utf-8', 'ignore')
                            for line in request_data.split(b'\r\n')[1:]
                            if line.lower().startswith(b'host:')
                        ),
                        '',
                    )
                    parsed_url = urlparse(f"//{host_header}")
                if not parsed_url.hostname:
                    raise ValueError("HTTP 请求缺少目标主机")
                target_host = parsed_url.hostname
                target_port = parsed_url.port or (
                    443 if parsed_url.scheme == 'https' else 80
                )

            remote_socket = self._get_upstream_connection(target_host, target_port)
            if not remote_socket:
                # 可以给客户端一个更友好的错误响应
                client_socket.sendall(b'HTTP/1.1 502 Bad Gateway\r\n\r\n')
                return

            if method == 'CONNECT':
                client_socket.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            else:
                remote_socket.sendall(request_data)

            self._forward_data(
                client_socket, remote_socket, target_host, target_port,
                getattr(remote_socket, '_proxy_label', '未知')
            )
        except Exception as e:
            if not isinstance(e, (ConnectionResetError, BrokenPipeError, OSError)):
                 self.log(f"处理 HTTP 请求时出错: {e}")
        finally:
            if remote_socket: remote_socket.close()
            if client_socket: client_socket.close()

    def _handle_socks5_client(self, client_socket):
        """处理单个SOCKS5客户端连接。"""
        remote_socket = None
        try:
            data = self._recv_exact(client_socket, 2)
            if data[0] != 5: return 
            nmethods = data[1]
            self._recv_exact(client_socket, nmethods)
            client_socket.sendall(b"\x05\x00")

            data = self._recv_exact(client_socket, 4)
            if data[0] != 5 or data[1] != 1: return
            
            atyp = data[3]
            if atyp == 1:
                addr = socket.inet_ntoa(self._recv_exact(client_socket, 4))
            elif atyp == 3:
                domain_len = self._recv_exact(client_socket, 1)[0]
                addr = self._recv_exact(client_socket, domain_len).decode('idna')
            elif atyp == 4:
                addr = socket.inet_ntop(socket.AF_INET6, self._recv_exact(client_socket, 16))
            else:
                client_socket.sendall(b"\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00")
                return
            
            port = struct.unpack('!H', self._recv_exact(client_socket, 2))[0]

            remote_socket = self._get_upstream_connection(addr, port)
            if not remote_socket:
                client_socket.sendall(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00") # Host unreachable
                return

            client_socket.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

            self._forward_data(
                client_socket, remote_socket, addr, port,
                getattr(remote_socket, '_proxy_label', '未知')
            )
        except Exception as e:
            if not isinstance(e, (ConnectionResetError, BrokenPipeError, OSError)):
                self.log(f"处理 SOCKS5 请求时出错: {e}")
        finally:
            if remote_socket: remote_socket.close()
            if client_socket: client_socket.close()

    def _forward_data(self, sock1, sock2, target_host, target_port, proxy_label):
        """在两个socket之间双向转发数据，直到任意一方关闭。"""
        client_to_target = 0
        target_to_client = 0
        try:
            while self._running:
                try:
                    readable, _, exceptional = select.select([sock1, sock2], [], [sock1, sock2], 5)
                    if exceptional or not readable:
                        break
                    for sock in readable:
                        other_sock = sock2 if sock is sock1 else sock1
                        data = sock.recv(8192)
                        if not data:
                            return
                        if sock is sock1:
                            client_to_target += len(data)
                        else:
                            target_to_client += len(data)
                        other_sock.sendall(data)
                except (ConnectionResetError, BrokenPipeError, OSError, select.error):
                    break
        finally:
            self.log_traffic(
                f"已断开 {target_host}:{target_port} | 上游: {proxy_label} | "
                f"上传: {client_to_target} B | 下载: {target_to_client} B"
            )
