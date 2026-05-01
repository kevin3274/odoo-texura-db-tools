"""Tests for backup execution flow (_run method)."""
import hashlib
import os
import shutil
import tempfile
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase


MODULE_PATH = 'odoo.addons.txr_db_backup_lite.models.db_backup'

_TEST_DIR = '/tmp/test_backup_exec'


class TestBackupExecution(TransactionCase):
    """Test the _run() execution state machine and helpers."""

    def setUp(self):
        super().setUp()
        os.makedirs(_TEST_DIR, exist_ok=True)
        self.backup = self.env['txr.db.backup'].create({
            'name': 'Test Backup Exec',
            'storage_type': 'local',
            'local_path': _TEST_DIR,
        })

    def tearDown(self):
        shutil.rmtree(_TEST_DIR, ignore_errors=True)
        super().tearDown()

    def _get_logs(self):
        return self.env['txr.db.backup.log'].search(
            [('backup_id', '=', self.backup.id)]
        )

    def test_run_creates_log_with_running_state(self):
        """_run() 应创建 state='running' 的 log 记录。"""
        with patch('odoo.service.db.dump_db') as mock_dump_db, \
             patch(f'{MODULE_PATH}.shutil') as mock_shutil, \
             patch(f'{MODULE_PATH}.os') as mock_os, \
             patch(f'{MODULE_PATH}.tempfile') as mock_tmp:
            # 让 _run 在 dump 阶段抛出异常，检查 log 是否已创建
            mock_tmp.mkdtemp.return_value = '/tmp/fake_txr'
            mock_os.path.join.side_effect = os.path.join
            mock_os.path.getsize.return_value = 1024
            mock_os.path.basename.side_effect = os.path.basename
            mock_os.path.exists.return_value = True
            mock_dump_db.side_effect = Exception('Simulated dump failure')
            mock_shutil.rmtree = MagicMock()

            self.backup._run()

            logs = self._get_logs()
            self.assertTrue(logs, '应创建至少一条 log 记录')

    def test_run_success_state_machine(self):
        """mock 成功流程，验证最终 state='success'（zip 格式，不调用 compress）。"""
        BackupClass = type(self.backup)
        with patch.object(BackupClass, '_do_dump') as mock_dump, \
             patch.object(BackupClass, '_compute_sha256') as mock_sha, \
             patch.object(BackupClass, '_upload_local'), \
             patch.object(BackupClass, '_verify_level1'), \
             patch.object(BackupClass, '_verify_level2'), \
             patch.object(BackupClass, '_notify'), \
             patch.object(BackupClass, '_apply_retention'), \
             patch(f'{MODULE_PATH}.tempfile') as mock_tmp, \
             patch(f'{MODULE_PATH}.shutil') as mock_shutil, \
             patch(f'{MODULE_PATH}.os') as mock_os:
            # zip 格式：_do_dump 返回 .zip 文件，不调用 compress
            fake_path = '/tmp/fake_txr/db_20240101_120000.zip'
            mock_tmp.mkdtemp.return_value = '/tmp/fake_txr'
            mock_dump.return_value = fake_path
            mock_sha.return_value = 'abc123'
            mock_os.path.getsize.return_value = 2048
            mock_shutil.rmtree = MagicMock()

            self.backup._run()

            log = self._get_logs()
            self.assertTrue(log, '应存在 log 记录')
            self.assertEqual(log[0].state, 'success')

    def test_run_dump_failure_sets_failed(self):
        """mock dump_db 抛出异常，验证 state='failed', phase='dump'。"""
        with patch('odoo.service.db.dump_db') as mock_dump_db, \
             patch(f'{MODULE_PATH}.tempfile') as mock_tmp, \
             patch(f'{MODULE_PATH}.shutil') as mock_shutil, \
             patch(f'{MODULE_PATH}.os') as mock_os:
            mock_tmp.mkdtemp.return_value = '/tmp/fake_txr'
            mock_os.path.join.side_effect = os.path.join
            mock_os.path.basename.side_effect = os.path.basename
            mock_os.makedirs = MagicMock()

            # dump_db 抛出异常模拟备份失败
            mock_dump_db.side_effect = Exception('database not found')
            mock_shutil.rmtree = MagicMock()

            self.backup._run()

            log = self._get_logs()
            self.assertTrue(log)
            self.assertEqual(log[0].state, 'failed')
            self.assertIn('dump', log[0].phase or '')

    def test_run_concurrent_protection(self):
        """先创建 running log，再调用 _run()，不应创建新 log。"""
        existing_log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'running',
            'phase': 'dump',
        })
        self.backup._run()
        logs = self._get_logs()
        # 只应存在原来那一条 running log
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].id, existing_log.id)

    def test_run_cleans_temp_files(self):
        """验证 finally 块清理临时目录。"""
        BackupClass = type(self.backup)
        with patch.object(BackupClass, '_do_dump') as mock_dump, \
             patch(f'{MODULE_PATH}.tempfile') as mock_tmp, \
             patch(f'{MODULE_PATH}.shutil') as mock_shutil, \
             patch(f'{MODULE_PATH}.os'):
            fake_tmp = '/tmp/fake_txr_cleanup'
            mock_tmp.mkdtemp.return_value = fake_tmp
            mock_dump.side_effect = Exception('Simulated dump error')
            mock_shutil.rmtree = MagicMock()

            self.backup._run()

            mock_shutil.rmtree.assert_called_once_with(fake_tmp, ignore_errors=True)

    def test_detect_pg_dump_version(self):
        """mock subprocess 返回版本字符串，验证解析结果。"""
        expected = 'pg_dump (PostgreSQL) 16.2'
        with patch(f'{MODULE_PATH}.subprocess') as mock_sub:
            mock_sub.run.return_value = MagicMock(
                returncode=0,
                stdout=expected,
                stderr='',
            )
            result = self.backup._detect_pg_dump_version()
        self.assertEqual(result, expected)

    def test_compute_sha256(self):
        """创建临时文件写入已知内容，验证 SHA256 计算正确。"""
        content = b'hello txr backup'
        expected = hashlib.sha256(content).hexdigest()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            result = self.backup._compute_sha256(tmp_path)
            self.assertEqual(result, expected)
        finally:
            os.unlink(tmp_path)
