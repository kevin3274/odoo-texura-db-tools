"""Tests for _upload_rclone: cloud storage upload path in txr.db.backup."""
import tempfile
from subprocess import TimeoutExpired
from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

MODULE = 'odoo.addons.txr_db_backup_pro.models.db_backup'


@tagged('txr_db_backup_pro')
class TestUploadRclone(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.remote = cls.env['txr.db.backup.cloud'].create({
            'name': 'upload-s3',
            'rclone_type': 's3',
            's3_access_key': 'KEY',
            's3_secret_key': 'SECRET',
            's3_bucket': 'mybucket',
        })
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'upload-test',
            'database_name': 'testdb',
            'storage_type': 'rclone',
            'local_path': tempfile.gettempdir(),
            'cloud_remote_id': cls.remote.id,
            'cloud_path': 'backups/odoo',
        })

    def _ok(self):
        r = MagicMock()
        r.returncode = 0
        r.stderr = ''
        r.stdout = ''
        return r

    def _fail(self, stderr='upload error', stdout=''):
        r = MagicMock()
        r.returncode = 1
        r.stderr = stderr
        r.stdout = stdout
        return r

    def _new_log(self):
        return self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'running',
        })

    # ------------------------------------------------------------------ #
    #  Test 1: guard clause — no cloud_remote_id
    # ------------------------------------------------------------------ #

    def test_no_remote_raises_before_any_subprocess(self):
        b = self.env['txr.db.backup'].create({
            'name': 'no-remote-upload',
            'database_name': 'testdb',
            'storage_type': 'rclone',
            'local_path': tempfile.gettempdir(),
        })
        log = self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'running',
        })
        with self.assertRaises(UserError):
            b._upload_rclone(log, '/tmp/x.dump')

    # ------------------------------------------------------------------ #
    #  Tests 2 & 3: log.file_path update
    # ------------------------------------------------------------------ #

    def test_success_with_cloud_path_updates_log_file_path(self):
        log = self._new_log()
        with patch.object(type(self.remote), '_check_rclone_binary'), \
             patch.object(type(self.remote), '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(self.remote), '_cleanup_tmp_config'), \
             patch(f'{MODULE}.subprocess.run', return_value=self._ok()):
            self.backup._upload_rclone(log, '/tmp/mydb_20260503.dump')
        self.assertEqual(log.file_path, 'backups/odoo/mydb_20260503.dump')

    def test_success_no_cloud_path_writes_basename_only(self):
        b = self.backup.copy({'name': 'upload-no-path', 'cloud_path': False})
        log = self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'running',
        })
        with patch.object(type(self.remote), '_check_rclone_binary'), \
             patch.object(type(self.remote), '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(self.remote), '_cleanup_tmp_config'), \
             patch(f'{MODULE}.subprocess.run', return_value=self._ok()):
            b._upload_rclone(log, '/tmp/mydb_20260503.dump')
        self.assertEqual(log.file_path, 'mydb_20260503.dump')

    # ------------------------------------------------------------------ #
    #  Tests 4, 5, 6: failure behaviour
    # ------------------------------------------------------------------ #

    def test_rclone_failure_raises_user_error(self):
        log = self._new_log()
        with patch.object(type(self.remote), '_check_rclone_binary'), \
             patch.object(type(self.remote), '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(self.remote), '_cleanup_tmp_config'), \
             patch(f'{MODULE}.subprocess.run', return_value=self._fail()):
            with self.assertRaises(UserError):
                self.backup._upload_rclone(log, '/tmp/x.dump')

    def test_failure_error_message_contains_stderr(self):
        log = self._new_log()
        with patch.object(type(self.remote), '_check_rclone_binary'), \
             patch.object(type(self.remote), '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(self.remote), '_cleanup_tmp_config'), \
             patch(f'{MODULE}.subprocess.run',
                   return_value=self._fail(stderr='access denied to bucket')):
            with self.assertRaises(UserError) as ctx:
                self.backup._upload_rclone(log, '/tmp/x.dump')
        self.assertIn('access denied to bucket', str(ctx.exception))

    def test_failure_falls_back_to_stdout_when_stderr_empty(self):
        log = self._new_log()
        with patch.object(type(self.remote), '_check_rclone_binary'), \
             patch.object(type(self.remote), '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(self.remote), '_cleanup_tmp_config'), \
             patch(f'{MODULE}.subprocess.run',
                   return_value=self._fail(stderr='', stdout='bucket not found')):
            with self.assertRaises(UserError) as ctx:
                self.backup._upload_rclone(log, '/tmp/x.dump')
        self.assertIn('bucket not found', str(ctx.exception))

    # ------------------------------------------------------------------ #
    #  Tests 7 & 8: cleanup always called (finally block)
    # ------------------------------------------------------------------ #

    def test_cleanup_called_on_success(self):
        log = self._new_log()
        with patch.object(type(self.remote), '_check_rclone_binary'), \
             patch.object(type(self.remote), '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(self.remote), '_cleanup_tmp_config') as mock_cleanup, \
             patch(f'{MODULE}.subprocess.run', return_value=self._ok()):
            self.backup._upload_rclone(log, '/tmp/x.dump')
        mock_cleanup.assert_called_once_with('/tmp/rc.conf')

    def test_cleanup_called_on_failure(self):
        log = self._new_log()
        with patch.object(type(self.remote), '_check_rclone_binary'), \
             patch.object(type(self.remote), '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(self.remote), '_cleanup_tmp_config') as mock_cleanup, \
             patch(f'{MODULE}.subprocess.run', return_value=self._fail()):
            with self.assertRaises(UserError):
                self.backup._upload_rclone(log, '/tmp/x.dump')
        mock_cleanup.assert_called_once_with('/tmp/rc.conf')

    # ------------------------------------------------------------------ #
    #  Test 9: command structure
    # ------------------------------------------------------------------ #

    def test_uses_rclone_copy_with_correct_dest(self):
        log = self._new_log()
        with patch.object(type(self.remote), '_check_rclone_binary'), \
             patch.object(type(self.remote), '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(self.remote), '_cleanup_tmp_config'), \
             patch(f'{MODULE}.subprocess.run', return_value=self._ok()) as mock_run:
            self.backup._upload_rclone(log, '/tmp/x.dump')
        args = mock_run.call_args[0][0]
        self.assertEqual(args[0], 'rclone')
        self.assertIn('copy', args)
        self.assertTrue(any('upload-s3:backups/odoo' in str(a) for a in args))

    # ------------------------------------------------------------------ #
    #  Test 10: nested cloud_path uses posixpath (not os.path)
    # ------------------------------------------------------------------ #

    def test_nested_cloud_path_posixpath_join(self):
        b = self.backup.copy({'name': 'upload-nested', 'cloud_path': 'a/b/c'})
        log = self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'running',
        })
        with patch.object(type(self.remote), '_check_rclone_binary'), \
             patch.object(type(self.remote), '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(self.remote), '_cleanup_tmp_config'), \
             patch(f'{MODULE}.subprocess.run', return_value=self._ok()):
            b._upload_rclone(log, '/tmp/mydb.dump')
        self.assertEqual(log.file_path, 'a/b/c/mydb.dump')

    # ------------------------------------------------------------------ #
    #  Test 11: timeout propagates unwrapped — documents current behaviour
    #  (unlike action_test_connection which wraps in UserError)
    # ------------------------------------------------------------------ #

    def test_timeout_propagates_as_subprocess_exception(self):
        log = self._new_log()
        with patch.object(type(self.remote), '_check_rclone_binary'), \
             patch.object(type(self.remote), '_write_tmp_config', return_value='/tmp/rc.conf'), \
             patch.object(type(self.remote), '_cleanup_tmp_config'), \
             patch(f'{MODULE}.subprocess.run',
                   side_effect=TimeoutExpired(cmd='rclone', timeout=3600)):
            with self.assertRaises(TimeoutExpired):
                self.backup._upload_rclone(log, '/tmp/x.dump')
