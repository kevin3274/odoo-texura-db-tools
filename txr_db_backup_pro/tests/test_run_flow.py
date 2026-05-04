"""Integration tests for the _run() dispatch chain and _finalize_landed_artifact().

P0: _run() orchestration — log creation, success/failure state transitions.
P1: _run_standard()/_finalize_landed_artifact() composition; _run_split() flow.
"""
import base64
import os
import shutil
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from odoo.tests.common import TransactionCase, tagged


# ──────────────────────────────────────────────────────────────────────────────
#  P0: _run() orchestration
# ──────────────────────────────────────────────────────────────────────────────

@tagged('txr_db_backup_pro')
class TestRunOrchestration(TransactionCase):
    """P0: _run() state-machine — log creation and success/failure transitions."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'run-orch-test',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'backup_format': 'zip',
            'compress': False,
            'backup_strategy': 'standard',
            'pre_backup_disk_check': False,
            'verify_l3_enabled': False,
        })

    def _run(self, run_standard_side_effect=None):
        """Call _run() with external I/O fully mocked; return the resulting log."""
        b = self.backup
        std_mock = MagicMock(side_effect=run_standard_side_effect)

        with ExitStack() as stack:
            stack.enter_context(patch.object(type(b), '_run_standard', std_mock))
            stack.enter_context(patch.object(type(b), '_apply_retention'))
            stack.enter_context(
                patch.object(type(b), '_estimate_filestore_size', return_value=None)
            )
            b._run()

        return self.env['txr.db.backup.log'].search(
            [('backup_id', '=', b.id)], limit=1, order='id desc'
        )

    # ------------------------------------------------------------------
    #  Success path
    # ------------------------------------------------------------------

    def test_run_creates_success_log(self):
        log = self._run()
        self.assertEqual(log.state, 'success')

    def test_run_sets_finished_at(self):
        log = self._run()
        self.assertTrue(log.finished_at)

    def test_run_dispatches_to_standard_strategy(self):
        b = self.backup
        std_mock = MagicMock()

        with ExitStack() as stack:
            stack.enter_context(patch.object(type(b), '_run_standard', std_mock))
            stack.enter_context(patch.object(type(b), '_apply_retention'))
            stack.enter_context(
                patch.object(type(b), '_estimate_filestore_size', return_value=None)
            )
            b._run()

        self.assertTrue(std_mock.called)

    def test_run_skips_if_already_running(self):
        """Concurrent-run guard: no second log if one is already running."""
        b = self.backup
        self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'running',
        })

        with ExitStack() as stack:
            stack.enter_context(patch.object(type(b), '_run_standard'))
            stack.enter_context(patch.object(type(b), '_apply_retention'))
            b._run()

        new_logs = self.env['txr.db.backup.log'].search(
            [('backup_id', '=', b.id), ('state', '!=', 'running')]
        )
        self.assertFalse(new_logs, 'no new log should be created while another is running')

    # ------------------------------------------------------------------
    #  Failure path
    # ------------------------------------------------------------------

    def test_run_failure_sets_failed_state(self):
        log = self._run(run_standard_side_effect=RuntimeError('pg_dump error'))
        self.assertEqual(log.state, 'failed')

    def test_run_failure_captures_error_message(self):
        log = self._run(run_standard_side_effect=RuntimeError('pg_dump error'))
        self.assertIn('pg_dump error', log.error_message)

    def test_run_failure_captures_traceback(self):
        log = self._run(run_standard_side_effect=RuntimeError('pg_dump error'))
        self.assertTrue(log.error_traceback)
        self.assertIn('RuntimeError', log.error_traceback)


# ──────────────────────────────────────────────────────────────────────────────
#  P1: _run_standard() + _finalize_landed_artifact() chain
# ──────────────────────────────────────────────────────────────────────────────

@tagged('txr_db_backup_pro')
class TestRunStandardChain(TransactionCase):
    """P1: _run_standard() calls through _finalize_landed_artifact() correctly."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._work_dir = tempfile.mkdtemp(prefix='txr_test_std_')
        cls._fake_dump = os.path.join(cls._work_dir, 'fake.dump')
        with open(cls._fake_dump, 'wb') as f:
            f.write(b'fake pg_dump output for integration test')

        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'run-chain-test',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'backup_format': 'dump',
            'compress': False,
            'backup_strategy': 'standard',
            'pre_backup_disk_check': False,
            'verify_l3_enabled': False,
            'encrypt_backup': False,
        })

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._work_dir, ignore_errors=True)
        super().tearDownClass()

    def _make_log(self):
        return self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'running',
        })

    def _call_run_standard(self):
        b = self.backup
        log = self._make_log()
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(type(b), '_do_dump', return_value=self._fake_dump)
            )
            stack.enter_context(patch.object(type(b), '_dispatch_upload'))
            stack.enter_context(patch.object(type(b), '_verify_level2'))
            stack.enter_context(patch.object(type(b), '_maybe_schedule_l3'))
            b._run_standard(log, tempfile.gettempdir())
        return log

    def test_run_standard_writes_sha256(self):
        log = self._call_run_standard()
        self.assertTrue(log.sha256)
        self.assertEqual(len(log.sha256), 64)

    def test_run_standard_writes_file_size(self):
        log = self._call_run_standard()
        self.assertGreater(log.file_size, 0)

    def test_run_standard_passes_l1_verification(self):
        log = self._call_run_standard()
        self.assertEqual(log.verify_l1, 'passed')


# ──────────────────────────────────────────────────────────────────────────────
#  P1: _finalize_landed_artifact() with compress=True + encrypt=True
# ──────────────────────────────────────────────────────────────────────────────

@tagged('txr_db_backup_pro')
class TestFinalizeCompressEncrypt(TransactionCase):
    """P1: compress→encrypt→sha256→upload→L1 chain in _finalize_landed_artifact()."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'finalize-enc-test',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'backup_format': 'dump',
            'compress': True,
            'backup_strategy': 'standard',
            'pre_backup_disk_check': False,
            'verify_l3_enabled': False,
            'encrypt_backup': True,
            'encrypt_key': base64.b64encode(os.urandom(32)).decode(),
        })

    def _run_finalize(self):
        b = self.backup
        work_dir = tempfile.mkdtemp(prefix='txr_finalize_')
        dump_path = os.path.join(work_dir, 'test.dump')
        with open(dump_path, 'wb') as f:
            f.write(b'SELECT 1; -- fake dump for finalize chain test')

        log = self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'running',
        })

        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(type(b), '_dispatch_upload'))
                stack.enter_context(patch.object(type(b), '_verify_level2'))
                stack.enter_context(patch.object(type(b), '_maybe_schedule_l3'))
                b._finalize_landed_artifact(log, dump_path, compress=True)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        return log

    def test_finalize_sets_encrypted_flag(self):
        log = self._run_finalize()
        self.assertTrue(log.encrypted)

    def test_finalize_sets_sha256(self):
        log = self._run_finalize()
        self.assertTrue(log.sha256)
        self.assertEqual(len(log.sha256), 64)

    def test_finalize_sets_file_size(self):
        log = self._run_finalize()
        self.assertGreater(log.file_size, 0)

    def test_finalize_passes_l1_verification(self):
        log = self._run_finalize()
        self.assertEqual(log.verify_l1, 'passed')


# ──────────────────────────────────────────────────────────────────────────────
#  P1: _run_split() full flow
# ──────────────────────────────────────────────────────────────────────────────

@tagged('txr_db_backup_pro')
class TestRunSplit(TransactionCase):
    """P1: _run_split() — uses dump_db, writes log fields, optional filestore sync."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'run-split-test',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'backup_format': 'dump',
            'compress': False,
            'backup_strategy': 'split',
            'pre_backup_disk_check': False,
            'verify_l3_enabled': False,
            'encrypt_backup': False,
            'filestore_sync_enabled': False,
        })

    @staticmethod
    def _fake_dump_db(db_name, f_out, fmt):
        f_out.write(b'fake pg_dump output for split test')

    def _make_log(self, backup=None):
        backup = backup or self.backup
        return self.env['txr.db.backup.log'].create({
            'backup_id': backup.id,
            'state': 'running',
        })

    def _run_split(self, backup=None, sync_mock=None):
        b = backup or self.backup
        log = self._make_log(b)

        with ExitStack() as stack, tempfile.TemporaryDirectory(prefix='txr_split_') as tmp:
            stack.enter_context(patch('odoo.service.db.dump_db', self._fake_dump_db))
            stack.enter_context(
                patch.object(type(b), '_detect_pg_dump_version', return_value='pg_dump 15.0')
            )
            stack.enter_context(patch.object(type(b), '_dispatch_upload'))
            stack.enter_context(patch.object(type(b), '_verify_level2'))
            stack.enter_context(patch.object(type(b), '_maybe_schedule_l3'))
            if sync_mock is not None:
                stack.enter_context(patch.object(type(b), '_sync_filestore', sync_mock))
            b._run_split(log, tmp)

        return log

    # ------------------------------------------------------------------

    def test_run_split_writes_pg_dump_version(self):
        log = self._run_split()
        self.assertEqual(log.pg_dump_version, 'pg_dump 15.0')

    def test_run_split_computes_sha256(self):
        log = self._run_split()
        self.assertTrue(log.sha256)
        self.assertEqual(len(log.sha256), 64)

    def test_run_split_does_not_sync_when_disabled(self):
        sync_mock = MagicMock()
        self._run_split(sync_mock=sync_mock)
        sync_mock.assert_not_called()

    def test_run_split_calls_sync_filestore_when_enabled(self):
        b = self.env['txr.db.backup'].create({
            'name': 'run-split-sync',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'backup_format': 'dump',
            'compress': False,
            'backup_strategy': 'split',
            'pre_backup_disk_check': False,
            'verify_l3_enabled': False,
            'encrypt_backup': False,
            'filestore_sync_enabled': True,
        })
        sync_mock = MagicMock()
        self._run_split(backup=b, sync_mock=sync_mock)
        sync_mock.assert_called_once()
