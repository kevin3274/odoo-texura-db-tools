"""Tests for L3 (full restore) verification: scheduling, preflight, resource priority."""
import os
import subprocess
import tempfile
import zipfile
from datetime import datetime
from unittest.mock import MagicMock, patch

import psycopg2

from odoo import fields
from odoo.tests.common import TransactionCase, tagged

MODULE = 'odoo.addons.txr_db_backup_pro.models.db_backup'


def _make_pg_mock(fetchone_side_effect):
    mock_cur = MagicMock()
    mock_cur.__enter__ = lambda s: mock_cur
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchone.side_effect = fetchone_side_effect
    mock_conn = MagicMock()
    mock_conn.__enter__ = lambda s: mock_conn
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cur
    mock_conn.close = MagicMock()
    return mock_conn


@tagged('txr_db_backup_pro')
class TestL3Verification(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'l3-test',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': '/tmp',
            'verify_l3_enabled': True,
            'verify_l3_frequency': 'every',
            'verify_l3_low_priority': True,
        })
        cls.log = cls.env['txr.db.backup.log'].create({
            'backup_id': cls.backup.id,
            'state': 'success',
            'file_path': '/tmp/test.dump',
            'file_size': 1024 * 1024,
        })

    # ------------------------------------------------------------------ #
    #  _should_run_l3
    # ------------------------------------------------------------------ #

    def test_should_run_l3_disabled(self):
        b = self.backup.copy({'verify_l3_enabled': False})
        self.assertFalse(b._should_run_l3())

    def test_should_run_l3_every(self):
        self.assertTrue(self.backup._should_run_l3())

    def test_should_run_l3_weekly_recent(self):
        b = self.backup.copy({
            'verify_l3_frequency': 'weekly',
            'verify_l3_last_run': fields.Datetime.now(),
        })
        self.assertFalse(b._should_run_l3())

    def test_should_run_l3_weekly_no_last_run(self):
        b = self.backup.copy({
            'verify_l3_frequency': 'weekly',
            'verify_l3_last_run': False,
        })
        self.assertTrue(b._should_run_l3())

    def test_should_run_l3_monthly_recent(self):
        b = self.backup.copy({
            'verify_l3_frequency': 'monthly',
            'verify_l3_last_run': fields.Datetime.now(),
        })
        self.assertFalse(b._should_run_l3())

    # ------------------------------------------------------------------ #
    #  _next_window_start (static method, no mock needed for datetime)
    # ------------------------------------------------------------------ #

    def test_next_window_start_in_window(self):
        result = self.backup._next_window_start(2.0, 4.0, datetime(2026, 5, 3, 3, 0))
        self.assertIsNone(result)

    def test_next_window_start_before_window(self):
        result = self.backup._next_window_start(2.0, 4.0, datetime(2026, 5, 3, 1, 0))
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 2)

    def test_next_window_start_after_window(self):
        result = self.backup._next_window_start(2.0, 4.0, datetime(2026, 5, 3, 5, 0))
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 2)
        self.assertEqual(result.day, 4)

    def test_next_window_start_cross_midnight_in_window(self):
        result = self.backup._next_window_start(22.0, 6.0, datetime(2026, 5, 3, 23, 0))
        self.assertIsNone(result)

    def test_next_window_start_cross_midnight_in_window_early(self):
        result = self.backup._next_window_start(22.0, 6.0, datetime(2026, 5, 3, 3, 0))
        self.assertIsNone(result)

    def test_next_window_start_cross_midnight_out_of_window(self):
        result = self.backup._next_window_start(22.0, 6.0, datetime(2026, 5, 3, 12, 0))
        self.assertIsNotNone(result)
        self.assertEqual(result.hour, 22)
        self.assertEqual(result.day, 3)

    # ------------------------------------------------------------------ #
    #  _build_l3_cmd
    # ------------------------------------------------------------------ #

    def test_build_l3_cmd_low_priority_with_ionice(self):
        with patch(f'{MODULE}._HAS_IONICE', True), patch(f'{MODULE}._HAS_NICE', True):
            cmd = self.backup._build_l3_cmd(['pg_restore', '-d', 'tmp_db', 'file.dump'])

        self.assertEqual(cmd[:3], ['ionice', '-c', '3'])
        self.assertEqual(cmd[3:6], ['nice', '-n', '10'])
        self.assertIn('pg_restore', cmd)

    def test_build_l3_cmd_only_nice(self):
        with patch(f'{MODULE}._HAS_IONICE', False), patch(f'{MODULE}._HAS_NICE', True):
            cmd = self.backup._build_l3_cmd(['pg_restore', '-d', 'tmp_db', 'file.dump'])

        self.assertEqual(cmd[:3], ['nice', '-n', '10'])
        self.assertNotIn('ionice', cmd)

    def test_build_l3_cmd_no_priority_tools(self):
        with patch(f'{MODULE}._HAS_IONICE', False), patch(f'{MODULE}._HAS_NICE', False):
            cmd = self.backup._build_l3_cmd(['pg_restore', '-d', 'tmp_db'])

        self.assertEqual(cmd, ['pg_restore', '-d', 'tmp_db'])

    def test_build_l3_cmd_disabled_low_priority(self):
        b = self.backup.copy({'verify_l3_low_priority': False})
        with patch(f'{MODULE}._HAS_IONICE', True), patch(f'{MODULE}._HAS_NICE', True):
            cmd = b._build_l3_cmd(['pg_restore', '-d', 'tmp_db'])

        self.assertEqual(cmd, ['pg_restore', '-d', 'tmp_db'])

    def test_build_l3_cmd_only_ionice_no_nice(self):
        with patch(f'{MODULE}._HAS_IONICE', True), patch(f'{MODULE}._HAS_NICE', False):
            cmd = self.backup._build_l3_cmd(['pg_restore'])

        self.assertEqual(cmd[:3], ['ionice', '-c', '3'])
        self.assertNotIn('nice', cmd)

    # ------------------------------------------------------------------ #
    #  _l3_preflight
    # ------------------------------------------------------------------ #

    def _preflight_log(self):
        return self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'file_path': '/tmp/x.dump',
        })

    def test_l3_preflight_writes_info_sufficient_space(self):
        mock_conn = _make_pg_mock([('/var/lib/postgresql/data',), (500 * 1024 ** 3,)])
        with patch(f'{MODULE}.psycopg2.connect', return_value=mock_conn), \
             patch(f'{MODULE}.shutil.disk_usage', return_value=MagicMock(free=10 ** 12)):
            log = self._preflight_log()
            self.backup._l3_preflight(log, '/tmp/x.dump')

        info = log.l3_preflight_info or ''
        self.assertGreater(len(info), 0)

    def test_l3_preflight_warns_insufficient_space(self):
        mock_conn = _make_pg_mock([('/var/lib/postgresql/data',), (500 * 1024 ** 3,)])
        with patch(f'{MODULE}.psycopg2.connect', return_value=mock_conn), \
             patch(f'{MODULE}.shutil.disk_usage', return_value=MagicMock(free=100 * 1024 ** 2)):
            log = self._preflight_log()
            self.backup._l3_preflight(log, '/tmp/x.dump')

        info = log.l3_preflight_info or ''
        self.assertIn('⚠️', info)

    def test_l3_preflight_handles_connection_failure(self):
        with patch(f'{MODULE}.psycopg2.connect', side_effect=Exception('conn failed')):
            log = self._preflight_log()
            self.backup._l3_preflight(log, '/tmp/x.dump')

        info = log.l3_preflight_info or ''
        self.assertIn('continuing', info)

    # ------------------------------------------------------------------ #
    #  _verify_level3 – main flow
    # ------------------------------------------------------------------ #

    def _make_success_run(self, returncode=0, stderr=''):
        cp = MagicMock()
        cp.returncode = returncode
        cp.stderr = stderr
        cp.stdout = ''
        return cp

    def _l3_log(self):
        return self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'file_path': '/tmp/test.dump',
        })

    def _run_verify_level3(self, log, run_return=None, run_side_effect=None, pg_connect=None):
        run_patch = (
            patch(f'{MODULE}.subprocess.run', side_effect=run_side_effect)
            if run_side_effect is not None
            else patch(f'{MODULE}.subprocess.run', return_value=run_return or self._make_success_run())
        )
        pg_patch = pg_connect or patch(f'{MODULE}.psycopg2.connect')
        with run_patch, \
             patch(f'{MODULE}.shutil.which', return_value=None), \
             patch(f'{MODULE}.tempfile.mkdtemp', return_value='/tmp/fake_l3'), \
             patch(f'{MODULE}.shutil.rmtree'), \
             pg_patch:
            self.backup._verify_level3(log, '/tmp/test.dump')

    def test_verify_level3_passed(self):
        log = self._l3_log()
        mock_conn = _make_pg_mock([(5,), (2,), (100,)])
        self._run_verify_level3(
            log,
            pg_connect=patch(f'{MODULE}.psycopg2.connect', return_value=mock_conn),
        )
        self.assertEqual(log.verify_l3, 'passed')

    def test_verify_level3_failed_empty_table(self):
        log = self._l3_log()
        mock_conn = _make_pg_mock([(0,)])
        self._run_verify_level3(
            log,
            pg_connect=patch(f'{MODULE}.psycopg2.connect', return_value=mock_conn),
        )
        self.assertEqual(log.verify_l3, 'failed')

    def test_verify_level3_timeout_on_createdb(self):
        log = self._l3_log()
        self._run_verify_level3(
            log,
            run_side_effect=subprocess.TimeoutExpired(cmd='createdb', timeout=60),
        )
        self.assertEqual(log.verify_l3, 'skipped')

    def test_verify_level3_insufficient_privilege_createdb(self):
        log = self._l3_log()
        self._run_verify_level3(
            log,
            run_return=self._make_success_run(returncode=1, stderr='permission denied to create database'),
        )
        self.assertEqual(log.verify_l3, 'skipped')

    def test_verify_level3_createdb_other_failure(self):
        log = self._l3_log()
        self._run_verify_level3(
            log,
            run_return=self._make_success_run(returncode=1, stderr='some other error'),
        )
        self.assertEqual(log.verify_l3, 'failed')

    def test_verify_level3_insufficient_privilege_query(self):
        log = self._l3_log()
        self._run_verify_level3(
            log,
            pg_connect=patch(
                f'{MODULE}.psycopg2.connect',
                side_effect=psycopg2.errors.InsufficientPrivilege('denied'),
            ),
        )
        self.assertEqual(log.verify_l3, 'skipped')

    # ------------------------------------------------------------------ #
    #  _schedule_l3_cron – window scheduling
    # ------------------------------------------------------------------ #

    def _schedule_l3_with_preflight(self, backup, log, extra_patch=None):
        mock_conn = _make_pg_mock([('/pgdata',), (1024 ** 3,)])
        pg = patch(f'{MODULE}.psycopg2.connect', return_value=mock_conn)
        disk = patch(f'{MODULE}.shutil.disk_usage', return_value=MagicMock(free=10 ** 12))
        if extra_patch:
            with pg, disk, extra_patch:
                backup._schedule_l3_cron(log, '/tmp/x.dump')
        else:
            with pg, disk:
                backup._schedule_l3_cron(log, '/tmp/x.dump')

    def test_schedule_l3_cron_no_window(self):
        b = self.backup.copy({'verify_l3_window_enabled': False})
        log = self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'success',
            'file_path': '/tmp/x.dump',
        })
        self._schedule_l3_with_preflight(b, log)
        crons = self.env['ir.cron'].search([('name', 'like', f'L3 verify: {b.name}')])
        self.assertTrue(crons)
        self.assertIsNotNone(crons[0].nextcall)

    def test_schedule_l3_cron_window_out(self):
        b = self.backup.copy({
            'verify_l3_window_enabled': True,
            'verify_l3_window_start': 22.0,
            'verify_l3_window_end': 6.0,
        })
        log = self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'success',
            'file_path': '/tmp/x.dump',
        })
        noon = datetime(2026, 5, 3, 12, 0)
        self._schedule_l3_with_preflight(
            b, log,
            extra_patch=patch(f'{MODULE}.fields.Datetime.now', return_value=noon),
        )
        crons = self.env['ir.cron'].search([('name', 'like', f'L3 verify: {b.name}')])
        self.assertTrue(crons)
        self.assertEqual(crons[0].nextcall.hour, 22)


# ──────────────────────────────────────────────────────────────────────────────
#  _l3_restore_zip
# ──────────────────────────────────────────────────────────────────────────────

@tagged('txr_db_backup_pro')
class TestL3RestoreZip(TransactionCase):
    """_l3_restore_zip: extracts dump.sql from Odoo zip and runs psql."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'l3-zip-test',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': '/tmp',
            'backup_format': 'zip',
            'verify_l3_enabled': True,
            'verify_l3_low_priority': False,
        })

    def _make_log(self):
        return self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'file_path': '/tmp/fake.zip',
        })

    @staticmethod
    def _create_zip_with_dump(path, content=b'SELECT 1;'):
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('dump.sql', content)

    @staticmethod
    def _create_zip_without_dump(path):
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('manifest.json', '{}')

    def test_l3_restore_zip_calls_psql_with_correct_db(self):
        log = self._make_log()
        with tempfile.TemporaryDirectory() as work_tmp:
            zip_path = os.path.join(work_tmp, 'backup.zip')
            self._create_zip_with_dump(zip_path)

            mock_cp = MagicMock()
            mock_cp.returncode = 0

            with patch(f'{MODULE}.subprocess.run', return_value=mock_cp) as run_mock:
                self.backup._l3_restore_zip(log, zip_path, 'target_db', work_tmp, 30)

        cmd_args = run_mock.call_args[0][0]
        self.assertIn('psql', cmd_args)
        self.assertIn('-d', cmd_args)
        self.assertIn('target_db', cmd_args)

    def test_l3_restore_zip_raises_when_no_dump_sql(self):
        log = self._make_log()
        with tempfile.TemporaryDirectory() as work_tmp:
            zip_path = os.path.join(work_tmp, 'backup.zip')
            self._create_zip_without_dump(zip_path)

            with self.assertRaises(RuntimeError) as cm:
                self.backup._l3_restore_zip(log, zip_path, 'target_db', work_tmp, 30)

        self.assertIn('dump.sql', str(cm.exception))

    def test_l3_restore_zip_raises_when_psql_fails(self):
        log = self._make_log()
        with tempfile.TemporaryDirectory() as work_tmp:
            zip_path = os.path.join(work_tmp, 'backup.zip')
            self._create_zip_with_dump(zip_path)

            mock_cp = MagicMock()
            mock_cp.returncode = 1
            mock_cp.stderr = 'psql error: connection refused'

            with patch(f'{MODULE}.subprocess.run', return_value=mock_cp):
                with self.assertRaises(RuntimeError) as cm:
                    self.backup._l3_restore_zip(log, zip_path, 'target_db', work_tmp, 30)

        self.assertIn('psql exit 1', str(cm.exception))
