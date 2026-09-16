import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from uploader.bilibili_uploader.runtime import (
    build_biliup_runtime_path,
    build_fallback_release,
    ensure_biliup_binary,
    fetch_latest_release,
    fetch_release_via_web_fallback,
    run_biliup_command,
)


class BiliupRuntimeTests(unittest.TestCase):
    def test_build_biliup_runtime_path_returns_platform_path(self):
        path = build_biliup_runtime_path("Windows")
        self.assertTrue(str(path).endswith("biliup.exe"))

    @patch("uploader.bilibili_uploader.runtime.fetch_latest_release")
    def test_ensure_biliup_binary_downloads_when_missing(self, mock_release):
        mock_release.return_value = {
            "tag_name": "v1.0.0",
            "asset_url": "https://example.invalid/biliup.exe",
            "asset_name": "biliup.exe",
        }
        with patch("uploader.bilibili_uploader.runtime.read_local_biliup_version", return_value=None):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("uploader.bilibili_uploader.runtime.download_biliup_asset") as mock_download:
                    with patch("uploader.bilibili_uploader.runtime.write_local_biliup_version") as mock_write_version:
                        ensure_biliup_binary(force_check=True)
        mock_download.assert_called_once()
        mock_write_version.assert_called_once_with("v1.0.0")

    @patch("uploader.bilibili_uploader.runtime.fetch_latest_release")
    def test_ensure_biliup_binary_reuses_local_when_up_to_date(self, mock_release):
        mock_release.return_value = {
            "tag_name": "v1.0.0",
            "asset_url": "https://example.invalid/biliup.exe",
            "asset_name": "biliup.exe",
        }
        with patch("uploader.bilibili_uploader.runtime.read_local_biliup_version", return_value="v1.0.0"):
            with patch("pathlib.Path.exists", return_value=True):
                with patch("uploader.bilibili_uploader.runtime.download_biliup_asset") as mock_download:
                    with patch("uploader.bilibili_uploader.runtime.write_local_biliup_version") as mock_write_version:
                        ensure_biliup_binary(force_check=True)
        mock_download.assert_not_called()
        mock_write_version.assert_not_called()

    @patch("uploader.bilibili_uploader.runtime.subprocess.run")
    @patch("uploader.bilibili_uploader.runtime.ensure_biliup_binary")
    def test_run_biliup_command_returns_completed_process(self, mock_ensure_binary, mock_run):
        mock_ensure_binary.return_value = build_biliup_runtime_path("Windows")
        mock_run.return_value = Mock(returncode=0, stdout="ok", stderr="")
        result = run_biliup_command(["login"])
        self.assertEqual(result.returncode, 0)

    @patch("uploader.bilibili_uploader.runtime.subprocess.run")
    @patch("uploader.bilibili_uploader.runtime.ensure_biliup_binary")
    def test_run_biliup_command_login_uses_interactive_stdio(self, mock_ensure_binary, mock_run):
        mock_ensure_binary.return_value = Path("C:/mock/biliup.exe")
        mock_run.return_value = Mock(returncode=0, stdout="", stderr="")
        run_biliup_command(["login"], interactive=True)
        _, kwargs = mock_run.call_args
        self.assertNotIn("capture_output", kwargs)

    @patch("uploader.bilibili_uploader.runtime.requests.get")
    def test_fetch_latest_release_falls_back_to_web_page_on_rate_limit(self, mock_get):
        mock_get.side_effect = [
            Mock(raise_for_status=Mock(side_effect=requests.HTTPError("403 rate limit"))),
            Mock(
                url="https://github.com/biliup/biliup/releases/tag/v1.2.4",
                raise_for_status=Mock(return_value=None),
                text="Release v1.2.4 · biliup/biliup · GitHub",
            ),
        ]
        with patch(
            "uploader.bilibili_uploader.runtime.build_fallback_release",
            return_value={"tag_name": "v1.2.4", "asset_name": "biliupR-v1.2.4-aarch64-macos.tar.xz", "asset_url": "https://example.invalid/x"},
        ) as mock_fallback:
            release = fetch_latest_release()
        self.assertEqual(release["tag_name"], "v1.2.4")
        mock_fallback.assert_called_once_with("v1.2.4")

    def test_extract_tag_from_url(self):
        from uploader.bilibili_uploader.runtime import _extract_tag_from_url

        self.assertEqual(
            _extract_tag_from_url("https://github.com/biliup/biliup/releases/tag/v1.2.4"),
            "v1.2.4",
        )
        self.assertIsNone(_extract_tag_from_url("https://github.com/biliup/biliup/releases"))

    @patch("uploader.bilibili_uploader.runtime.requests.head")
    def test_build_fallback_release_probes_candidate_assets(self, mock_head):
        # 第一个候选(带 R 前缀)直接命中
        mock_head.return_value = Mock(status_code=200)
        release = build_fallback_release("v1.2.4")
        self.assertIn("biliupR-v1.2.4-", release["asset_name"])
        self.assertTrue(release["asset_url"].endswith(release["asset_name"]))
        mock_head.assert_called_once()

    @patch("uploader.bilibili_uploader.runtime.requests.get")
    def test_web_fallback_raises_when_tag_not_resolved(self, mock_get):
        mock_get.return_value = Mock(
            url="https://github.com/biliup/biliup/releases",
            raise_for_status=Mock(return_value=None),
            text="no tag here",
        )
        with self.assertRaises(RuntimeError):
            fetch_release_via_web_fallback()
