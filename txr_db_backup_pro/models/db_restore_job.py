import base64
import logging
import os
import shutil
import subprocess
import tempfile
import traceback
from datetime import timedelta

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .db_backup import _ENCRYPT_NONCE_LEN

_logger = logging.getLogger(__name__)


class TxrDbRestoreJob(models.Model):
    _name = 'txr.db.restore.job'
    _description = 'Database Restore Job'
    _order = 'create_date desc'

    ACTIVE_STATES = ('pending', 'downloading', 'decrypting', 'restoring')

    backup_log_id = fields.Many2one(
        'txr.db.backup.log',
        required=True,
        ondelete='cascade',
        string='Backup Log',
    )
    target_db = fields.Char(string='Target Database')
    copy_uuid = fields.Boolean(
        default=True,
        string='Copy UUID',
        help='Preserve the source database UUID in the restored database.',
    )
    triggered_by_uid = fields.Many2one(
        'res.users',
        readonly=True,
        default=lambda self: self.env.user,
        string='Triggered By',
    )
    cron_id = fields.Many2one(
        'ir.cron',
        ondelete='set null',
        readonly=True,
        string='Cron Job',
    )
    state = fields.Selection(
        [
            ('draft', 'Draft'),
            ('pending', 'Pending'),
            ('downloading', 'Downloading'),
            ('decrypting', 'Decrypting'),
            ('restoring', 'Restoring'),
            ('done', 'Done'),
            ('failed', 'Failed'),
        ],
        default='draft',
        required=True,
        string='State',
    )
    phase_detail = fields.Char(string='Phase Detail')
    started_at = fields.Datetime(readonly=True, string='Started At')
    finished_at = fields.Datetime(readonly=True, string='Finished At')
    result_message = fields.Text(readonly=True, string='Result Message')
    error_traceback = fields.Text(readonly=True, string='Error Traceback')

    # ------------------------------------------------------------------ #
    #  Onchange
    # ------------------------------------------------------------------ #

    @api.onchange('backup_log_id')
    def _onchange_backup_log_id(self):
        if self.backup_log_id:
            src = self.backup_log_id.backup_id.database_name
            today = fields.Date.context_today(self).strftime('%Y%m%d')
            self.target_db = f'{src}_restored_{today}'

    # ------------------------------------------------------------------ #
    #  Public action: submit
    # ------------------------------------------------------------------ #

    def action_submit(self):
        self.ensure_one()
        if not self.target_db:
            raise UserError(_('Target database name is required.'))
        if self.target_db == self.env.cr.dbname:
            raise UserError(_(
                'Cannot restore over the current database. '
                'Use /web/database/manager instead.'
            ))
        self.state = 'pending'
        model_id = self.env['ir.model']._get_id('txr.db.restore.job')
        cron = self.env['ir.cron'].sudo().create({
            'name': f'Restore: {self.backup_log_id.backup_id.name} → {self.target_db}',
            'model_id': model_id,
            'state': 'code',
            'code': f'env["txr.db.restore.job"].browse([{self.id}])._execute()',
            'interval_number': 1,
            'interval_type': 'days',
            'nextcall': fields.Datetime.now(),
            'active': True,
            'user_id': self.env.ref('base.user_root').id,
        })
        self.cron_id = cron.id
        return {'type': 'ir.actions.act_window_close'}

    # ------------------------------------------------------------------ #
    #  Cron entry point
    # ------------------------------------------------------------------ #

    def _execute(self):
        self.ensure_one()
        if self.cron_id:
            self.cron_id.sudo().active = False
        self.write({'started_at': fields.Datetime.now(), 'state': 'downloading'})
        tmp_dir = None
        try:
            tmp_dir, file_path = self._download_to_tmp()
            file_path = self._decrypt_if_needed(file_path)
            self.state = 'restoring'
            self._restore_db(file_path)
            self.write({
                'state': 'done',
                'finished_at': fields.Datetime.now(),
                'result_message': f'Restored to {self.target_db} successfully.',
            })
        except Exception as exc:
            self.write({
                'state': 'failed',
                'finished_at': fields.Datetime.now(),
                'result_message': str(exc),
                'error_traceback': traceback.format_exc(),
            })
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            try:
                self._notify()
            except Exception as exc:
                _logger.warning('Restore notify failed: %s', exc)

    # ------------------------------------------------------------------ #
    #  Download helper
    # ------------------------------------------------------------------ #

    def _download_to_tmp(self):
        """Return (tmp_dir_or_None, local_file_path).

        For local storage returns (None, path) — no cleanup needed.
        For remote storage creates a temp dir and downloads the file into it.
        """
        log = self.backup_log_id
        backup = log.backup_id
        st = backup.storage_type

        if st == 'local':
            return None, log.file_path

        tmp_dir = tempfile.mkdtemp(prefix='txr_restore_')

        if st == 'sftp':
            backup._check_sftp_binary()
            cmd, key_tmp = backup._prepare_sftp_cmd()
            try:
                local_file = os.path.join(tmp_dir, os.path.basename(log.file_path))
                result = subprocess.run(
                    cmd,
                    input=f'get {log.file_path} {local_file}',
                    capture_output=True,
                    text=True,
                    timeout=3600,
                )
                if result.returncode != 0:
                    raise UserError(_('SFTP download failed: %s', result.stderr))
                return tmp_dir, local_file
            finally:
                backup._cleanup_sftp_key(key_tmp)

        if st == 'rclone':
            local_file = backup.cloud_remote_id._download_from_remote(
                log.file_path, tmp_dir
            )
            return tmp_dir, local_file

        raise UserError(_('Unknown storage type: %s', st))

    # ------------------------------------------------------------------ #
    #  Decrypt helper
    # ------------------------------------------------------------------ #

    def _decrypt_if_needed(self, path: str) -> str:
        """Decrypt AES-256-GCM encrypted file if path ends with .enc.

        Format: [4B chunk_size header][12B nonce | 4B ct_len | ciphertext]*
        Removes the source .enc file after successful decryption.
        """
        if not path.endswith('.enc'):
            return path

        self.state = 'decrypting'
        backup = self.backup_log_id.backup_id
        if not backup.encrypt_key:
            raise UserError(_(
                'Backup is encrypted but no encrypt_key is set on the backup config.'
            ))

        key_bytes = base64.b64decode(backup.encrypt_key)
        out_path = path[:-4]  # strip .enc
        aes = AESGCM(key_bytes)

        with open(path, 'rb') as f_in, open(out_path, 'wb') as f_out:
            # Read (and discard) the 4-byte chunk_size header written by the encryptor
            header = f_in.read(4)
            if len(header) < 4:
                raise UserError(_('Corrupted encrypted file (missing chunk_size header).'))
            while True:
                nonce = f_in.read(_ENCRYPT_NONCE_LEN)
                if not nonce:
                    break  # clean EOF
                if len(nonce) < _ENCRYPT_NONCE_LEN:
                    raise UserError(_('Corrupted encrypted file (truncated nonce).'))
                ct_len_bytes = f_in.read(4)
                if len(ct_len_bytes) < 4:
                    raise UserError(_('Corrupted encrypted file (truncated ct_len).'))
                ct_len = int.from_bytes(ct_len_bytes, 'little')
                ct = f_in.read(ct_len)
                if len(ct) < ct_len:
                    raise UserError(_('Corrupted encrypted file (truncated chunk).'))
                pt = aes.decrypt(nonce, ct, None)
                f_out.write(pt)

        os.remove(path)
        return out_path

    # ------------------------------------------------------------------ #
    #  Restore helper
    # ------------------------------------------------------------------ #

    def _restore_db(self, path: str) -> None:
        """Call Odoo's restore_db with the given dump file path."""
        from odoo.service.db import restore_db
        with open(path, 'rb') as f:
            restore_db(self.target_db, f, copy=self.copy_uuid)

    # ------------------------------------------------------------------ #
    #  Notification
    # ------------------------------------------------------------------ #

    def _notify(self) -> None:
        backup = self.backup_log_id.backup_id
        partner_ids = list(backup.notify_partner_ids.ids)
        if (
            self.triggered_by_uid
            and self.triggered_by_uid.partner_id
            and self.triggered_by_uid.partner_id.id not in partner_ids
        ):
            partner_ids = partner_ids + [self.triggered_by_uid.partner_id.id]
        if not partner_ids:
            return

        if self.state == 'done':
            subject = f'[RESTORED] {backup.name} → {self.target_db}'
            body = (
                f'<p>Restore from backup <b>{backup.name}</b> '
                f'(log {self.backup_log_id.id}) to database '
                f'<b>{self.target_db}</b> completed successfully.</p>'
                f'<p>Triggered by: {self.triggered_by_uid.name}</p>'
            )
        elif self.state == 'failed':
            subject = f'[RESTORE FAILED] {backup.name} → {self.target_db}'
            body = (
                f'<p>Restore failed: {self.result_message or "Unknown error"}</p>'
                f'<p>Triggered by: {self.triggered_by_uid.name}</p>'
            )
        else:
            return

        backup.message_post(
            body=body,
            subject=subject,
            partner_ids=partner_ids,
            message_type='comment',
            subtype_xmlid='mail.mt_note',
        )

    # ------------------------------------------------------------------ #
    #  Cleanup cron
    # ------------------------------------------------------------------ #

    @api.model
    def _cleanup_draft_orphans(self) -> int:
        """Weekly cron: delete draft restore jobs older than 7 days."""
        cutoff = fields.Datetime.now() - timedelta(days=7)
        old = self.search([('state', '=', 'draft'), ('create_date', '<', cutoff)])
        count = len(old)
        old.unlink()
        _logger.info('Cleaned up %d stale draft restore jobs', count)
        return count
