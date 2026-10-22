import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

from videomanager.compression.capability_probe import probe_capabilities
from videomanager.compression.profiles import Profile
from videomanager.compression.safe_replace import _restore_extra_streams, _validate_chapters
from videomanager.compression.worker import CompressionError, _probe


class ChapterRepairTests(unittest.TestCase):
    def test_chapters_rebuilt_without_changing_video_audio_or_original(self):
        cap = probe_capabilities(test_nvenc=False)
        if "libx264" not in cap.encoders:
            self.skipTest("Test FFmpeg lacks libx264")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain, source, output = (root / name for name in ("plain.mp4", "source.mp4", "copy.mp4"))
            metadata = root / "chapters.ffmeta"
            metadata.write_text(';FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=\n'
                                '[CHAPTER]\nTIMEBASE=1/1000\nSTART=1500\nEND=3000\ntitle=Chapter \\= two\n', encoding="utf8")

            def run(args):
                result = subprocess.run([cap.ffmpeg, "-hide_banner", "-loglevel", "error", *args],
                                        capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr[-800:])
                return result.stdout

            run(["-f", "lavfi", "-i", "testsrc2=size=128x72:rate=24", "-f", "lavfi", "-i", "sine=frequency=440",
                 "-t", "3", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(plain)])
            run(["-i", str(plain), "-f", "ffmetadata", "-i", str(metadata), "-map", "0", "-map_chapters", "1",
                 "-c", "copy", str(source)])
            output.write_bytes(plain.read_bytes())
            original_hash = hashlib.sha256(source.read_bytes()).digest()
            before = run(["-i", str(output), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-f", "streamhash", "-"])
            original_info = _probe(cap.ffprobe, source)
            result = _restore_extra_streams(cap, root, source, output, original_info, Profile(encoder="libx264"))
            after = run(["-i", str(output), "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-f", "streamhash", "-"])
            self.assertEqual(before, after)
            self.assertEqual(original_hash, hashlib.sha256(source.read_bytes()).digest())
            self.assertEqual(len(result["chapters"]), 2)
            self.assertEqual(result["chapters"][1]["tags"]["title"], "Chapter = two")
            self.assertFalse(list(root.glob(".*.streams.*")))

    def test_changed_or_missing_chapters_are_rejected(self):
        original = [{"start_time": "0", "end_time": "3", "tags": {"title": "Original"}}]
        with self.assertRaises(CompressionError):
            _validate_chapters(original, [])
        with self.assertRaises(CompressionError):
            _validate_chapters(original, [{"start_time": "0", "end_time": "3", "tags": {"title": "Changed"}}])

    def test_only_final_end_may_follow_verified_media_duration(self):
        original = [{"start_time": "0", "end_time": "3", "tags": {"title": ""}}]
        clipped = [{"start_time": "0", "end_time": "2.945", "tags": {"title": ""}}]
        _validate_chapters(original, clipped, source_duration_ms=3000, output_duration_ms=2945)
        with self.assertRaises(CompressionError):
            _validate_chapters(original, clipped, source_duration_ms=3000, output_duration_ms=2990)
        with self.assertRaises(CompressionError):
            _validate_chapters(original * 2, clipped * 2, source_duration_ms=3000, output_duration_ms=2945)

    def test_full_final_chapter_may_extend_to_verified_copy_duration(self):
        original = [{"start_time": "0", "end_time": "5736.931", "tags": {"title": ""}}]
        extended = [{"start_time": "0", "end_time": "5737.189546", "tags": {"title": ""}}]
        _validate_chapters(original, extended, source_duration_ms=5736931, output_duration_ms=5737190)
        _validate_chapters(original, original, source_duration_ms=5736931, output_duration_ms=5737190)
        with self.assertRaises(CompressionError):
            _validate_chapters(original, extended, source_duration_ms=5736931, output_duration_ms=5737090)
        with self.assertRaises(CompressionError):
            _validate_chapters(original, extended, source_duration_ms=5737031, output_duration_ms=5737190)
        with self.assertRaises(CompressionError):
            _validate_chapters(original * 2, extended * 2, source_duration_ms=5736931, output_duration_ms=5737190)
