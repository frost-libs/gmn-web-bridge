# SPDX-License-Identifier: MIT
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import httpx

from gmn_web_bridge.__main__ import startup_error
from gmn_web_bridge.client import GeminiClient, GeminiError
from gmn_web_bridge.config import Settings
from gmn_web_bridge.uploads import ImageFetchError, fetch_image, sniff_mime
from test_server import _settings


def _page(build_label: str, at: str | None = None) -> str:
    parts = [f'"cfb2h":"{build_label}"', '"FdrFJe":"sid"']
    if at:
        parts.append(f'"SNlM0e":"{at}"')
    return " ".join(parts)


def _stream(text: str) -> str:
    inner = json.dumps([None, None, None, None, [[None, [text]]]])
    outer = json.dumps([["wrb.fr", None, inner]])
    return ")]}'\n" + outer + "\n"


class StartupTest(unittest.TestCase):
    def test_the_running_container_environment_stays_in_local_mode(self):
        env = {
            "GMN_API_KEY": "sk-test",
            "GMN_COOKIE": "SID=a; SAPISID=b",
            "GMN_AUTH_USER": "0",
            "GMN_PORT": "8091",
            "GMN_TEMPORARY_CHATS": "true",
            "GMN_XSRF_TOKEN": "tok",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.mode, "local")
        self.assertEqual(settings.port, 8091)
        self.assertTrue(settings.temporary_chats)
        self.assertEqual(settings.cookie, "SID=a; SAPISID=b")
        self.assertEqual(settings.upstream_proxy, "")
        self.assertFalse(settings.log_requests)
        self.assertFalse(settings.cors)
        self.assertTrue(settings.fetch_image_urls)
        self.assertIsNone(startup_error(settings))

    def test_local_mode_requires_a_cookie_and_both_modes_require_a_key(self):
        self.assertIn("GMN_COOKIE", startup_error(_settings(cookie="", mode="local")) or "")
        self.assertIsNone(startup_error(_settings(cookie="", mode="anonymous")))
        self.assertIn("GMN_API_KEY", startup_error(_settings(api_key="", mode="local")) or "")
        self.assertIn("GMN_API_KEY", startup_error(_settings(api_key="", mode="anonymous")) or "")

    def test_an_unknown_mode_is_rejected(self):
        with patch.dict(os.environ, {"GMN_MODE": "proxy"}, clear=True):
            with self.assertRaises(ValueError):
                Settings.from_env()


class SessionTest(unittest.TestCase):
    def test_an_anonymous_request_carries_no_session(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "GET":
                return httpx.Response(200, text=_page("boq_anon"))
            return httpx.Response(200, text=_stream("Hi"))

        client = GeminiClient(
            _settings(mode="anonymous", cookie="", xsrf_token="ignored", auth_user="1"),
            http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        try:
            self.assertEqual("".join(client.generate("Hello", "gemini-3.8-flash")), "Hi")
        finally:
            client.close()

        self.assertTrue(str(seen[0].url).endswith("/app"))
        post = seen[1]
        names = {name.lower() for name in post.headers}
        self.assertNotIn("cookie", names)
        self.assertNotIn("authorization", names)
        self.assertNotIn(b"at=", post.content)
        self.assertNotIn("/u/", str(post.url))

    def test_anonymous_mode_with_a_cookie_uses_the_signed_in_path(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "GET":
                return httpx.Response(200, text=_page("boq_user", at="token"))
            return httpx.Response(200, text=_stream("Hi"))

        client = GeminiClient(
            _settings(mode="anonymous", cookie="SID=a; SAPISID=b", auth_user="1"),
            http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        try:
            self.assertEqual("".join(client.generate("Hello", "gemini-3.8-flash")), "Hi")
        finally:
            client.close()

        self.assertIn("/u/1/app", str(seen[0].url))
        self.assertIn("/u/1/", str(seen[1].url))
        self.assertIn("cookie", {name.lower() for name in seen[1].headers})
        self.assertIn(b"at=", seen[1].content)

    def test_a_stale_build_label_is_read_again_once(self):
        gets = {"n": 0}
        posts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                gets["n"] += 1
                label = "boq_old" if gets["n"] == 1 else "boq_new"
                return httpx.Response(200, text=_page(label, at="token"))
            posts.append(str(request.url))
            if len(posts) == 1:
                return httpx.Response(405, text="stale")
            return httpx.Response(200, text=_stream("OK"))

        client = GeminiClient(
            _settings(cookie="SID=a; SAPISID=b"),
            http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        try:
            self.assertEqual("".join(client.generate("Hello", "gemini-3.8-flash")), "OK")
        finally:
            client.close()

        self.assertEqual(len(posts), 2)
        self.assertIn("boq_old", posts[0])
        self.assertIn("boq_new", posts[1])

    def test_a_pinned_build_label_is_not_refreshed(self):
        posts = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, text=_page("boq_page", at="token"))
            posts["n"] += 1
            return httpx.Response(405, text="stale")

        client = GeminiClient(
            _settings(cookie="SID=a; SAPISID=b", build_label="boq_pin"),
            http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        try:
            with self.assertRaises(GeminiError) as raised:
                "".join(client.generate("Hello", "gemini-3.8-flash"))
        finally:
            client.close()

        self.assertEqual(posts["n"], 1)
        self.assertIn("build label", str(raised.exception))

    def test_an_anonymous_upload_rejection_names_the_cookie(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.host == "gemini.google.com":
                return httpx.Response(200, text=_page("boq_anon"))
            return httpx.Response(403, text="no")

        client = GeminiClient(
            _settings(mode="anonymous", cookie=""),
            http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        try:
            with self.assertRaises(GeminiError) as raised:
                "".join(client.generate("Hello", "gemini-3.8-flash", [(b"png", "image/png", "image.png")]))
        finally:
            client.close()

        self.assertEqual(raised.exception.status, 502)
        self.assertIn("Set GMN_COOKIE", str(raised.exception))

    def test_image_urls_can_be_turned_off(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, text=_page("boq_anon"))

        client = GeminiClient(
            _settings(mode="anonymous", cookie="", fetch_image_urls=False),
            http=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        try:
            with self.assertRaises(GeminiError) as raised:
                "".join(
                    client.generate(
                        "Hello",
                        "gemini-3.8-flash",
                        [("https://example.com/a.png", "image/png", "image.png")],
                    )
                )
        finally:
            client.close()

        self.assertEqual(raised.exception.status, 400)
        self.assertNotIn("example.com", " ".join(seen))


class ProxyAndImageTest(unittest.TestCase):
    def test_the_upstream_proxy_is_passed_to_httpx(self):
        with patch("gmn_web_bridge.client.httpx.Client") as ctor:
            ctor.return_value = MagicMock()
            GeminiClient(_settings(upstream_proxy="http://127.0.0.1:7890"))
            self.assertEqual(ctor.call_args.kwargs["proxy"], "http://127.0.0.1:7890")

    def test_https_proxy_is_left_for_httpx(self):
        proxy_env = {
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "https_proxy": "http://127.0.0.1:7890",
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "http_proxy": "http://127.0.0.1:7890",
            "ALL_PROXY": "http://127.0.0.1:7890",
            "all_proxy": "http://127.0.0.1:7890",
        }
        with patch.dict(os.environ, proxy_env):
            client = GeminiClient(_settings())
            try:
                urls = []
                for mount in client._http._mounts.values():
                    pool = getattr(mount, "_pool", None)
                    proxy_url = getattr(pool, "_proxy_url", None)
                    if proxy_url is not None:
                        urls.append(str(proxy_url))
            finally:
                client.close()

        self.assertTrue(any("127.0.0.1" in url and "7890" in url for url in urls))

    def test_an_image_url_is_sniffed_and_capped(self):
        png = b"\x89PNG\r\n\x1a\nrest"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/big":
                return httpx.Response(200, content=b"x" * 8)
            return httpx.Response(200, content=png, headers={"content-type": "application/octet-stream"})

        http = httpx.Client(transport=httpx.MockTransport(handler))
        try:
            data, mime = fetch_image(http, "https://example.com/a", "image/jpeg")
            self.assertEqual(data, png)
            self.assertEqual(mime, "image/png")
            self.assertEqual(sniff_mime(png), "image/png")
            with self.assertRaises(ImageFetchError):
                fetch_image(http, "https://example.com/big", "image/png", limit=4)
            with self.assertRaises(ImageFetchError):
                fetch_image(http, "file:///etc/passwd", "image/png")
        finally:
            http.close()
