"""Tests for TxrDbBackup cron synchronization logic."""
import os
import shutil
from unittest.mock import patch

from odoo.tests.common import TransactionCase

_TEST_DIR = '/tmp/test_backup'


class TestDbBackupModel(TransactionCase):
    """Test cron sync behavior on CRUD operations."""

    def setUp(self):
        super().setUp()
        os.makedirs(_TEST_DIR, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(_TEST_DIR, ignore_errors=True)
        super().tearDown()

    def _create_backup(self, **kwargs):
        vals = {
            'name': 'Test Backup',
            'storage_type': 'local',
            'local_path': '/tmp/test_backup',
        }
        vals.update(kwargs)
        return self.env['txr.db.backup'].create(vals)

    def test_create_backup_creates_cron(self):
        """创建 backup 配置后，cron_id 非空，cron 参数与配置一致。"""
        backup = self._create_backup(interval_number=2, interval_type='hours')
        self.assertTrue(backup.cron_id, 'cron_id 应在创建后自动设置')
        cron = backup.cron_id
        self.assertEqual(cron.interval_number, 2)
        self.assertEqual(cron.interval_type, 'hours')
        self.assertTrue(cron.active)

    def test_write_schedule_updates_cron(self):
        """修改 interval_number/type 后 cron 同步更新。"""
        backup = self._create_backup(interval_number=1, interval_type='days')
        backup.write({'interval_number': 3, 'interval_type': 'weeks'})
        cron = backup.cron_id
        self.assertEqual(cron.interval_number, 3)
        self.assertEqual(cron.interval_type, 'weeks')

    def test_write_active_false_disables_cron(self):
        """active=False 时 cron.active 也为 False。"""
        backup = self._create_backup()
        self.assertTrue(backup.cron_id.active)
        backup.write({'active': False})
        self.assertFalse(backup.cron_id.active)

    def test_unlink_removes_cron(self):
        """删除配置后关联 cron 也被删除。"""
        backup = self._create_backup()
        cron_id = backup.cron_id.id
        backup.unlink()
        cron = self.env['ir.cron'].browse(cron_id)
        self.assertFalse(cron.exists(), '关联 cron 应随 backup 一起删除')

    def test_create_sets_default_database(self):
        """database_name 默认为当前数据库名。"""
        backup = self._create_backup()
        self.assertEqual(backup.database_name, self.env.cr.dbname)

    # -------------------------------------------------------------------------
    # Task 13.5: execution_time 控制 cron.nextcall
    # -------------------------------------------------------------------------

    def test_execution_time_updates_nextcall(self):
        """execution_time 变更后 cron.nextcall 正确更新。"""
        backup = self._create_backup(execution_time=3.0, tz='UTC')
        cron = backup.cron_id
        # nextcall 的时间部分应为 03:00 UTC
        self.assertEqual(cron.nextcall.hour, 3)
        # 修改为 05:00
        backup.write({'execution_time': 5.0})
        self.assertEqual(backup.cron_id.nextcall.hour, 5)

    def test_execution_time_hours_interval(self):
        """hours 间隔时，cron 使用 interval_number 小时间隔而非 1 天。"""
        backup = self._create_backup(
            execution_time=2.0, tz='UTC',
            interval_number=4, interval_type='hours',
        )
        cron = backup.cron_id
        # 核心断言：cron 的间隔参数应按小时配置，而非天
        self.assertEqual(cron.interval_number, 4)
        self.assertEqual(cron.interval_type, 'hours')

    # -------------------------------------------------------------------------
    # Task 15.2: Run Now 不影响 cron.nextcall
    # -------------------------------------------------------------------------

    def test_run_now_does_not_affect_nextcall(self):
        """Run Now 不影响 cron.nextcall。"""
        backup = self._create_backup(execution_time=3.0, tz='UTC')
        original_nextcall = backup.cron_id.nextcall
        # mock _do_dump 避免实际执行 pg_dump
        with patch.object(type(backup), '_do_dump', side_effect=Exception('test skip')):
            try:
                backup._run()
            except Exception:
                pass
        # nextcall 不变
        self.assertEqual(backup.cron_id.nextcall, original_nextcall)

    # -------------------------------------------------------------------------
    # Task 16.4: 无效路径保存时抛出 ValidationError
    # -------------------------------------------------------------------------

    def test_invalid_local_path_raises_validation_error(self):
        """无效路径保存时抛出 ValidationError。"""
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.env['txr.db.backup'].create({
                'name': 'Bad Path Test',
                'storage_type': 'local',
                'local_path': '/nonexistent/impossible/path/that/cant/be/created',
            })
