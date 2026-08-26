"""输出目录名 / 音频文件名必须落在 Linux NAME_MAX（255 字节）内。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import (  # noqa: E402
    _NAME_MAX_BYTES,
    _output_dir_name,
    _utf8_clip,
    safe_basename,
    sanitize_filename,
)

# 线上报错的 BV1Tegj6nE3c 标题（单 P 时还会被拼成 title-title）
_LONG_TITLE = (
    "杨植麟39分钟-讲透Kimi下一步要做什参数模型如何稳定训练-超长上下文如何降低成本"
    "-文字-图片和视频如何共同学习-以及一个主智能体-怎样调度多个子智能体并行完"
    "-杨植麟39分钟-讲透Kimi下一步要做"
)


class Utf8ClipTest(unittest.TestCase):
    def test_short_unchanged(self):
        self.assertEqual(_utf8_clip("hello", 10), "hello")

    def test_does_not_split_cjk(self):
        text = "杨植麟下一步"
        clipped = _utf8_clip(text, 7)
        clipped.encode("utf-8")  # must be valid
        self.assertLessEqual(len(clipped.encode("utf-8")), 7)
        self.assertTrue(text.startswith(clipped))


class SanitizeFilenameTest(unittest.TestCase):
    def test_cjk_under_default_byte_cap(self):
        name = sanitize_filename(_LONG_TITLE)
        self.assertLessEqual(len(name.encode("utf-8")), 180)
        self.assertGreater(len(name), 0)

    def test_old_100_char_slice_exceeds_name_max(self):
        """回归：按 100 字符截断中文会超过 255 字节。"""
        old = _LONG_TITLE[:100]
        self.assertGreater(len(old.encode("utf-8")), _NAME_MAX_BYTES)


class SafeBasenameTest(unittest.TestCase):
    def test_audio_fits_name_max(self):
        name = safe_basename(_LONG_TITLE, ext="m4a")
        self.assertLessEqual(len(name.encode("utf-8")), _NAME_MAX_BYTES)
        self.assertTrue(name.endswith(".m4a"))

    def test_audio_with_bvid_fits(self):
        name = safe_basename(_LONG_TITLE, extra="BV1Tegj6nE3c", ext="m4a")
        self.assertLessEqual(len(name.encode("utf-8")), _NAME_MAX_BYTES)
        self.assertIn("BV1Tegj6nE3c", name)
        self.assertTrue(name.endswith(".m4a"))

    def test_can_create_file_on_disk(self):
        name = safe_basename(_LONG_TITLE, ext="m4a")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.write_bytes(b"ok")
            self.assertTrue(path.exists())


class OutputDirNameTest(unittest.TestCase):
    def test_long_cjk_dir_fits_name_max(self):
        fake_root = Path("/home/soft/projects/bilibili-scraper/outputs")
        with mock.patch("app.OUTPUT_DIR", fake_root):
            stem = sanitize_filename(_LONG_TITLE)
            name = _output_dir_name(stem, "20260826-BV1Tegj6nE3c-p1")
        self.assertLessEqual(len(name.encode("utf-8")), _NAME_MAX_BYTES)
        self.assertIn("BV1Tegj6nE3c", name)

    def test_can_mkdir(self):
        fake_root = Path("/home/soft/projects/bilibili-scraper/outputs")
        with mock.patch("app.OUTPUT_DIR", fake_root):
            stem = f"{sanitize_filename(_LONG_TITLE)}-{sanitize_filename(_LONG_TITLE)}"
            name = _output_dir_name(stem, "20260826-BV1Tegj6nE3c-p1")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / name
            path.mkdir()
            audio = path / safe_basename(_LONG_TITLE, ext="m4a")
            audio.write_bytes(b"ok")
            self.assertTrue(audio.exists())
            self.assertLessEqual(len(os.fsencode(audio.name)), _NAME_MAX_BYTES)
            self.assertLessEqual(len(os.fsencode(path.name)), _NAME_MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
