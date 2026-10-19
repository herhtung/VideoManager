import sys
import tempfile
import unittest
from pathlib import Path

from videomanager.compression.worker import _run_ffmpeg


class FfmpegProgressTests(unittest.TestCase):
    def test_unavailable_timestamps_do_not_fail_or_reset_progress(self):
        progress = []
        code = "print('out_time_us=N/A'); print('out_time_us=500000'); print('out_time_us=N/A')"
        with tempfile.TemporaryDirectory() as directory:
            _run_ffmpeg([sys.executable, "-c", code], 1, lambda: False, progress.append,
                        space_path=Path(directory), minimum_free=0)
        self.assertEqual(progress, [50.0, 100.0])
