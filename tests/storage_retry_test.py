import unittest
from unittest.mock import patch

from baidupcs_py.baidupcs.pcs import BaiduPCS, Method

from backend.storage import BaiduStorage, _extract_shared_page_data, api_retry


class SharedPageParserTest(unittest.TestCase):
    def test_parses_legacy_yundata_page(self):
        html = '<script>yunData.setData({"uk": 1, "share_id": 2});</script>'

        self.assertEqual(
            _extract_shared_page_data(html),
            {"uk": 1, "share_id": 2},
        )

    def test_parses_legacy_locals_mset_page(self):
        html = '<script>locals.mset({"uk": 3});</script>'

        self.assertEqual(_extract_shared_page_data(html), {"uk": 3})

    def test_parses_current_locals_data_script(self):
        html = '''
        <html><body>
        <script type="application/json" class="js-data locals-data" id="locals">
        {"uk": 4, "share_id": 5, "file_list": [{"path": "/A股数据_分笔数据"}]}
        </script>
        </body></html>
        '''

        self.assertEqual(
            _extract_shared_page_data(html),
            {
                "uk": 4,
                "share_id": 5,
                "file_list": [{"path": "/A股数据_分笔数据"}],
            },
        )

    def test_reports_terminal_error_for_unknown_page(self):
        with self.assertRaisesRegex(
            AssertionError,
            "`BaiduPCS.shared_paths`: Don't get shared info",
        ):
            _extract_shared_page_data('<html><body>验证码</body></html>')

    def test_patch_is_applied_to_baidupcs_shared_paths(self):
        class Cookies:
            def get_dict(self):
                return {"STOKEN": "updated"}

        class Response:
            text = '<script class="locals-data" type="application/json">{"uk": 6}</script>'
            cookies = Cookies()

        client = BaiduPCS.__new__(BaiduPCS)
        client._stoken = "token"
        client._request = lambda method, url, params=None: Response()
        updated_cookies = {}
        client._cookies_update = updated_cookies.update

        result = BaiduPCS.shared_paths(
            client,
            "https://pan.baidu.com/s/example",
        )

        self.assertEqual(result, {"uk": 6})
        self.assertEqual(updated_cookies, {"STOKEN": "updated"})
        self.assertTrue(BaiduPCS.shared_paths._shared_page_compat_patch)


class ApiRetryConfigTest(unittest.TestCase):
    def test_uses_runtime_retry_config(self):
        class DummyStorage:
            def __init__(self):
                self.config = {
                    "retry": {
                        "max_attempts": 3,
                        "delay_seconds": 61,
                    }
                }
                self.calls = 0

            @api_retry(max_retries=1, delay_range=(2, 3))
            def request(self):
                self.calls += 1
                if self.calls < 4:
                    raise RuntimeError("temporary error")
                return "ok"

        storage = DummyStorage()

        with patch("backend.storage._interruptible_sleep") as sleep_mock:
            self.assertEqual(storage.request(), "ok")

        self.assertEqual(storage.calls, 4)
        self.assertEqual(sleep_mock.call_count, 3)
        sleep_mock.assert_any_call(61.0, None)

    def test_does_not_retry_terminal_baidu_error(self):
        class DummyStorage:
            config = {
                "retry": {
                    "max_attempts": 3,
                    "delay_seconds": 61,
                }
            }

            def __init__(self):
                self.calls = 0

            @api_retry(max_retries=1, delay_range=(2, 3))
            def request(self):
                self.calls += 1
                raise RuntimeError("error_code: 115")

        storage = DummyStorage()

        with patch("backend.storage._interruptible_sleep") as sleep_mock:
            with self.assertRaisesRegex(RuntimeError, "error_code: 115"):
                storage.request()

        self.assertEqual(storage.calls, 1)
        sleep_mock.assert_not_called()

    def test_reaccesses_share_before_retrying_shared_page(self):
        class DummyClient:
            def __init__(self):
                self.access_calls = 0
                self.shared_path_calls = 0

            def access_shared(self, shared_url, password, show_vcode=False):
                self.access_calls += 1

            def shared_paths(self, shared_url):
                self.shared_path_calls += 1
                if self.shared_path_calls < 4:
                    raise AssertionError("`BaiduPCS.shared_paths`: Don't get shared info")
                return ["ready"]

        storage = BaiduStorage.__new__(BaiduStorage)
        storage.config = {
            "retry": {
                "max_attempts": 3,
                "delay_seconds": 61,
            }
        }
        client = DummyClient()

        with patch("backend.storage._interruptible_sleep"):
            result = storage._access_and_get_shared_paths_with_retry(
                "https://example.invalid/share",
                "1234",
                client=client,
            )

        self.assertEqual(result, ["ready"])
        self.assertEqual(client.access_calls, 4)
        self.assertEqual(client.shared_path_calls, 4)


if __name__ == "__main__":
    unittest.main()
