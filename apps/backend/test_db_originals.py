import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from db_originals import copy_originals, verify_original_copy

class OriginalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.source = tempfile.TemporaryDirectory()
        self.destination = tempfile.TemporaryDirectory()
        self.addCleanup(self.source.cleanup)
        self.addCleanup(self.destination.cleanup)
        self.root, self.target = Path(self.source.name), Path(self.destination.name)
        rid = str(uuid.uuid4())
        raw = b'%PDF-synthetic'
        (self.root/'resumes').mkdir()
        (self.root/'resume_originals').mkdir()
        (self.root/'resume_originals'/f'{rid}.pdf').write_bytes(raw)
        (self.root/'resumes'/f'{rid}.json').write_text(json.dumps({'resume_id':rid,'user_id':str(uuid.uuid4()),'original':{'ext':'pdf','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}}), encoding='utf-8')

    def test_roundtrip_and_corrupted_bytes(self):
        result = copy_originals(self.root,self.target)
        manifest = result['manifest']
        self.assertEqual(verify_original_copy(self.target,manifest), {'verified_originals':1})
        (self.target/manifest[0]['path']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'integrity mismatch'):
            verify_original_copy(self.target,manifest)

    def test_interrupted_copy_remains_uncertified_and_no_overwrite(self):
        with patch('db_originals.shutil.copyfileobj',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                copy_originals(self.root,self.target)
        self.assertTrue((self.target/'.originals-copy-incomplete').exists())
        with self.assertRaisesRegex(ValueError,'incomplete'):
            verify_original_copy(self.target,[])
        with self.assertRaisesRegex(ValueError,'empty'):
            copy_originals(self.root,self.target)

    def test_extra_file_and_manifest_tampering_rejected(self):
        manifest = copy_originals(self.root,self.target)['manifest']
        extra = self.target/'extra.txt'
        extra.write_text('untracked')
        with self.assertRaisesRegex(ValueError,'Unexpected'):
            verify_original_copy(self.target,manifest)
        with self.assertRaisesRegex(ValueError,'differs'):
            verify_original_copy(self.target,[])

if __name__ == '__main__':
    unittest.main()
