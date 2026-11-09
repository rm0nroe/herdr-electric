import hashlib
import io
import json
from pathlib import Path
import runpy
import tarfile
import tempfile
import unittest
from unittest.mock import patch


class ReleasedCodex(unittest.TestCase):
    def test_reuse_requires_the_pinned_archive_and_identical_codex_sources(self):
        build = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/build-electric.py'))
        reuse = build['released_codex']
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            archive = work / 'previous.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                prior = {'sources': {'codex': {'revision': 'old'}}}
                payload = json.dumps(prior).encode()
                info = tarfile.TarInfo(build['PREVIOUS_BUNDLE'] + '/manifest.json')
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            with self.assertRaisesRegex(ValueError, 'unexpected previous Electric archive'):
                reuse(work, archive)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            with patch.dict(reuse.__globals__, PREVIOUS_SHA=digest):
                with self.assertRaisesRegex(ValueError, 'Codex sources changed'):
                    reuse(work, archive)
            self.assertFalse((work / 'codex-electric').exists())
