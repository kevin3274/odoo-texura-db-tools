from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .db_restore_job import TxrDbRestoreJob


class TxrDbBackupLog(models.Model):
    _inherit = 'txr.db.backup.log'

    # Extend the phase Selection with Pro-only phases
    phase = fields.Selection(
        selection_add=[('filestore_sync', 'Filestore Sync')],
        ondelete={'filestore_sync': 'set null'},
    )

    encrypted = fields.Boolean(
        default=False,
        string='Encrypted',
        help='Whether this backup file was encrypted with AES-256-GCM.',
    )

    filestore_files_synced = fields.Integer(string='Filestore Files Synced')
    filestore_bytes_synced = fields.Integer(string='Filestore Bytes Synced')
    filestore_sync_duration = fields.Float(string='Filestore Sync Duration (s)')

    backup_strategy_snapshot = fields.Char(
        readonly=True,
        string='Backup Strategy (snapshot)',
        help='Value of backup_strategy at the time this backup ran.',
    )

    preflight_info = fields.Text(
        readonly=True,
        string='Pre-flight Info',
        help='Summary written at _run() entry: DB size, filestore size, disk, strategy.',
    )

    l3_preflight_info = fields.Text(
        readonly=True,
        string='L3 Pre-flight Info',
        help='L3 scheduling preflight: PG data dir space, tmp dir space, priority mode.',
    )

    restore_job_ids = fields.One2many(
        'txr.db.restore.job',
        'backup_log_id',
        string='Restore Jobs',
    )
    restore_job_count = fields.Integer(
        compute='_compute_restore_job_count',
        string='Restore Count',
    )
    has_active_restore_job = fields.Boolean(
        compute='_compute_has_active_restore_job',
        string='Has Active Restore Job',
    )

    @api.depends('restore_job_ids')
    def _compute_restore_job_count(self):
        for rec in self:
            rec.restore_job_count = len(rec.restore_job_ids)

    @api.depends('restore_job_ids.state')
    def _compute_has_active_restore_job(self):
        for rec in self:
            rec.has_active_restore_job = any(
                j.state in TxrDbRestoreJob.ACTIVE_STATES for j in rec.restore_job_ids
            )

    def action_start_restore(self):
        """Open restore wizard for this successful backup log."""
        self.ensure_one()
        if self.state != 'success':
            raise UserError(_('Can only restore from a successful backup.'))
        if self.has_active_restore_job:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Restore in Progress'),
                    'message': _(
                        'Another restore from this backup is still running. '
                        'Please wait or cancel it first.'
                    ),
                    'type': 'warning',
                    'sticky': True,
                },
            }
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': self.id,
            'state': 'draft',
            'triggered_by_uid': self.env.uid,
        })
        return {
            'name': _('Restore Database'),
            'type': 'ir.actions.act_window',
            'res_model': 'txr.db.restore.job',
            'res_id': job.id,
            'view_mode': 'form',
            'target': 'new',
        }
