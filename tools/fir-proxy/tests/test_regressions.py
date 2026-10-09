import io
import queue
import socket
import sys
import threading
import unittest
from unittest.mock import patch

from cli import load_proxies
from hq import deduce_protocol
from modules.checker import ProxyChecker
from modules.rotator import ProxyRotator
from modules.server import ProxyServer


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            'headers': {'X-Forwarded-For': '203.0.113.10'},
            'origin': '203.0.113.10',
        }


class FakeSession:
    def head(self, *args, **kwargs):
        return FakeResponse()

    def get(self, *args, **kwargs):
        return FakeResponse()


class RegressionTests(unittest.TestCase):
    def test_unmatched_rotator_filter_returns_without_deadlock(self):
        rotator = ProxyRotator()
        rotator.add_proxy({
            'proxy': '127.0.0.1:8080',
            'location': '中国',
            'status': 'Working',
        })
        rotator.set_filters('不存在的地区')
        completed = threading.Event()
        results = []

        def select_proxy():
            results.append(rotator.get_next_proxy())
            completed.set()

        threading.Thread(target=select_proxy, daemon=True).start()
        self.assertTrue(completed.wait(1))
        self.assertEqual(results, [None])
        self.assertEqual(rotator.current_filter_region, '不存在的地区')

    def test_transparent_proxy_is_working(self):
        checker = ProxyChecker(timeout=1)
        checker.session = FakeSession()
        checker.public_ip = '203.0.113.10'
        checker._get_proxy_location = lambda _host: '测试'

        result = checker._full_check_proxy({
            'proxy': '127.0.0.1:8080',
            'protocol': 'http',
        })

        self.assertEqual(result['anonymity'], 'Transparent')
        self.assertEqual(result['status'], 'Working')
        self.assertEqual(result['location'], '测试')

    def test_precheck_failure_is_reported_as_a_result(self):
        checker = ProxyChecker(timeout=1)
        checker._pre_check_proxy = lambda _proxy: False
        result_queue = queue.Queue()
        log_queue = queue.Queue()

        checker.validate_all(
            {'http': ['127.0.0.1:8080']},
            result_queue,
            log_queue,
            max_workers=1,
        )

        result = result_queue.get_nowait()
        self.assertEqual(result['status'], 'Failed')
        self.assertEqual(result['failure_stage'], 'tcp_precheck')
        self.assertIsNone(result_queue.get_nowait())

    def test_server_start_fails_when_port_is_occupied(self):
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        occupied.bind(('127.0.0.1', 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        try:
            server = ProxyServer(
                '127.0.0.1', port,
                '127.0.0.1', 0,
                ProxyRotator(), queue.Queue(),
            )
            self.assertFalse(server.start_all())
            self.assertFalse(server._running)
            self.assertIsNone(server._http_server_socket)
            self.assertIsNone(server._socks5_server_socket)
        finally:
            occupied.close()

    def test_socket_helpers_support_fragmented_and_ipv6_requests(self):
        reader, writer = socket.socketpair()
        try:
            writer.sendall(b'ab')

            def send_rest():
                writer.sendall(b'cd')

            thread = threading.Thread(target=send_rest)
            thread.start()
            self.assertEqual(ProxyServer._recv_exact(reader, 4), b'abcd')
            thread.join()
        finally:
            reader.close()
            writer.close()

        self.assertEqual(
            ProxyServer._parse_connect_target('[2001:db8::1]:443'),
            ('2001:db8::1', 443),
        )

    def test_server_rejects_connections_when_handler_limit_is_reached(self):
        server = ProxyServer(
            '127.0.0.1', 0,
            '127.0.0.1', 0,
            ProxyRotator(), queue.Queue(),
        )
        server._handler_slots = threading.BoundedSemaphore(1)
        started = threading.Event()
        release_handler = threading.Event()
        client_one, peer_one = socket.socketpair()
        client_two, peer_two = socket.socketpair()
        try:
            def block_handler(_client_socket):
                started.set()
                release_handler.wait(1)

            self.assertTrue(server._start_client_handler(block_handler, client_one))
            self.assertTrue(started.wait(1))
            self.assertFalse(server._start_client_handler(block_handler, client_two))
            self.assertEqual(peer_two.recv(1), b'')
            release_handler.set()
        finally:
            client_one.close()
            client_two.close()
            peer_one.close()
            peer_two.close()

    def test_stdin_accepts_jsonl_and_hq_preserves_socks4(self):
        stdin = io.StringIO(
            '{"protocol":"http","proxy":"127.0.0.1:8080"}\n'
            '{"protocol":"socks4","proxy":"127.0.0.1:1080"}\n'
        )
        with patch.object(sys, 'stdin', stdin):
            records, invalid = load_proxies('-')

        self.assertEqual(invalid, 0)
        self.assertEqual([record['protocol'] for record in records], ['http', 'socks4'])
        self.assertEqual(
            deduce_protocol('socks4://127.0.0.1:1080', 'http'),
            'socks4',
        )

    def test_stdin_accepts_a_single_json_proxy_object(self):
        with patch.object(
            sys,
            'stdin',
            io.StringIO('{"protocol":"http","proxy":"127.0.0.1:8080"}'),
        ):
            records, invalid = load_proxies('-')

        self.assertEqual(invalid, 0)
        self.assertEqual(
            records,
            [{
                'protocol': 'http',
                'proxy': '127.0.0.1:8080',
                'status': 'Working',
            }],
        )


if __name__ == '__main__':
    unittest.main()
