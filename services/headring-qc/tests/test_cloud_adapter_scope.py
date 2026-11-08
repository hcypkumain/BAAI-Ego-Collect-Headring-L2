from pathlib import Path
import sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[3];sys.path.insert(0,str(ROOT/'services/headring-qc'))
from cloud_client import _ScopedBucket,_ScopedConnection,CloudError,expected_etag,ScopedUploader

class Fake:
    def __init__(self):self.calls=[];self.keys=[]
    def list(self,**kwargs):self.calls.append(('list',kwargs));return iter(self.keys)
    def new_key(self,key,**kwargs):self.calls.append(('put',key));return key
    def delete_key(self,key,**kwargs):self.calls.append(('delete',key))
    def get_bucket(self,name,**kwargs):self.calls.append(('bucket',name));return self

class Tests(unittest.TestCase):
    def test_object_receipt_is_written_atomically(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'proof.json';value={'key':'data/EGOL2/fixture/file','sha256':'a'*64}
            ScopedUploader._atomic_json(path,value)
            self.assertEqual(json.loads(path.read_text()),value)
            self.assertFalse(path.with_name(path.name+'.tmp').exists())
    def test_wrong_bucket_refused_before_sdk_call(self):
        fake=Fake();client=_ScopedConnection(fake,())
        with self.assertRaises(CloudError):client.get_bucket('another-bucket')
        self.assertFalse(fake.calls)
    def test_unscoped_list_refused_before_sdk_call(self):
        for prefix in [None,'','data/','data/EGOL2_extra/','data/OTHER/']:
            fake=Fake();bucket=_ScopedBucket(fake,())
            with self.assertRaises((CloudError,ValueError)):list(bucket.list(prefix=prefix))
            self.assertFalse(fake.calls)
    def test_out_of_scope_server_result_not_exposed_to_caller(self):
        fake=Fake();fake.keys=[type('Key',(),{'name':'data/OTHER/private'})()]
        with self.assertRaises(CloudError):list(_ScopedBucket(fake,()).list(prefix='data/EGOL2/'))
    def test_upload_cannot_escape_two_roots(self):
        fake=Fake();bucket=_ScopedBucket(fake,())
        with self.assertRaises(ValueError):bucket.new_key('data/OTHER/session/video')
        self.assertFalse(fake.calls)
    def test_deletion_is_one_time_snapshot_not_general_capability(self):
        fake=Fake();bucket=_ScopedBucket(fake,['data/EGOL2/old/file'])
        bucket.delete_key('data/EGOL2/old/file');self.assertEqual(fake.calls,[('delete','data/EGOL2/old/file')])
        for key in ['data/EGOL2/new/file','data/EGOL3_hand/old/file','data/OTHER/file']:
            with self.assertRaises((ValueError,CloudError)):bucket.delete_key(key)
        self.assertEqual(len(fake.calls),1)
    def test_single_and_multipart_integrity_expected_etag(self):
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'file';p.write_bytes(b'abc');self.assertEqual(expected_etag(p),hashlib.md5(b'abc').hexdigest())
            data=b'a'*(5*1024*1024)+b'last';p.write_bytes(data)
            expected=hashlib.md5(hashlib.md5(data[:5*1024*1024]).digest()+hashlib.md5(b'last').digest()).hexdigest()+'-2'
            self.assertEqual(expected_etag(p),expected)

if __name__=='__main__':unittest.main(verbosity=2)
