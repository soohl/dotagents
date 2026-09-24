"""Verify that browser egress cannot connect to private networks or re-resolve DNS."""
import asyncio
import importlib.util
from pathlib import Path
import socket
import unittest
from unittest.mock import AsyncMock, patch

spec = importlib.util.spec_from_file_location('browser_proxy', Path(__file__).resolve().parents[1] / 'src/browser_proxy.py')
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)


class AddressTests(unittest.TestCase):
    def test_special_addresses_are_blocked(self):
        for ip in ['127.0.0.1', '10.0.0.1', '192.168.1.1', '172.20.0.2',
                   '169.254.169.254', '100.64.0.1', '0.0.0.0', '224.0.0.1',
                   '::1', 'fc00::1', 'fe80::1', 'ff02::1', '::ffff:127.0.0.1',
                   '2002:7f00:1::', '2001:db8::1', '64:ff9b::7f00:1']:
            self.assertFalse(proxy.public_address(ip), ip)
        for ip in ['1.1.1.1', '8.8.8.8', '2606:4700:4700::1111']:
            self.assertTrue(proxy.public_address(ip), ip)


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_mixed_dns_answers_fail_closed(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443))
                   for ip in ('1.1.1.1', '127.0.0.1')]
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=answers)), \
             patch.object(asyncio, 'open_connection', AsyncMock()) as connect:
            with self.assertRaises(ValueError):
                await proxy.public_connection('mixed.example', 443)
            connect.assert_not_called()

    async def test_connect_uses_validated_ip_not_hostname(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))]
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=answers)) as resolve, \
             patch.object(asyncio, 'open_connection', AsyncMock(return_value=('reader', 'writer'))) as connect:
            self.assertEqual(await proxy.public_connection('rebind.example', 443), ('reader', 'writer'))
            connect.assert_awaited_once_with('1.1.1.1', 443, family=socket.AF_INET)
            resolve.assert_awaited_once()

    async def test_non_web_port_fails_before_dns(self):
        with patch.object(asyncio, 'open_connection', AsyncMock()) as connect:
            with self.assertRaises(ValueError):
                await proxy.public_connection('1.1.1.1', 22)
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
