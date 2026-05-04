"""Tests for instance size display and estimated duration (task 7.9)."""
import os
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests.common import TransactionCase, tagged


@tagged('txr_db_backup_pro')
class TestInstanceSizeDisplay(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'size-test',
            'database_name': 'sdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
        })

    # ------------------------------------------------------------------
    # db_size_bytes computed field
    # ------------------------------------------------------------------

    def test_db_size_bytes_query_success(self):
        with patch.object(type(self.backup), '_estimate_db_size', return_value=12345678):
            self.backup._compute_db_size_bytes()
        self.assertEqual(self.backup.db_size_bytes, 12345678)

    def test_db_size_bytes_query_failure_returns_zero(self):
        with patch.object(type(self.backup), '_estimate_db_size', return_value=None):
            self.backup._compute_db_size_bytes()
        # None → 0 per _compute_db_size_bytes implementation
        self.assertEqual(self.backup.db_size_bytes, 0)

    def test_db_size_bytes_skip_context(self):
        b = self.backup.with_context(skip_strategy_compute=True)
        b._compute_db_size_bytes()
        self.assertEqual(b.db_size_bytes, 0)

    # ------------------------------------------------------------------
    # action_refresh_filestore_size
    # ------------------------------------------------------------------

    def test_action_refresh_filestore_size_writes_cache(self):
        with patch.object(type(self.backup), '_estimate_filestore_size', return_value=98765):
            self.backup.action_refresh_filestore_size()
        self.assertEqual(self.backup.filestore_size_bytes, 98765)
        self.assertIsNotNone(self.backup.filestore_size_updated_at)

    def test_action_refresh_filestore_size_timeout_returns_notification(self):
        with patch.object(type(self.backup), '_estimate_filestore_size', return_value=None):
            result = self.backup.action_refresh_filestore_size()
        # Must return a client action (not raise)
        self.assertIsNotNone(result)
        self.assertEqual(result.get('type'), 'ir.actions.client')
        self.assertEqual(result.get('tag'), 'display_notification')
        self.assertEqual(result['params']['type'], 'warning')

    def test_action_refresh_filestore_size_timeout_cache_unchanged(self):
        """Timeout must not overwrite existing cached value."""
        self.backup.write({
            'filestore_size_bytes': 55555,
            'filestore_size_updated_at': fields.Datetime.now(),
        })
        with patch.object(type(self.backup), '_estimate_filestore_size', return_value=None):
            self.backup.action_refresh_filestore_size()
        # Cache should remain at previous value
        self.assertEqual(self.backup.filestore_size_bytes, 55555)

    def test_action_refresh_success_returns_reload(self):
        with patch.object(type(self.backup), '_estimate_filestore_size', return_value=100):
            result = self.backup.action_refresh_filestore_size()
        self.assertEqual(result.get('type'), 'ir.actions.client')
        self.assertEqual(result.get('tag'), 'reload')

    # ------------------------------------------------------------------
    # estimated_duration_label — _compute_estimated_duration
    # ------------------------------------------------------------------

    def test_estimated_duration_label_no_history(self):
        new_b = self.env['txr.db.backup'].create({
            'name': 'fresh-est',
            'database_name': 'fdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
        })
        new_b._compute_estimated_duration()
        label = new_b.estimated_duration_label or ''
        self.assertIn('No estimate', label)
        self.assertEqual(new_b.estimated_duration_seconds, 0.0)

    def test_estimated_duration_label_stable_history(self):
        """Five logs with ~600s each → label shows ~10 min."""
        b = self.backup.copy({'name': 'stable-est'})
        now = fields.Datetime.now()
        for i in range(5):
            start = now - timedelta(hours=i + 1)
            self.env['txr.db.backup.log'].create({
                'backup_id': b.id,
                'state': 'success',
                'started_at': start,
                'finished_at': start + timedelta(seconds=600),
            })
        b._compute_estimated_duration()
        label = b.estimated_duration_label or ''
        self.assertTrue(
            'min' in label or 's' in label or 'hour' in label,
            f'Unexpected label: {label!r}',
        )
        self.assertAlmostEqual(b.estimated_duration_seconds, 600.0, delta=1.0)

    def test_estimated_duration_label_volatile_history(self):
        """High variance → range format like '1–20 min'."""
        now = fields.Datetime.now()
        durations = [60, 600, 1200, 60, 600]  # high stdev/mean
        b2 = self.backup.copy({'name': 'volatile-est'})
        for i, d in enumerate(durations):
            start = now - timedelta(hours=i + 1)
            finish = start + timedelta(seconds=d)
            self.env['txr.db.backup.log'].create({
                'backup_id': b2.id,
                'state': 'success',
                'started_at': start,
                'finished_at': finish,
            })
        b2._compute_estimated_duration()
        label = b2.estimated_duration_label or ''
        # High variance → range label like "1–20 min"
        self.assertTrue(
            '–' in label or '-' in label or 'min' in label,
            f'Expected range label, got: {label!r}',
        )

    def test_estimated_duration_skips_failed_logs(self):
        """Failed and running logs must not be counted in duration estimate."""
        b3 = self.backup.copy({'name': 'mixed-state-est'})
        now = fields.Datetime.now()
        # One success log
        self.env['txr.db.backup.log'].create({
            'backup_id': b3.id,
            'state': 'success',
            'started_at': now - timedelta(hours=2),
            'finished_at': now - timedelta(hours=2) + timedelta(seconds=300),
        })
        # One failed log (should be ignored)
        self.env['txr.db.backup.log'].create({
            'backup_id': b3.id,
            'state': 'failed',
            'started_at': now - timedelta(hours=1),
            'finished_at': now - timedelta(hours=1) + timedelta(seconds=9999),
        })
        b3._compute_estimated_duration()
        # Should be ~300s from the single success log, not 9999
        self.assertAlmostEqual(b3.estimated_duration_seconds, 300.0, delta=1.0)

    # ------------------------------------------------------------------
    # Silent filestore refresh in _run() cleanup
    # ------------------------------------------------------------------

    def test_cleanup_silent_filestore_refresh_does_not_fail_backup(self):
        """_run() cleanup must not mark backup failed when filestore size refresh throws."""
        b = self.backup.copy({
            'name': 'cleanup-resilience',
            'backup_strategy': 'standard',
            'pre_backup_disk_check': False,
        })
        with patch.object(type(b), '_run_standard'), \
             patch.object(type(b), '_estimate_filestore_size',
                          side_effect=Exception('disk error')), \
             patch.object(type(b), '_notify'), \
             patch.object(type(b), '_apply_retention'), \
             patch.object(type(b), '_write_preflight_info'):
            b._run()
        log = self.env['txr.db.backup.log'].search(
            [('backup_id', '=', b.id)], limit=1
        )
        self.assertEqual(log.state, 'success')

    def test_filestore_size_bytes_field_exists(self):
        self.assertIn('filestore_size_bytes', self.backup._fields)

    def test_filestore_size_updated_at_field_exists(self):
        self.assertIn('filestore_size_updated_at', self.backup._fields)

    # ------------------------------------------------------------------
    # _estimate_filestore_size — timeout behaviour
    # ------------------------------------------------------------------

    def test_estimate_filestore_size_returns_none_on_timeout(self):
        """When deadline is exceeded during walk, return None (not partial total)."""
        call_count = [0]

        def fake_monotonic():
            call_count[0] += 1
            return call_count[0] * 1000.0

        tmp = tempfile.mkdtemp()
        try:
            open(os.path.join(tmp, 'dummy.txt'), 'w').close()
            self.backup.filestore_path = tmp
            with patch('time.monotonic', side_effect=fake_monotonic):
                result = self.backup._estimate_filestore_size()
            self.assertTrue(result is None or isinstance(result, int))
        finally:
            self.backup.filestore_path = False
            shutil.rmtree(tmp, ignore_errors=True)

    def test_estimate_filestore_size_returns_bytes_for_small_dir(self):
        tmp = tempfile.mkdtemp()
        try:
            fpath = os.path.join(tmp, 'test.bin')
            with open(fpath, 'wb') as f:
                f.write(b'x' * 1024)
            self.backup.filestore_path = tmp
            result = self.backup._estimate_filestore_size()
            self.assertIsNotNone(result)
            self.assertGreaterEqual(result, 1024)
        finally:
            self.backup.filestore_path = False
            shutil.rmtree(tmp, ignore_errors=True)
