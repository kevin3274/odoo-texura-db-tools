"""Tests for cloud storage backend (txr.db.backup.cloud)."""
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

CLOUD_MODULE = 'odoo.addons.txr_db_backup_pro.models.db_backup_cloud'


@tagged('txr_db_backup_pro')
class TestCloudStorage(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cloud_s3 = cls.env['txr.db.backup.cloud'].create({
            'name': 'test-s3',
            'rclone_type': 's3',
            's3_provider': 'AWS',
            's3_access_key': 'AKEY',
            's3_secret_key': 'SKEY',
            's3_bucket': 'mybucket',
            's3_region': 'us-east-1',
        })
        cls.cloud_gcs = cls.env['txr.db.backup.cloud'].create({
            'name': 'test-gcs',
            'rclone_type': 'gcs',
            'gcs_bucket': 'gbucket',
            'gcs_service_account_json': '{"type":"service_account"}',
        })
        cls.cloud_custom = cls.env['txr.db.backup.cloud'].create({
            'name': 'test-custom',
            'rclone_type': 'custom',
            'rclone_config_raw': '[customremote]\ntype = drive\n',
        })

    # ------------------------------------------------------------------ #
    #  _generate_rclone_config
    # ------------------------------------------------------------------ #

    def test_generate_rclone_config_s3(self):
        cfg = self.cloud_s3._generate_rclone_config()
        self.assertIn('[test-s3]', cfg)
        self.assertIn('type = s3', cfg)
        self.assertIn('access_key_id = AKEY', cfg)
        self.assertIn('region = us-east-1', cfg)

    def test_generate_rclone_config_s3_has_secret_key(self):
        cfg = self.cloud_s3._generate_rclone_config()
        self.assertIn('secret_access_key = SKEY', cfg)

    def test_generate_rclone_config_gcs(self):
        cfg = self.cloud_gcs._generate_rclone_config()
        self.assertIn('type = google cloud storage', cfg)
        self.assertIn('service_account_credentials =', cfg)
        self.assertIn('gbucket', cfg)

    def test_generate_rclone_config_custom_passthrough(self):
        cfg = self.cloud_custom._generate_rclone_config()
        self.assertEqual(cfg, '[customremote]\ntype = drive\n')

    def test_generate_rclone_config_s3_with_endpoint(self):
        remote = self.env['txr.db.backup.cloud'].create({
            'name': 'minio-test',
            'rclone_type': 's3',
            's3_provider': 'Minio',
            's3_access_key': 'KEY',
            's3_secret_key': 'SECRET',
            's3_bucket': 'mybucket',
            's3_endpoint': 'https://minio.example.com',
        })
        cfg = remote._generate_rclone_config()
        self.assertIn('endpoint = https://minio.example.com', cfg)

    def test_generate_rclone_config_azure(self):
        remote = self.env['txr.db.backup.cloud'].create({
            'name': 'azure-test',
            'rclone_type': 'azure',
            'azure_account': 'myaccount',
            'azure_key': 'mykey',
            'azure_container': 'mycontainer',
        })
        cfg = remote._generate_rclone_config()
        self.assertIn('type = azureblob', cfg)
        self.assertIn('account = myaccount', cfg)

    def test_generate_rclone_config_b2(self):
        remote = self.env['txr.db.backup.cloud'].create({
            'name': 'b2-test',
            'rclone_type': 'b2',
            'b2_account': 'b2account',
            'b2_key': 'b2key',
            'b2_bucket': 'b2bucket',
        })
        cfg = remote._generate_rclone_config()
        self.assertIn('type = b2', cfg)
        self.assertIn('account = b2account', cfg)

    # ------------------------------------------------------------------ #
    #  _check_rclone_binary
    # ------------------------------------------------------------------ #

    def test_rclone_binary_missing_raises(self):
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value=None):
            with self.assertRaises(UserError):
                self.cloud_s3._check_rclone_binary()

    def test_rclone_binary_found_no_error(self):
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value='/usr/bin/rclone'):
            self.cloud_s3._check_rclone_binary()

    # ------------------------------------------------------------------ #
    #  action_test_connection
    # ------------------------------------------------------------------ #

    def test_action_test_connection_success(self):
        mock_result = MagicMock(returncode=0, stdout='', stderr='')
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value='/usr/bin/rclone'), \
             patch(f'{CLOUD_MODULE}.subprocess.run', return_value=mock_result):
            result = self.cloud_s3.action_test_connection()
        self.assertEqual(result.get('type'), 'ir.actions.client')
        self.assertEqual(result['params']['type'], 'success')

    def test_action_test_connection_failure_raises(self):
        mock_result = MagicMock(returncode=1, stdout='', stderr='auth error')
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value='/usr/bin/rclone'), \
             patch(f'{CLOUD_MODULE}.subprocess.run', return_value=mock_result):
            with self.assertRaises(UserError):
                self.cloud_s3.action_test_connection()

    def test_action_test_connection_timeout_raises(self):
        from subprocess import TimeoutExpired
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value='/usr/bin/rclone'), \
             patch(f'{CLOUD_MODULE}.subprocess.run',
                   side_effect=TimeoutExpired(cmd='rclone', timeout=30)):
            with self.assertRaises(UserError):
                self.cloud_s3.action_test_connection()

    def test_action_test_connection_uses_ls_command(self):
        mock_result = MagicMock(returncode=0, stdout='', stderr='')
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value='/usr/bin/rclone'), \
             patch(f'{CLOUD_MODULE}.subprocess.run', return_value=mock_result) as mocked:
            self.cloud_s3.action_test_connection()
        args = mocked.call_args[0][0]
        self.assertIn('ls', args)
        self.assertNotIn('lsd', args)
        self.assertIn('rclone', args[0])

    # ------------------------------------------------------------------ #
    #  _download_from_remote
    # ------------------------------------------------------------------ #

    def test_download_from_remote_calls_rclone_copy(self):
        mock_result = MagicMock(returncode=0, stdout='', stderr='')
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value='/usr/bin/rclone'), \
             patch(f'{CLOUD_MODULE}.subprocess.run', return_value=mock_result) as mocked:
            path = self.cloud_s3._download_from_remote('backups/file.dump', '/tmp/x')
        args = mocked.call_args[0][0]
        self.assertEqual(args[0], 'rclone')
        self.assertIn('copy', args)
        self.assertEqual(path, '/tmp/x/file.dump')

    def test_download_from_remote_failure_raises(self):
        mock_result = MagicMock(returncode=1, stdout='', stderr='not found')
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value='/usr/bin/rclone'), \
             patch(f'{CLOUD_MODULE}.subprocess.run', return_value=mock_result):
            with self.assertRaises(UserError):
                self.cloud_s3._download_from_remote('backups/file.dump', '/tmp/x')

    def test_download_from_remote_returns_correct_local_path(self):
        mock_result = MagicMock(returncode=0, stdout='', stderr='')
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value='/usr/bin/rclone'), \
             patch(f'{CLOUD_MODULE}.subprocess.run', return_value=mock_result):
            path = self.cloud_s3._download_from_remote(
                'nested/path/backup_20240101.dump', '/tmp/restore_dir'
            )
        self.assertEqual(path, '/tmp/restore_dir/backup_20240101.dump')

    def test_download_from_remote_includes_remote_name_in_cmd(self):
        mock_result = MagicMock(returncode=0, stdout='', stderr='')
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value='/usr/bin/rclone'), \
             patch(f'{CLOUD_MODULE}.subprocess.run', return_value=mock_result) as mocked:
            self.cloud_s3._download_from_remote('backups/file.dump', '/tmp/x')
        args = mocked.call_args[0][0]
        self.assertTrue(any('test-s3:' in str(a) for a in args))
