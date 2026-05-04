"""Tests for txr.db.restore.job: lifecycle, download, execute, cleanup."""
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged

MODULE = 'odoo.addons.txr_db_backup_pro.models.db_restore_job'


@tagged('txr_db_backup_pro')
class TestRestoreJob(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'restore-test',
            'database_name': 'srcdb',
            'storage_type': 'local',
            'local_path': '/tmp',
        })
        cls.log = cls.env['txr.db.backup.log'].create({
            'backup_id': cls.backup.id,
            'state': 'success',
            'file_path': '/tmp/srcdb_20260503.dump',
        })

    # ------------------------------------------------------------------ #
    #  action_start_restore (on backup log)
    # ------------------------------------------------------------------ #

    def test_action_start_restore_creates_draft_job(self):
        result = self.log.action_start_restore()
        self.assertEqual(result.get('res_model'), 'txr.db.restore.job')
        jobs = self.env['txr.db.restore.job'].search([
            ('backup_log_id', '=', self.log.id),
            ('state', '=', 'draft'),
        ])
        self.assertGreaterEqual(len(jobs), 1)

    def test_action_start_restore_returns_window_action(self):
        result = self.log.action_start_restore()
        self.assertEqual(result.get('type'), 'ir.actions.act_window')

    def test_action_start_restore_concurrency_protection(self):
        self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'state': 'pending',
        })
        result = self.log.action_start_restore()
        self.assertEqual(result.get('tag'), 'display_notification')

    def test_action_start_restore_fails_for_non_success_log(self):
        failed_log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'failed',
            'file_path': '/tmp/x.dump',
        })
        with self.assertRaises(UserError):
            failed_log.action_start_restore()

    # ------------------------------------------------------------------ #
    #  _onchange_backup_log_id
    # ------------------------------------------------------------------ #

    def test_onchange_target_db_default(self):
        job = self.env['txr.db.restore.job'].new({'backup_log_id': self.log.id})
        job._onchange_backup_log_id()
        self.assertIn('srcdb_restored_', job.target_db or '')

    # ------------------------------------------------------------------ #
    #  action_submit
    # ------------------------------------------------------------------ #

    def test_action_submit_rejects_current_db(self):
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': self.env.cr.dbname,
        })
        with self.assertRaises(UserError):
            job.action_submit()

    def test_action_submit_rejects_empty_target(self):
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': False,
        })
        with self.assertRaises(UserError):
            job.action_submit()

    def test_action_submit_creates_cron(self):
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'newrestore_db_unique1',
        })
        job.action_submit()
        self.assertTrue(job.cron_id)
        self.assertEqual(job.state, 'pending')

    def test_action_submit_returns_close_action(self):
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'newrestore_db_unique2',
        })
        result = job.action_submit()
        self.assertEqual(result.get('type'), 'ir.actions.act_window_close')

    # ------------------------------------------------------------------ #
    #  _download_to_tmp
    # ------------------------------------------------------------------ #

    def test_download_local_returns_log_file_path(self):
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'tgt_local',
        })
        tmp_dir, path = job._download_to_tmp()
        self.assertIsNone(tmp_dir)
        self.assertEqual(path, self.log.file_path)

    def test_download_unknown_storage_raises(self):
        backup_other = self.env['txr.db.backup'].create({
            'name': 'other-storage-test',
            'database_name': 'srcdb',
            'storage_type': 'local',
            'local_path': '/tmp',
        })
        log_other = self.env['txr.db.backup.log'].create({
            'backup_id': backup_other.id,
            'state': 'success',
            'file_path': '/tmp/x.dump',
        })
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': log_other.id,
            'target_db': 'tgt_unknown',
        })
        # Temporarily change storage_type to an unknown value via SQL to bypass selection constraint
        self.env.cr.execute(
            "UPDATE txr_db_backup SET storage_type = 'unknown_type' WHERE id = %s",
            (backup_other.id,),
        )
        backup_other.invalidate_recordset()
        with self.assertRaises(UserError):
            job._download_to_tmp()

    # ------------------------------------------------------------------ #
    #  _execute – full flow
    # ------------------------------------------------------------------ #

    def test_execute_full_flow_success(self):
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'restored_success_x',
        })
        with patch.object(type(job), '_download_to_tmp', return_value=(None, '/tmp/x.dump')), \
             patch.object(type(job), '_decrypt_if_needed', side_effect=lambda p: p), \
             patch.object(type(job), '_restore_db'), \
             patch.object(type(job), '_notify'):
            job._execute()

        self.assertEqual(job.state, 'done')
        self.assertIsNotNone(job.finished_at)
        self.assertIn('restored_success_x', job.result_message or '')

    def test_execute_failure_writes_traceback(self):
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'restored_fail_x',
        })
        with patch.object(type(job), '_download_to_tmp', return_value=(None, '/tmp/x.dump')), \
             patch.object(type(job), '_decrypt_if_needed', side_effect=lambda p: p), \
             patch.object(type(job), '_restore_db', side_effect=ValueError('fake restore error')), \
             patch.object(type(job), '_notify'):
            job._execute()

        self.assertEqual(job.state, 'failed')
        self.assertIn('fake restore error', job.result_message or '')
        self.assertTrue(job.error_traceback)

    def test_execute_cleans_up_tmp_dir_on_success(self):
        real_tmp = tempfile.mkdtemp(prefix='txr_test_')
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'restored_cleanup_x',
        })

        with patch.object(type(job), '_download_to_tmp', return_value=(real_tmp, '/tmp/x.dump')), \
             patch.object(type(job), '_decrypt_if_needed', side_effect=lambda p: p), \
             patch.object(type(job), '_restore_db'), \
             patch.object(type(job), '_notify'):
            job._execute()

        self.assertFalse(os.path.exists(real_tmp))

    def test_execute_sets_downloading_state_first(self):
        states_seen = []

        def fake_download():
            states_seen.append(job.state)
            return (None, '/tmp/x.dump')

        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'restored_state_check',
        })

        with patch.object(type(job), '_download_to_tmp', side_effect=fake_download), \
             patch.object(type(job), '_decrypt_if_needed', side_effect=lambda p: p), \
             patch.object(type(job), '_restore_db'), \
             patch.object(type(job), '_notify'):
            job._execute()

        self.assertIn('downloading', states_seen)

    # ------------------------------------------------------------------ #
    #  _cleanup_draft_orphans
    # ------------------------------------------------------------------ #

    def test_cleanup_draft_orphans_removes_old(self):
        old_log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'file_path': '/tmp/old.dump',
        })
        old_job = self.env['txr.db.restore.job'].create({
            'backup_log_id': old_log.id,
            'state': 'draft',
        })
        # Force create_date to 10 days ago
        self.env.cr.execute(
            "UPDATE txr_db_restore_job SET create_date = %s WHERE id = %s",
            (datetime.now() - timedelta(days=10), old_job.id),
        )
        old_job.invalidate_recordset()

        count = self.env['txr.db.restore.job']._cleanup_draft_orphans()
        self.assertGreaterEqual(count, 1)
        self.assertFalse(old_job.exists())

    def test_cleanup_draft_orphans_keeps_recent(self):
        recent_log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'file_path': '/tmp/recent.dump',
        })
        recent_job = self.env['txr.db.restore.job'].create({
            'backup_log_id': recent_log.id,
            'state': 'draft',
        })
        # Just created → within 7 days → should not be deleted
        self.env['txr.db.restore.job']._cleanup_draft_orphans()
        self.assertTrue(recent_job.exists())

    def test_cleanup_draft_orphans_keeps_non_draft(self):
        non_draft_log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'file_path': '/tmp/nd.dump',
        })
        done_job = self.env['txr.db.restore.job'].create({
            'backup_log_id': non_draft_log.id,
            'state': 'done',
        })
        # Force create_date old
        self.env.cr.execute(
            "UPDATE txr_db_restore_job SET create_date = %s WHERE id = %s",
            (datetime.now() - timedelta(days=10), done_job.id),
        )
        done_job.invalidate_recordset()
        self.env['txr.db.restore.job']._cleanup_draft_orphans()
        # done job must survive
        self.assertTrue(done_job.exists())

    # ------------------------------------------------------------------ #
    #  _notify
    # ------------------------------------------------------------------ #

    def test_notify_no_partners_no_message_post(self):
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'restored_notify_none',
            'state': 'done',
            'result_message': 'OK',
            'triggered_by_uid': False,
        })
        with patch.object(self.backup.__class__, 'message_post') as mocked:
            job._notify()
        mocked.assert_not_called()

    def test_notify_sends_message_on_done_with_partner(self):
        partner = self.env['res.partner'].create({'name': 'Test Notify Partner'})
        self.backup.write({'notify_partner_ids': [(4, partner.id)]})

        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'restored_notify_ok',
            'state': 'done',
            'result_message': 'Restored OK',
        })
        with patch.object(self.backup.__class__, 'message_post') as mocked:
            job._notify()
        mocked.assert_called_once()
        call_kwargs = mocked.call_args[1]
        self.assertIn('RESTORED', call_kwargs.get('subject', ''))

    def test_notify_sends_failed_subject_on_failed_state(self):
        partner = self.env['res.partner'].create({'name': 'Test Notify Partner2'})
        self.backup.write({'notify_partner_ids': [(4, partner.id)]})

        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'restored_notify_fail',
            'state': 'failed',
            'result_message': 'Something went wrong',
        })
        with patch.object(self.backup.__class__, 'message_post') as mocked:
            job._notify()
        mocked.assert_called_once()
        call_kwargs = mocked.call_args[1]
        self.assertIn('RESTORE FAILED', call_kwargs.get('subject', ''))

    def test_notify_skips_for_intermediate_state(self):
        partner = self.env['res.partner'].create({'name': 'Test Notify Partner3'})
        self.backup.write({'notify_partner_ids': [(4, partner.id)]})

        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.log.id,
            'target_db': 'restored_notify_pending',
            'state': 'pending',
        })
        with patch.object(self.backup.__class__, 'message_post') as mocked:
            job._notify()
        mocked.assert_not_called()
