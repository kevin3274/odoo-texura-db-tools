"""Tests for SFTP storage backend (_upload_sftp / _check_sftp_binary)."""
import os
import tempfile
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


MODULE_PATH = 'odoo.addons.txr_db_backup_lite.models.db_backup'


class TestSftpStorage(TransactionCase):
    """Test SFTP upload logic and error handling."""

    def setUp(self):
        super().setUp()
        self.backup = self.env['txr.db.backup'].create({
            'name': 'SFTP Test Backup',
            'storage_type': 'sftp',
            'sftp_host': 'backup.example.com',
            'sftp_port': 22,
            'sftp_username': 'backupuser',
            'sftp_remote_path': '/remote/backups',
        })
        self.log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'running',
            'phase': 'upload',
            'storage_type': 'sftp',
        })
        # 创建临时文件模拟备份文件
        fd, self.fake_file = tempfile.mkstemp(suffix='.dump.gz')
        os.write(fd, b'fake backup content')
        os.close(fd)

    def tearDown(self):
        if os.path.exists(self.fake_file):
            os.unlink(self.fake_file)
        super().tearDown()

    def test_upload_sftp_with_key_path(self):
        """使用 sftp_key_path 时，命令中包含 -i key_path。"""
        self.backup.write({'sftp_key_path': '/home/user/.ssh/id_rsa'})
        with patch(f'{MODULE_PATH}.subprocess') as mock_sub, \
             patch(f'{MODULE_PATH}.shutil') as mock_shutil:
            mock_shutil.which.return_value = '/usr/bin/sftp'
            mock_sub.run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            self.backup._upload_sftp(self.log, self.fake_file)

        call_args = mock_sub.run.call_args[0][0]
        self.assertIn('-i', call_args)
        key_idx = call_args.index('-i')
        self.assertEqual(call_args[key_idx + 1], '/home/user/.ssh/id_rsa')

    def test_upload_sftp_with_key_content(self):
        """使用 sftp_private_key 时，创建临时文件，chmod 0600，最终删除。"""
        self.backup.write({'sftp_private_key': '-----BEGIN OPENSSH PRIVATE KEY-----\nfake_key\n-----END OPENSSH PRIVATE KEY-----'})
        fake_key_fd = 10
        fake_key_path = '/tmp/txr_sftp_key_fake'

        with patch(f'{MODULE_PATH}.subprocess') as mock_sub, \
             patch(f'{MODULE_PATH}.shutil') as mock_shutil, \
             patch(f'{MODULE_PATH}.tempfile') as mock_tmp, \
             patch(f'{MODULE_PATH}.os') as mock_os:
            mock_shutil.which.return_value = '/usr/bin/sftp'
            mock_tmp.mkstemp.return_value = (fake_key_fd, fake_key_path)
            mock_os.write = MagicMock()
            mock_os.close = MagicMock()
            mock_os.chmod = MagicMock()
            mock_os.remove = MagicMock()
            mock_os.path.join.side_effect = os.path.join
            mock_os.path.basename.side_effect = os.path.basename
            mock_os.path.exists.return_value = True
            mock_sub.run.return_value = MagicMock(returncode=0, stdout='', stderr='')

            self.backup._upload_sftp(self.log, self.fake_file)

        mock_os.chmod.assert_called_once_with(fake_key_path, 0o600)
        mock_os.remove.assert_called_once_with(fake_key_path)

    def test_upload_sftp_failure(self):
        """SFTP returncode=1 时，抛出 UserError。"""
        self.backup.write({'sftp_key_path': '/home/user/.ssh/id_rsa'})
        with patch(f'{MODULE_PATH}.subprocess') as mock_sub, \
             patch(f'{MODULE_PATH}.shutil') as mock_shutil:
            mock_shutil.which.return_value = '/usr/bin/sftp'
            mock_sub.run.return_value = MagicMock(
                returncode=1, stdout='', stderr='Connection refused'
            )
            with self.assertRaises(UserError):
                self.backup._upload_sftp(self.log, self.fake_file)

    def test_check_sftp_binary_missing(self):
        """shutil.which 返回 None 时，_check_sftp_binary 抛出 UserError。"""
        with patch(f'{MODULE_PATH}.shutil') as mock_shutil:
            mock_shutil.which.return_value = None
            with self.assertRaises(UserError):
                self.backup._check_sftp_binary()

    def test_upload_sftp_no_key_uses_system_default(self):
        """两个 key 字段都为空时，使用系统默认密钥（命令不含 -i）。"""
        self.backup.write({'sftp_key_path': False, 'sftp_private_key': False})
        with patch(f'{MODULE_PATH}.subprocess') as mock_sub, \
             patch(f'{MODULE_PATH}.shutil') as mock_shutil:
            mock_shutil.which.return_value = '/usr/bin/sftp'
            mock_sub.run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            self.backup._upload_sftp(self.log, self.fake_file)
        cmd = mock_sub.run.call_args[0][0]
        self.assertNotIn('-i', cmd, 'sftp command should not contain -i when no key configured')

    # -------------------------------------------------------------------------
    # Test Connection (D11)
    # -------------------------------------------------------------------------

    def test_action_test_sftp_connection_success(self):
        """Test Connection 成功时返回通知 action。"""
        self.backup.write({'sftp_key_path': '/home/user/.ssh/id_rsa'})
        with patch(f'{MODULE_PATH}.subprocess') as mock_sub, \
             patch(f'{MODULE_PATH}.shutil') as mock_shutil:
            mock_shutil.which.return_value = '/usr/bin/sftp'
            mock_sub.run.return_value = MagicMock(returncode=0, stdout='file1\nfile2', stderr='')
            result = self.backup.action_test_sftp_connection()
        self.assertEqual(result['type'], 'ir.actions.client')
        self.assertEqual(result['tag'], 'display_notification')
        self.assertEqual(result['params']['type'], 'success')

    def test_action_test_sftp_connection_failure(self):
        """Test Connection 失败时抛出 UserError。"""
        self.backup.write({'sftp_key_path': '/home/user/.ssh/id_rsa'})
        with patch(f'{MODULE_PATH}.subprocess') as mock_sub, \
             patch(f'{MODULE_PATH}.shutil') as mock_shutil:
            mock_shutil.which.return_value = '/usr/bin/sftp'
            mock_sub.run.return_value = MagicMock(
                returncode=1, stdout='', stderr='Connection refused'
            )
            with self.assertRaises(UserError):
                self.backup.action_test_sftp_connection()
