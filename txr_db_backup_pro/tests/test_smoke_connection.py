"""Smoke tests for rclone connection (skipped when rclone is not installed)."""
import os
import shutil
import unittest
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

CLOUD_MODULE = 'odoo.addons.txr_db_backup_pro.models.db_backup_cloud'


@tagged('txr_db_backup_pro')
class TestSmokeRcloneConnection(TransactionCase):

    @unittest.skipUnless(shutil.which('rclone'), 'rclone not installed')
    def test_rclone_binary_check(self):
        cloud = self.env['txr.db.backup.cloud'].create({
            'name': 'smoke-binary-test',
            'rclone_type': 'custom',
            'rclone_config_raw': '[smoke-binary-test]\ntype = memory\n',
        })
        cloud._check_rclone_binary()

    @unittest.skipUnless(shutil.which('rclone'), 'rclone not installed')
    def test_rclone_lsd_memory_backend(self):
        cloud = self.env['txr.db.backup.cloud'].create({
            'name': 'mem',
            'rclone_type': 'custom',
            'rclone_config_raw': '[mem]\ntype = memory\n',
        })
        result = cloud.action_test_connection()
        self.assertEqual(result['params']['type'], 'success')

    @unittest.skipUnless(shutil.which('rclone'), 'rclone not installed')
    def test_write_and_cleanup_tmp_config(self):
        cloud = self.env['txr.db.backup.cloud'].create({
            'name': 'smoke-cfg-test',
            'rclone_type': 'custom',
            'rclone_config_raw': '[smoke-cfg-test]\ntype = memory\n',
        })
        cfg_path = cloud._write_tmp_config()
        self.assertTrue(os.path.exists(cfg_path))
        # Config file should be readable and contain remote name
        with open(cfg_path) as f:
            content = f.read()
        self.assertIn('smoke-cfg-test', content)
        cloud._cleanup_tmp_config(cfg_path)
        self.assertFalse(os.path.exists(cfg_path))

    @unittest.skipUnless(shutil.which('rclone'), 'rclone not installed')
    def test_cleanup_tmp_config_idempotent(self):
        cloud = self.env['txr.db.backup.cloud'].create({
            'name': 'smoke-idempotent',
            'rclone_type': 'custom',
            'rclone_config_raw': '[smoke-idempotent]\ntype = memory\n',
        })
        cloud._cleanup_tmp_config('/tmp/nonexistent_txr_rclone_99999.conf')

    @unittest.skipIf(shutil.which('rclone'), 'rclone IS installed; testing missing-binary path')
    def test_check_rclone_binary_missing(self):
        cloud = self.env['txr.db.backup.cloud'].create({
            'name': 'smoke-missing-rclone',
            'rclone_type': 'custom',
            'rclone_config_raw': '[smoke-missing-rclone]\ntype = memory\n',
        })
        with patch(f'{CLOUD_MODULE}.shutil.which', return_value=None):
            with self.assertRaises(UserError):
                cloud._check_rclone_binary()
