import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from mpdrussoundpi.russound.lib.tunein import (
    MAX_PLAYLIST_BYTES, MAX_RESPONSE_BYTES, TuneInCatalog, TuneInError,
)

TUNEIN_MODULE = "mpdrussoundpi.russound.lib.tunein"


def feed(body, *, status="200"):
    return json.dumps({"head": {"status": status}, "body": body}).encode()


class FakeResponse:
    def __init__(self, body):
        self.body = body
        self.closed = False

    def read(self, size):
        return self.body[:size]

    def close(self):
        self.closed = True


class TuneInCatalogTests(unittest.TestCase):
    def test_search_normalizes_only_direct_station_results_and_caches(self):
        response = FakeResponse(feed([
            {
                "type": "audio", "item": "station", "guide_id": "s231065",
                "text": " Radio BBC ", "subtext": "News", "bitrate": "128",
                "formats": "aac", "URL": "https://station.example/live",
            },
            {"type": "link", "item": "show", "guide_id": "p123", "text": "Podcast"},
            {"type": "audio", "item": "station", "guide_id": "bad", "text": "Invalid ID"},
            {"type": "audio", "item": "station", "guide_id": "s231065", "text": "Duplicate"},
        ]))
        opener = Mock()
        opener.open.return_value = response
        catalog = TuneInCatalog()
        with patch(f"{TUNEIN_MODULE}.build_opener", return_value=opener):
            first = catalog.search("BBC Radio")
            second = catalog.search("bbc radio")
        self.assertEqual(first, [{
            "id": "s231065",
            "name": "Radio BBC",
            "description": "News",
            "bitrate_kbps": 128,
            "formats": "aac",
        }])
        self.assertEqual(second, first)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        timeout = opener.open.call_args.kwargs["timeout"]
        self.assertEqual(request.full_url, "https://opml.radiotime.com/Search.ashx?query=BBC+Radio&formats=mp3%2Caac%2Cogg&render=json")
        self.assertEqual(timeout, 3.0)
        self.assertNotIn("URL", json.dumps(first))
        self.assertTrue(response.closed)

    def test_query_and_station_identity_are_strictly_bounded(self):
        catalog = TuneInCatalog()
        for query in ("", "a", "x" * 81, "radio\n"):
            with self.assertRaises(ValueError):
                catalog.search(query)
        for station_id in ("", "p123", "s1/../../etc", "s" + "1" * 13):
            with self.assertRaises(ValueError):
                catalog.resolve(station_id)

    def test_resolution_accepts_only_supported_direct_public_streams(self):
        response = FakeResponse(feed([
            {
                "element": "audio", "is_direct": True, "media_type": "mp3",
                "url": "http://127.0.0.1/internal",
            },
            {
                "element": "audio", "is_direct": True, "media_type": "aac",
                "url": "https://stream.example/live.aac",
            },
        ]))
        opener = Mock()
        opener.open.return_value = response
        catalog = TuneInCatalog()
        with patch(f"{TUNEIN_MODULE}.build_opener", return_value=opener):
            self.assertEqual(catalog.resolve("s231065"), "https://stream.example/live.aac")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://opml.radiotime.com/Tune.ashx?id=s231065&formats=mp3%2Caac%2Cogg&render=json")

    def test_resolution_rejects_unsupported_media_and_invalid_responses(self):
        opener = Mock()
        opener.open.side_effect = [
            FakeResponse(feed([{
                "element": "audio", "is_direct": True, "media_type": "hls",
                "url": "https://stream.example/live.m3u8",
            }])),
            FakeResponse(feed([], status="500")),
            URLError("private-upstream-details"),
        ]
        catalog = TuneInCatalog()
        with patch(f"{TUNEIN_MODULE}.build_opener", return_value=opener):
            for _ in range(3):
                with self.assertRaises(TuneInError) as caught:
                    catalog.resolve("s123")
                self.assertNotIn("private-upstream-details", str(caught.exception))

    def test_search_response_size_is_bounded(self):
        opener = Mock()
        opener.open.return_value = FakeResponse(b"x" * (MAX_RESPONSE_BYTES + 1))
        catalog = TuneInCatalog()
        with patch(f"{TUNEIN_MODULE}.build_opener", return_value=opener):
            with self.assertRaisesRegex(TuneInError, "size"):
                catalog.search("BBC")

    def test_station_id_query_returns_only_that_playable_station(self):
        describe = FakeResponse(feed([{
            "element": "station", "guide_id": "s296247", "name": " METAL Radio ",
            "slogan": "Maximum Rock", "location": "Austria", "url": "https://private.example",
        }]))
        tune = FakeResponse(feed([
            {"element": "audio", "is_direct": True, "media_type": "hls", "bitrate": 64,
             "url": "https://hls.example/live.m3u8"},
            {"element": "audio", "is_direct": True, "media_type": "MP3", "bitrate": 128,
             "url": "https://stream.example/metal"},
        ]))
        opener = Mock()
        opener.open.side_effect = [describe, tune]
        catalog = TuneInCatalog()
        with patch(f"{TUNEIN_MODULE}.build_opener", return_value=opener):
            first = catalog.search(" S296247 ")
            second = catalog.search("s296247")
        self.assertEqual(first, [{
            "id": "s296247",
            "name": "METAL Radio",
            "description": "Maximum Rock · Austria",
            "bitrate_kbps": 128,
            "formats": "mp3",
        }])
        self.assertEqual(second, first)
        urls = [call.args[0].full_url for call in opener.open.call_args_list]
        self.assertEqual(urls, [
            "https://opml.radiotime.com/Describe.ashx?id=s296247&render=json",
            "https://opml.radiotime.com/Tune.ashx?id=s296247&formats=mp3%2Caac%2Cogg&render=json",
        ])
        self.assertNotIn("example", json.dumps(first))

    def test_station_id_query_without_station_or_supported_stream_is_empty(self):
        cases = [
            [FakeResponse(feed([], status="400"))],
            [FakeResponse(feed([{"element": "station", "guide_id": "s2", "name": "Other"}]))],
            [
                FakeResponse(feed([{"element": "station", "guide_id": "s1", "name": "HLS only"}])),
                FakeResponse(feed([{"element": "audio", "is_direct": True, "media_type": "hls",
                                    "url": "https://hls.example/live.m3u8"}])),
            ],
        ]
        for responses in cases:
            opener = Mock()
            opener.open.side_effect = responses
            with patch(f"{TUNEIN_MODULE}.build_opener", return_value=opener):
                self.assertEqual(TuneInCatalog().search("s1"), [])
            self.assertEqual(opener.open.call_count, len(responses))

    def test_station_id_query_reports_unreachable_tunein(self):
        describe = FakeResponse(feed([{"element": "station", "guide_id": "s1", "name": "Station"}]))
        opener = Mock()
        opener.open.side_effect = [describe, URLError("private-upstream-details")]
        with patch(f"{TUNEIN_MODULE}.build_opener", return_value=opener):
            with self.assertRaisesRegex(TuneInError, "unavailable") as caught:
                TuneInCatalog().search("s1")
        self.assertNotIn("private-upstream-details", str(caught.exception))


    def test_resolution_falls_back_to_simple_playlists_when_no_direct_stream_exists(self):
        tune = FakeResponse(feed([
            {"element": "audio", "is_direct": False, "media_type": "mp3", "playlist_type": "pls",
             "url": "http://stream.example/station.pls"},
            {"element": "audio", "is_direct": False, "media_type": "mp3", "playlist_type": "m3u",
             "url": "https://stream.example/station.m3u"},
        ]))
        pls = FakeResponse(
            b"[playlist]\nNumberOfEntries=3\nFile1=http://127.0.0.1/private\n"
            b"File2=https://stream.example/nested.m3u\nFile3=http://stream.example/live\n"
            b"Title3=Station\nLength3=-1\nVersion=2\n"
        )
        opener = Mock()
        opener.open.side_effect = [tune, pls]
        with patch(f"{TUNEIN_MODULE}.build_opener", return_value=opener):
            self.assertEqual(TuneInCatalog().resolve("s260655"), "http://stream.example/live")
        self.assertEqual(opener.open.call_args_list[1].args[0].full_url, "http://stream.example/station.pls")

    def test_resolution_tries_next_playlist_and_bounds_playlist_size(self):
        tune = FakeResponse(feed([
            {"element": "audio", "is_direct": False, "media_type": "mp3", "playlist_type": "pls",
             "url": "http://stream.example/large.pls"},
            {"element": "audio", "is_direct": False, "media_type": "mp3", "playlist_type": "m3u",
             "url": "https://stream.example/unreachable.m3u"},
            {"element": "audio", "is_direct": False, "media_type": "mp3", "playlist_type": "m3u",
             "url": "https://stream.example/station.m3u"},
            {"element": "audio", "is_direct": False, "media_type": "hls", "playlist_type": "m3u",
             "url": "https://stream.example/ignored.m3u"},
        ]))
        opener = Mock()
        opener.open.side_effect = [
            tune,
            FakeResponse(b"x" * (MAX_PLAYLIST_BYTES + 1)),
            URLError("private-upstream-details"),
            FakeResponse(b"#EXTM3U\n#EXTINF:-1,Station\nhttps://stream.example/live\n"),
        ]
        with patch(f"{TUNEIN_MODULE}.build_opener", return_value=opener):
            self.assertEqual(TuneInCatalog().resolve("s1"), "https://stream.example/live")
        self.assertEqual(opener.open.call_count, 4)

    def test_resolution_without_any_supported_stream_fails_safely(self):
        tune = FakeResponse(feed([
            {"element": "audio", "is_direct": False, "media_type": "mp3", "playlist_type": "m3u",
             "url": "https://stream.example/station.m3u"},
        ]))
        opener = Mock()
        opener.open.side_effect = [tune, FakeResponse(b"#EXTM3U\nftp://stream.example/live\n")]
        with patch(f"{TUNEIN_MODULE}.build_opener", return_value=opener):
            with self.assertRaisesRegex(TuneInError, "supported stream"):
                TuneInCatalog().resolve("s1")

if __name__ == "__main__":
    unittest.main()
