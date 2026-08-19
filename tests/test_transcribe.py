"""Groq / 本地转写调度的单元测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import (
    Job,
    _groq_payload_segments,
    format_transcript_segments,
    transcribe_audio,
    transcribe_language,
    transcribe_provider,
)


class TranscribeProviderTest(unittest.TestCase):
    def test_explicit_local_wins(self):
        with mock.patch.dict(os.environ, {"TRANSCRIBE_PROVIDER": "local", "GROQ_API_KEY": "gsk_x"}):
            self.assertEqual(transcribe_provider(), "local")

    def test_explicit_groq(self):
        with mock.patch.dict(os.environ, {"TRANSCRIBE_PROVIDER": "groq", "GROQ_API_KEY": ""}):
            self.assertEqual(transcribe_provider(), "groq")

    def test_key_implies_groq(self):
        with mock.patch.dict(os.environ, {"TRANSCRIBE_PROVIDER": "", "GROQ_API_KEY": "gsk_x"}):
            self.assertEqual(transcribe_provider(), "groq")

    def test_no_key_is_local(self):
        with mock.patch.dict(os.environ, {"TRANSCRIBE_PROVIDER": "", "GROQ_API_KEY": ""}):
            self.assertEqual(transcribe_provider(), "local")


class TranscribeLanguageTest(unittest.TestCase):
    def test_auto_is_none(self):
        with mock.patch.dict(os.environ, {"TRANSCRIBE_LANGUAGE": "auto"}):
            self.assertIsNone(transcribe_language(default="zh"))

    def test_empty_uses_default(self):
        with mock.patch.dict(os.environ, {"TRANSCRIBE_LANGUAGE": ""}):
            self.assertEqual(transcribe_language(default="zh"), "zh")
            self.assertIsNone(transcribe_language())

    def test_zh(self):
        with mock.patch.dict(os.environ, {"TRANSCRIBE_LANGUAGE": "zh"}):
            self.assertEqual(transcribe_language(), "zh")


class TranscriptFormatTest(unittest.TestCase):
    def test_format_segments(self):
        text = format_transcript_segments([(1.2, 3.8, "  hello ")])
        self.assertEqual(text, "[00:00:01 - 00:00:03] hello")

    def test_groq_payload_with_offset(self):
        triples = _groq_payload_segments(
            {"segments": [{"start": 1.2, "end": 3.9, "text": " 你好 "}]},
            60,
        )
        self.assertEqual(format_transcript_segments(triples), "[00:01:01 - 00:01:03] 你好")

    def test_groq_payload_text_fallback(self):
        triples = _groq_payload_segments({"text": "整段", "duration": 12}, 0)
        self.assertEqual(format_transcript_segments(triples), "[00:00:00 - 00:00:12] 整段")


class TranscribeFallbackTest(unittest.TestCase):
    def test_groq_failure_falls_back_local(self):
        job = Job(id="test", url="https://example.com")
        audio = Path("audio.mp3")
        with mock.patch.dict(os.environ, {"TRANSCRIBE_PROVIDER": "groq", "GROQ_API_KEY": "gsk_x"}):
            with mock.patch("app.transcribe_with_groq", side_effect=RuntimeError("boom")):
                with mock.patch("app.transcribe_with_faster_whisper", return_value="LOCAL") as local_fn:
                    with mock.patch("app.shutil.which", return_value=None):
                        text = transcribe_audio(audio, Path("."), job)
        self.assertEqual(text, "LOCAL")
        local_fn.assert_called_once()
        self.assertTrue(any("回退到本地 Whisper" in line for line in job.logs))

    def test_cancel_does_not_fallback(self):
        from app import JobCancelledError

        job = Job(id="test", url="https://example.com")
        with mock.patch.dict(os.environ, {"TRANSCRIBE_PROVIDER": "groq", "GROQ_API_KEY": "gsk_x"}):
            with mock.patch("app.transcribe_with_groq", side_effect=JobCancelledError("cancelled")):
                with mock.patch("app.transcribe_with_faster_whisper") as local_fn:
                    with self.assertRaises(JobCancelledError):
                        transcribe_audio(Path("audio.mp3"), Path("."), job)
        local_fn.assert_not_called()


class GroqRequestTest(unittest.TestCase):
    def test_posts_verbose_json(self):
        job = Job(id="test", url="https://example.com")
        payload = {
            "language": "zh",
            "duration": 2.0,
            "segments": [{"start": 0.0, "end": 2.0, "text": "你好世界"}],
            "text": "你好世界",
        }
        with tempfile.TemporaryDirectory() as tmp:
            audio = Path(tmp) / "clip.mp3"
            audio.write_bytes(b"ID3fake")
            fake_resp = mock.Mock(status_code=200)
            fake_resp.json.return_value = payload
            with mock.patch.dict(os.environ, {"GROQ_API_KEY": "gsk_x", "GROQ_WHISPER_MODEL": "whisper-large-v3-turbo", "TRANSCRIBE_LANGUAGE": "auto"}):
                with mock.patch("app.shutil.which", return_value=None):
                    with mock.patch("app.check_cancelled"):
                        with mock.patch("app.requests.post", return_value=fake_resp) as post:
                            from app import transcribe_with_groq

                            text = transcribe_with_groq(audio, Path(tmp), job)
        self.assertEqual(text, "[00:00:00 - 00:00:02] 你好世界")
        _, kwargs = post.call_args
        self.assertEqual(kwargs["data"]["model"], "whisper-large-v3-turbo")
        self.assertEqual(kwargs["data"]["response_format"], "verbose_json")
        self.assertNotIn("language", kwargs["data"])


if __name__ == "__main__":
    unittest.main()
