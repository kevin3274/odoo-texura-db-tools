"""Tests for large DB strategy: pre-flight, split, streaming (task 7.7)."""
import os
import tempfile
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged
from odoo.tools import config


@tagged('txr_db_backup_pro')
class TestLargeDbStrategy(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'lg-test',
            'database_name': 'lgdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'pre_backup_disk_check': True,
            'pre_backup_disk_min_gb': 1.0,
        })

    # ------------------------------------------------------------------
    # _get_filestore_path
    # ------------------------------------------------------------------

    def test_get_filestore_path_user_override(self):
        custom = tempfile.mkdtemp(prefix='fs_test_')
        try:
            self.backup.filestore_path = custom
            self.assertEqual(self.backup._get_filestore_path(), custom)
        finally:
            self.backup.filestore_path = False
            os.rmdir(custom)

    def test_get_filestore_path_auto_derives_from_config(self):
        self.backup.filestore_path = False
        with patch('os.path.isdir', return_value=True), \
             patch('os.access', return_value=True):
            expected = os.path.join(
                config['data_dir'], 'filestore', self.backup.database_name
            )
            result = self.backup._get_filestore_path()
            self.assertEqual(result, expected)

    def test_get_filestore_path_invalid_raises(self):
        self.backup.filestore_path = '/nonexistent/totally/fake/path'
        try:
            with self.assertRaises(UserError):
                self.backup._get_filestore_path()
        finally:
            self.backup.filestore_path = False

    # ------------------------------------------------------------------
    # _pre_flight_disk_check
    # ------------------------------------------------------------------

    def test_pre_flight_disk_check_insufficient(self):
        self.backup.backup_strategy = 'standard'
        mock_du = MagicMock(free=0, total=10 ** 9, used=10 ** 9)
        with patch('shutil.disk_usage', return_value=mock_du), \
             patch.object(type(self.backup), '_estimate_db_size', return_value=10 ** 9), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=10 ** 9):
            with self.assertRaises(UserError):
                self.backup._pre_flight_disk_check(tempfile.gettempdir())

    def test_pre_flight_disk_check_sufficient(self):
        self.backup.backup_strategy = 'standard'
        mock_du = MagicMock(free=10 ** 12, total=2 * 10 ** 12, used=10 ** 12)
        with patch('shutil.disk_usage', return_value=mock_du), \
             patch.object(type(self.backup), '_estimate_db_size', return_value=10 ** 9), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=10 ** 9):
            # should not raise
            self.backup._pre_flight_disk_check(tempfile.gettempdir())

    def test_pre_flight_disk_check_streaming_skips(self):
        b = self.backup.copy({'backup_strategy': 'streaming', 'name': 'lg-stream'})
        mock_du = MagicMock(free=0)
        with patch('shutil.disk_usage', return_value=mock_du):
            # streaming mode skips check — should not raise
            b._pre_flight_disk_check(tempfile.gettempdir())

    def test_pre_flight_disk_check_split_uses_db_only(self):
        """Split strategy only factors db size, not filestore."""
        b = self.backup.copy({
            'backup_strategy': 'split',
            'pre_backup_disk_min_gb': 0.0,
            'name': 'lg-split',
        })
        db_size = 10 ** 9  # 1 GB
        # free = 2 GB > 1 GB * 1.5 = 1.5 GB
        mock_du = MagicMock(free=2 * 10 ** 9)
        with patch('shutil.disk_usage', return_value=mock_du), \
             patch.object(type(b), '_estimate_db_size', return_value=db_size):
            b._pre_flight_disk_check(tempfile.gettempdir())

    def test_pre_flight_disk_check_split_raises_when_tight(self):
        b = self.backup.copy({
            'backup_strategy': 'split',
            'pre_backup_disk_min_gb': 0.0,
            'name': 'lg-split-tight',
        })
        db_size = 10 ** 9  # 1 GB, needs 1.5 GB
        mock_du = MagicMock(free=1)  # essentially 0 free
        with patch('shutil.disk_usage', return_value=mock_du), \
             patch.object(type(b), '_estimate_db_size', return_value=db_size):
            with self.assertRaises(UserError):
                b._pre_flight_disk_check(tempfile.gettempdir())

    # ------------------------------------------------------------------
    # _sync_filestore (split strategy rclone sync)
    # ------------------------------------------------------------------

    def _make_cloud_backup(self, name, strategy, extra=None):
        """Create a backup + cloud remote pair for cloud-based tests."""
        remote = self.env['txr.db.backup.cloud'].create({
            'name': f'{name}-remote',
            'rclone_type': 's3',
        })
        vals = {
            'name': name,
            'backup_strategy': strategy,
            'storage_type': 'rclone',
            'cloud_remote_id': remote.id,
        }
        if extra:
            vals.update(extra)
        b = self.backup.copy(vals)
        return b, remote

    def _make_split_backup_with_remote(self):
        return self._make_cloud_backup(
            'lg-split-sync', 'split', {'filestore_sync_enabled': True}
        )

    def test_sync_filestore_runs_rclone_sync_with_immutable(self):
        b, remote = self._make_split_backup_with_remote()
        log = self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'running',
        })

        fake_proc = MagicMock()
        fake_proc.wait.return_value = 0
        fake_proc.stderr = iter([])
        fake_proc.stdout = iter([])

        with patch.object(type(remote),  '_check_rclone_binary'), \
             patch.object(type(remote),  '_write_tmp_config', return_value='/tmp/rclone.conf'), \
             patch.object(type(remote),  '_cleanup_tmp_config'), \
             patch('subprocess.Popen', return_value=fake_proc) as mock_popen, \
             patch.object(type(b), '_get_filestore_path', return_value='/tmp/fs'):
            b._sync_filestore(log)

        mock_popen.assert_called_once()
        call_args = mock_popen.call_args[0][0]  # first positional arg = cmd list
        self.assertIn('--immutable', call_args)
        self.assertIn('sync', call_args)

    def test_sync_filestore_nonzero_returncode_raises(self):
        b, remote = self._make_split_backup_with_remote()
        log = self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'running',
        })

        fake_proc = MagicMock()
        fake_proc.wait.return_value = 1
        fake_proc.stderr = iter(['rclone error'])
        fake_proc.stdout = iter([])

        with patch.object(type(remote),  '_check_rclone_binary'), \
             patch.object(type(remote),  '_write_tmp_config', return_value='/tmp/rclone.conf'), \
             patch.object(type(remote),  '_cleanup_tmp_config'), \
             patch('subprocess.Popen', return_value=fake_proc), \
             patch.object(type(b), '_get_filestore_path', return_value='/tmp/fs'):
            with self.assertRaises(RuntimeError):
                b._sync_filestore(log)

    # ------------------------------------------------------------------
    # _streaming_backup failures
    # ------------------------------------------------------------------

    def _make_streaming_backup(self):
        return self._make_cloud_backup(
            'lg-streaming', 'streaming', {'encrypt_backup': False}
        )

    def _make_streaming_log(self, backup):
        return self.env['txr.db.backup.log'].create({
            'backup_id': backup.id,
            'state': 'running',
            'verify_l1': 'skipped',
            'verify_l2': 'skipped',
            'verify_l3': 'skipped',
        })

    def _make_fake_proc(self, rc=0, stdout=None, stderr=None):
        proc = MagicMock()
        proc.stdout = stdout or MagicMock()
        proc.stderr = stderr or iter([])
        proc.stdin = MagicMock()
        proc.wait.return_value = rc
        proc.poll.return_value = rc
        return proc

    def test_streaming_backup_pg_dump_failure(self):
        b, remote = self._make_streaming_backup()
        log = self._make_streaming_log(b)

        dump_proc = self._make_fake_proc(rc=1, stderr=iter([b'dump failed']))
        rclone_proc = self._make_fake_proc(rc=0)

        def popen_side_effect(cmd, **kwargs):
            if 'pg_dump' in cmd:
                return dump_proc
            return rclone_proc

        with patch.object(type(remote),  '_check_rclone_binary'), \
             patch.object(type(remote),  '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(remote),  '_cleanup_tmp_config'), \
             patch('subprocess.Popen', side_effect=popen_side_effect), \
             patch.object(type(b), '_copy_pipe_with_timeout'):
            with self.assertRaises(RuntimeError) as ctx:
                b._streaming_backup(log)
        self.assertIn('pg_dump', str(ctx.exception))

    def test_streaming_backup_rclone_failure(self):
        b, remote = self._make_streaming_backup()
        log = self._make_streaming_log(b)

        dump_proc = self._make_fake_proc(rc=0, stderr=iter([]))
        rclone_proc = self._make_fake_proc(rc=1, stderr=iter([b'rclone error']))

        def popen_side_effect(cmd, **kwargs):
            if 'pg_dump' in cmd:
                return dump_proc
            return rclone_proc

        with patch.object(type(remote),  '_check_rclone_binary'), \
             patch.object(type(remote),  '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(remote),  '_cleanup_tmp_config'), \
             patch('subprocess.Popen', side_effect=popen_side_effect), \
             patch.object(type(b), '_copy_pipe_with_timeout'):
            with self.assertRaises(RuntimeError) as ctx:
                b._streaming_backup(log)
        self.assertIn('rclone', str(ctx.exception))

    def test_streaming_backup_bridge_exception(self):
        b, remote = self._make_streaming_backup()
        log = self._make_streaming_log(b)

        dump_proc = self._make_fake_proc(rc=0, stderr=iter([]))
        rclone_proc = self._make_fake_proc(rc=0, stderr=iter([]))

        def popen_side_effect(cmd, **kwargs):
            if 'pg_dump' in cmd:
                return dump_proc
            return rclone_proc

        with patch.object(type(remote),  '_check_rclone_binary'), \
             patch.object(type(remote),  '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(remote),  '_cleanup_tmp_config'), \
             patch('subprocess.Popen', side_effect=popen_side_effect), \
             patch.object(type(b), '_copy_pipe_with_timeout',
                          side_effect=Exception('pipe error')):
            with self.assertRaises(RuntimeError) as ctx:
                b._streaming_backup(log)
        self.assertIn('bridge', str(ctx.exception))

    def test_streaming_skips_l1_l2_l3(self):
        """_run_streaming writes 'skipped' for all verification levels."""
        b, remote = self._make_streaming_backup()
        log = self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'running',
        })

        with patch.object(type(b), '_streaming_backup'):
            b._run_streaming(log)

        self.assertEqual(log.verify_l1, 'skipped')
        self.assertEqual(log.verify_l2, 'skipped')
        self.assertEqual(log.verify_l3, 'skipped')

    def test_streaming_timeout_kills_procs(self):
        """Deadline breach causes procs to be killed and raises UserError."""
        b, remote = self._make_streaming_backup()

        # Use a proc whose poll() returns None (still running) so kill is called
        dump_proc = MagicMock()
        dump_proc.stdout = MagicMock()
        dump_proc.stderr = iter([])
        dump_proc.stdin = MagicMock()
        dump_proc.wait.return_value = 0
        dump_proc.poll.return_value = None  # still alive

        rclone_proc = MagicMock()
        rclone_proc.stdin = MagicMock()
        rclone_proc.stderr = iter([])
        rclone_proc.stdout = MagicMock()
        rclone_proc.wait.return_value = 0
        rclone_proc.poll.return_value = None  # still alive

        def popen_side_effect(cmd, **kwargs):
            if 'pg_dump' in cmd:
                return dump_proc
            return rclone_proc

        # Past deadline (always returns 10000 > deadline=0) → triggers kill + raise
        def fake_monotonic():
            return 10000.0

        with patch.object(type(remote),  '_check_rclone_binary'), \
             patch.object(type(remote),  '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(remote),  '_cleanup_tmp_config'), \
             patch('subprocess.Popen', side_effect=popen_side_effect), \
             patch('time.monotonic', side_effect=fake_monotonic):
            # _copy_pipe_with_timeout calls _check_streaming_deadline internally
            # We need it to actually run; but timeout_sec is computed from config
            # so we directly test _check_streaming_deadline:
            with self.assertRaises(UserError):
                procs = (dump_proc, rclone_proc)
                b._check_streaming_deadline(deadline=0, procs=procs)

        dump_proc.kill.assert_called_once()
        rclone_proc.kill.assert_called_once()
