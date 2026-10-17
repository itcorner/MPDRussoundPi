import socket
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler, Request

from mpdrussoundpi.russound.lib import publicnetwork as network


MODULE = network.__name__


def answer(address, port=443):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    destination = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return family, socket.SOCK_STREAM, 6, "", destination


class PublicNetworkTests(unittest.TestCase):
    def test_public_ipv4_and_ipv6_and_special_use_addresses(self):
        for address in ("1.1.1.1", "93.184.216.34", "192.0.0.9", "2606:4700:4700::1111"):
            with self.subTest(address=address):
                self.assertTrue(network.is_public_address(address))
        for address in (
            "0.0.0.0", "127.0.0.1", "127.1.2.3", "10.1.2.3", "172.16.0.1",
            "192.168.1.1", "169.254.169.254", "100.64.0.1", "192.0.0.8",
            "192.0.2.1", "198.18.0.1", "224.0.0.1", "255.255.255.255",
            "::", "::1", "::ffff:127.0.0.1", "fc00::1", "fd00::1", "fe80::1",
            "ff02::1", "64:ff9b::a00:1", "2002:7f00:1::", "2001:db8::1",
        ):
            with self.subTest(address=address):
                self.assertFalse(network.is_public_address(address))

    def test_dns_checks_every_answer_and_rejects_mixed_or_empty_results(self):
        for answers in (
            [], [answer("192.168.1.1")],
            [answer("1.1.1.1"), answer("::1")],
            [answer("1.1.1.1"), answer("169.254.1.1")],
        ):
            with patch(f"{MODULE}.socket.getaddrinfo", return_value=answers):
                with self.assertRaises(network.PublicNetworkError):
                    network.validate_public_url("https://radio.example/live")
        with patch(f"{MODULE}.socket.getaddrinfo", return_value=[answer("1.1.1.1"), answer("2606:4700::1")]):
            self.assertEqual(network.validate_public_url("https://radio.example/live"),
                             "https://radio.example/live")

    def test_literal_private_destinations_do_not_trigger_dns(self):
        with patch(f"{MODULE}.socket.getaddrinfo") as resolver:
            for url in ("http://127.0.0.1/", "http://[::1]/", "http://10.0.0.1/"):
                with self.assertRaises(network.PublicNetworkError):
                    network.validate_public_url(url)
            resolver.assert_not_called()

    def test_dns_failures_are_safe_and_invalid_schemes_and_credentials_rejected(self):
        with patch(f"{MODULE}.socket.getaddrinfo", side_effect=socket.gaierror("secret-hostname")):
            with self.assertRaisesRegex(network.PublicNetworkError, "DNS lookup failed") as caught:
                network.validate_public_url("https://radio.example/")
        self.assertNotIn("secret", str(caught.exception))
        for url in ("file:///etc/passwd", "ftp://radio.example/", "https://user:secret@radio.example/"):
            with self.assertRaises(ValueError):
                network.validate_public_url(url)

    def test_connection_pins_the_checked_address_without_a_second_lookup(self):
        connection = Mock()
        with patch(f"{MODULE}.socket.getaddrinfo", return_value=[answer("93.184.216.34", 8000)]) as resolver:
            with patch(f"{MODULE}.socket.socket", return_value=connection):
                self.assertIs(network._connect_public(("radio.example", 8000), 3), connection)
        resolver.assert_called_once_with("radio.example", 8000, type=socket.SOCK_STREAM)
        connection.connect.assert_called_once_with(("93.184.216.34", 8000))

    def test_rebinding_between_validation_and_connection_is_rejected(self):
        with patch(f"{MODULE}.socket.getaddrinfo", side_effect=[
            [answer("1.1.1.1")], [answer("127.0.0.1")],
        ]):
            network.validate_public_url("https://radio.example/")
            with patch(f"{MODULE}.socket.socket") as create:
                with self.assertRaises(network.PublicNetworkError):
                    network._connect_public(("radio.example", 443), 3)
                create.assert_not_called()

    def test_failed_socket_closed_and_next_public_address_tried(self):
        first, second = Mock(), Mock()
        first.connect.side_effect = OSError("unreachable")
        with patch(f"{MODULE}.socket.getaddrinfo", return_value=[answer("1.1.1.1"), answer("8.8.8.8")]):
            with patch(f"{MODULE}.socket.socket", side_effect=[first, second]):
                self.assertIs(network._connect_public(("radio.example", 443), 3), second)
        first.close.assert_called_once()

    def test_unavailable_address_family_does_not_prevent_public_ipv4_fallback(self):
        connection = Mock()
        with patch(f"{MODULE}.socket.getaddrinfo", return_value=[
            answer("2606:4700::1"), answer("1.1.1.1"),
        ]):
            with patch(f"{MODULE}.socket.socket", side_effect=[OSError("IPv6 disabled"), connection]):
                self.assertIs(network._connect_public(("radio.example", 443), 3), connection)
        connection.connect.assert_called_once_with(("1.1.1.1", 443))

    def test_tls_uses_original_hostname_and_default_certificate_validation(self):
        context, connection = Mock(), Mock()
        context.wrap_socket.return_value = Mock()
        with patch(f"{MODULE}._connect_public", return_value=connection):
            client = network._PublicHTTPSConnection("radio.example", timeout=3, context=context)
            client.connect()
        context.wrap_socket.assert_called_once_with(connection, server_hostname="radio.example")
        normal = network._PublicHTTPSConnection("radio.example", timeout=3)
        self.assertEqual(normal._context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(normal._context.check_hostname)

    def test_http_host_header_keeps_original_hostname(self):
        connection = Mock()
        with patch(f"{MODULE}._connect_public", return_value=connection):
            client = network._PublicHTTPConnection("radio.example", port=8000, timeout=3)
            client.request("GET", "/live")
        request_bytes = b"".join(call.args[0] for call in connection.sendall.call_args_list)
        self.assertIn(b"Host: radio.example:8000\r\n", request_bytes)

    def test_redirects_revalidate_dns_for_public_and_private_targets(self):
        handler = network._PublicRedirect()
        request = Request("https://radio.example/start")
        with patch(f"{MODULE}.socket.getaddrinfo", return_value=[answer("1.1.1.1")]):
            redirected = handler.redirect_request(
                request, None, 302, "Found", {}, "https://cdn.example/live",
            )
            self.assertEqual(redirected.full_url, "https://cdn.example/live")
        with patch(f"{MODULE}.socket.getaddrinfo", return_value=[answer("192.168.1.1")]):
            with self.assertRaises(network.PublicNetworkError):
                handler.redirect_request(request, None, 302, "Found", {}, "http://lan.example/")
        self.assertEqual(handler.max_redirections, 4)

    def test_environment_proxies_are_not_used(self):
        with patch.dict("os.environ", {"https_proxy": "http://127.0.0.1:8888",
                                       "http_proxy": "http://127.0.0.1:8888"}):
            opener = network.public_opener()
        for handler in opener.handlers:
            if isinstance(handler, ProxyHandler):
                self.assertEqual(handler.proxies, {})


if __name__ == "__main__":
    unittest.main()
