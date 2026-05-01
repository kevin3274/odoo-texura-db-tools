"""Tests for backup retention policies (_apply_retention)."""
import os
import shutil
from datetime import timedelta

from odoo import fields
from odoo.tests.common import TransactionCase

_TEST_DIR = '/tmp/test_retention'


class TestBackupRetention(TransactionCase):
    """Test count-based and days-based retention cleanup."""

    def setUp(self):
        super().setUp()
        os.makedirs(_TEST_DIR, exist_ok=True)
        self.backup = self.env['txr.db.backup'].create({
            'name': 'Retention Test Backup',
            'storage_type': 'local',
            'local_path': _TEST_DIR,
            'retention_count': 0,
            'retention_days': 0,
        })

    def tearDown(self):
        shutil.rmtree(_TEST_DIR, ignore_errors=True)
        super().tearDown()

    def _create_success_logs(self, count):
        """批量创建 success 状态 log。"""
        now = fields.Datetime.now()
        logs = []
        for i in range(count):
            log = self.env['txr.db.backup.log'].create({
                'backup_id': self.backup.id,
                'state': 'success',
                'phase': 'notify',
                'started_at': now - timedelta(hours=count - i),
                'storage_type': 'local',
            })
            logs.append(log)
        return logs

    def _count_success_logs(self):
        return self.env['txr.db.backup.log'].search_count([
            ('backup_id', '=', self.backup.id),
            ('state', '=', 'success'),
        ])

    def test_retention_count(self):
        """创建 8 条 success log，retention_count=7，调用后剩 7 条。"""
        self._create_success_logs(8)
        self.backup.write({'retention_count': 7})
        self.backup._apply_retention()
        self.assertEqual(self._count_success_logs(), 7)

    def test_retention_days(self):
        """31 天前的 log，retention_days=30 时被删除。"""
        now = fields.Datetime.now()
        old_log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'started_at': now - timedelta(days=31),
            'storage_type': 'local',
        })
        # 新 log 不应被删
        new_log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'started_at': now - timedelta(days=1),
            'storage_type': 'local',
        })
        self.backup.write({'retention_days': 30})
        self.backup._apply_retention()

        self.assertFalse(old_log.exists(), '31 天前的 log 应被删除')
        self.assertTrue(new_log.exists(), '1 天前的 log 应保留')

    def test_retention_both_policies(self):
        """同时配置两种策略，取更严格（count=3 且 days=30）。"""
        now = fields.Datetime.now()
        # 3 条 1 天前的 log
        for i in range(3):
            self.env['txr.db.backup.log'].create({
                'backup_id': self.backup.id,
                'state': 'success',
                'started_at': now - timedelta(days=1),
                'storage_type': 'local',
            })
        # 2 条 31 天前的 log（超出 days 策略）
        for i in range(2):
            self.env['txr.db.backup.log'].create({
                'backup_id': self.backup.id,
                'state': 'success',
                'started_at': now - timedelta(days=31),
                'storage_type': 'local',
            })
        self.backup.write({'retention_count': 4, 'retention_days': 30})
        self.backup._apply_retention()
        # days 策略删掉 31 天前的 2 条，剩余 3 条（在 count=4 内）
        self.assertEqual(self._count_success_logs(), 3)

    def test_retention_zero_no_delete(self):
        """retention_count=0, retention_days=0 时，不删除任何记录。"""
        self._create_success_logs(5)
        self.backup.write({'retention_count': 0, 'retention_days': 0})
        self.backup._apply_retention()
        self.assertEqual(self._count_success_logs(), 5)

    def test_retention_only_success(self):
        """failed/warning 状态的 log 不被清理。"""
        now = fields.Datetime.now()
        failed_log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'failed',
            'started_at': now - timedelta(days=60),
            'storage_type': 'local',
        })
        warning_log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'warning',
            'started_at': now - timedelta(days=60),
            'storage_type': 'local',
        })
        # 超过 count 的 success log
        self._create_success_logs(5)
        self.backup.write({'retention_count': 3, 'retention_days': 7})
        self.backup._apply_retention()

        self.assertTrue(failed_log.exists(), 'failed log 不应被清理')
        self.assertTrue(warning_log.exists(), 'warning log 不应被清理')
