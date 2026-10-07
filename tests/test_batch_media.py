import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'cinema/scripts'))
import batch_media as batch
import batch_cloud as nas


class BatchTests(unittest.TestCase):
    def test_source_cleanup_requires_audit_and_matching_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();source=root/'input.mp4';source.write_bytes(b'original')
            uid='123456789abc';private=root/'jobs'/uid/'private';private.mkdir(parents=True)
            (private/'key').write_bytes(b'k'*16)
            item={'uid':uid,'source':str(source),'size':source.stat().st_size,'mtime_ns':source.stat().st_mtime_ns}
            proof={'uid':uid,'status':'verified','key_sha256':hashlib.sha256(b'k'*16).hexdigest()}
            with patch.object(batch,'WORK',root):
                with self.assertRaises(ValueError):batch.delete_verified_source(item,{**proof,'status':'failed'},root)
                with self.assertRaises(ValueError):batch.delete_verified_source(item,{**proof,'key_sha256':'wrong'},root)
                self.assertTrue(source.exists())
                batch.delete_verified_source(item,proof,root)
                self.assertFalse(source.exists())
                self.assertEqual((private/'key').read_bytes(),b'k'*16)

    def test_cloud_upload_submits_only_one_pack_at_a_time(self):
        class FakeCloud:
            def __init__(self):self.files=[];self.tasks=[]
            def listing(self,path):return self.files
            def api(self,path,data=None):
                if path.endswith('/undone'):return [t for t in self.tasks if t['state']==1]
                if path.endswith('/done'):return [t for t in self.tasks if t['state']==2]
                self.assert_copy=data['names']
                task={'id':str(len(self.tasks)),'state':1,'progress':0,'name':data['names'][0]}
                self.tasks.append(task)
                return {'tasks':[task]}
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'sequential-upload.json').write_text(json.dumps({'current':None,'attempts':{}}))
            api=FakeCloud();report={'packs':[{'file':'pack_000.ts','bytes':10},{'file':'pack_001.ts','bytes':20}]}
            with patch.object(nas,'get_job',return_value=(base,base,{'folder':'TEST_001'},report)):
                self.assertEqual(nas.status('123456789abc',api)['status'],'uploading')
                self.assertEqual(len(api.tasks),1)
                nas.status('123456789abc',api)
                self.assertEqual(len(api.tasks),1)
                api.tasks[0].update(state=2,progress=100);api.files.append({'name':'pack_000.ts','size':10})
                nas.status('123456789abc',api)
                self.assertEqual(len(api.tasks),2)
                api.tasks[1].update(state=2,progress=100);api.files.append({'name':'pack_001.ts','size':20})
                self.assertEqual(nas.status('123456789abc',api)['status'],'uploaded')

    def test_hevc_hardware_transcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'input.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=24',
                            '-t','7','-c:v','libx265','-x265-params','log-level=error:pools=1:frame-threads=1',
                            '-pix_fmt','yuv420p',str(source)],check=True,capture_output=True)
            probe=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(source)]))
            item={'uid':'abcdef123456','source':str(source),'size':source.stat().st_size,
                  'mtime_ns':source.stat().st_mtime_ns,'probe':probe}
            with patch.object(batch,'WORK',root):
                report=batch.package(item)
            self.assertEqual(report['video_mode'],batch.VIDEO_ENCODER)
            self.assertTrue(report['local_decryption_verified'])
            self.assertAlmostEqual(report['duration'],7,delta=.1)

    def test_hls_encryption_roundtrip_and_resume_preserve_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'input.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24',
                            '-f', 'lavfi', '-i', 'sine=frequency=400', '-t', '13', '-c:v', 'libx264', '-g', '48',
                            '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], check=True)
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)]))
            item = {'uid': '123456789abc', 'source': str(source), 'size': source.stat().st_size,
                    'mtime_ns': source.stat().st_mtime_ns, 'probe': probe}
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            with patch.object(batch, 'WORK', root):
                report = batch.package(item)
                self.assertTrue(report['local_decryption_verified'])
                self.assertEqual(report['video_mode'], 'copy')
                self.assertAlmostEqual(report['duration'], 13, delta=.1)
                private = root / 'jobs' / item['uid'] / 'private'
                key = (private / 'key').read_bytes()
                self.assertEqual(len(key), 16)
                rows = json.loads((private / 'parts.json').read_text())
                self.assertTrue(all(r['length'] % 16 == 0 for r in rows))
                for pack in report['packs']:
                    self.assertEqual(sum(r['length'] for r in rows if r['pack'] == pack['file']), pack['bytes'])
                second = batch.package(item)
                self.assertEqual(second, report)
                self.assertEqual((private / 'key').read_bytes(), key)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), original)

    def test_source_change_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'original.mp4';source.write_bytes(b'untouched')
            with self.assertRaises(ValueError):
                batch.package({'source':str(source),'size':0,'mtime_ns':0,'uid':'123456789abc'})
            self.assertEqual(source.read_bytes(),b'untouched')

    def test_existing_playback_directory_cannot_be_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);media=root/'media';(media/'TEST_001').mkdir(parents=True)
            item={'uid':'123456789abc','folder':'TEST_001','size':1,'mtime_ns':1}
            with patch.object(nas,'MEDIA',media),patch.object(nas,'STATE',root/'state'),patch.object(nas,'PACKS',root/'packs'):
                with self.assertRaises(ValueError):nas.initialize(item['uid'],item,None)
            self.assertFalse((root/'state').exists())

    def test_cleanup_requires_verified_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);packs=base/'packs';packs.mkdir();(packs/'pack_000.ts').write_bytes(b'keep')
            with patch.object(nas,'get_job',return_value=(base,packs,{}, {'packs':[{'file':'pack_000.ts'}]})):
                with self.assertRaises(ValueError):nas.cleanup('123456789abc')
            self.assertEqual((packs/'pack_000.ts').read_bytes(),b'keep')


if __name__ == '__main__':
    unittest.main()
