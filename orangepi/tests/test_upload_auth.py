"""Platform login must be followed by temporary object-storage credentials."""
from pathlib import Path
import sys,types,unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src/orangepi_collect/control'))
import cloud_worker
from cloud_common import CloudError

class UploadAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.client=Mock();self.client.sts_token={key:'example' for key in ('accessKeyId','secretAccessKey','securityToken')}
        self.client.get_eai_token.return_value=True;self.client.get_ks3_sts.return_value=True
        self.connection=Mock();self.vendor=types.ModuleType('robot_data_uploader.uploader');self.vendor.Connection=self.connection;self.vendor.RobotDataUploader=Mock(return_value=self.client)
        self.config=types.ModuleType('robot_data_uploader.config');self.config.ENDPOINT_BACKUP='storage.example.invalid'
        package=types.ModuleType('robot_data_uploader');package.uploader=self.vendor;package.config=self.config
        self.modules=patch.dict(sys.modules,{'robot_data_uploader':package,'robot_data_uploader.uploader':self.vendor,'robot_data_uploader.config':self.config});self.modules.start();self.addCleanup(self.modules.stop)
    def authenticate(self):return cloud_worker.authenticate({'ak':'example','sk':'example'})
    def test_platform_then_sts_authorization_and_tls_connection(self):
        self.assertIs(self.authenticate()[0],self.client)
        names=[call[0] for call in self.client.method_calls];self.assertLess(names.index('get_eai_token'),names.index('get_ks3_sts'))
        self.vendor.Connection(host=self.config.ENDPOINT_BACKUP)
        self.assertEqual(self.connection.call_args.kwargs['is_secure'],True);self.assertEqual(self.connection.call_args.kwargs['port'],443)
    def test_failed_platform_login_does_not_request_storage_credentials(self):
        self.client.get_eai_token.return_value=False
        with self.assertRaises(CloudError):self.authenticate()
        self.client.get_ks3_sts.assert_not_called()
    def test_failed_sts_request_is_a_reportable_failure(self):
        self.client.get_ks3_sts.return_value=False
        with self.assertRaisesRegex(CloudError,'临时存储授权获取失败'):self.authenticate()
    def test_missing_sts_field_does_not_return_an_unusable_client(self):
        self.client.sts_token.pop('securityToken')
        with self.assertRaisesRegex(CloudError,'授权不完整'):self.authenticate()

if __name__=='__main__':unittest.main()
