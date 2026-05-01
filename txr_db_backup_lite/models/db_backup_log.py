import os

from odoo import api, fields, models

VERIFICATION_STATES = [
    ('pending', 'Pending'),
    ('passed', 'Passed'),
    ('failed', 'Failed'),
    ('skipped', 'Skipped'),
]


class TxrDbBackupLog(models.Model):
    _name = 'txr.db.backup.log'
    _description = 'Database Backup Log'
    _order = 'started_at desc'

    backup_id = fields.Many2one(
        'txr.db.backup', required=True, ondelete='cascade', string='Backup Config'
    )
    state = fields.Selection(
        [
            ('pending', 'Pending'),
            ('running', 'Running'),
            ('success', 'Success'),
            ('warning', 'Warning'),
            ('failed', 'Failed'),
        ],
        default='pending',
        required=True,
    )
    phase = fields.Selection(
        [
            ('dump', 'Dump'),
            ('compress', 'Compress'),
            ('upload', 'Upload'),
            ('verify', 'Verify'),
            ('cleanup', 'Cleanup'),
            ('notify', 'Notify'),
        ],
        string='Current Phase',
    )
    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    duration = fields.Float(
        compute='_compute_duration', store=True, string='Duration (s)'
    )
    file_size = fields.Integer(string='File Size (bytes)')
    sha256 = fields.Char(string='SHA256 Hash')
    pg_dump_version = fields.Char()
    storage_type = fields.Selection(
        [('local', 'Local'), ('sftp', 'SFTP')], string='Storage Type (snapshot)'
    )
    verify_l1 = fields.Selection(
        VERIFICATION_STATES, default='pending', string='L1: File Integrity'
    )
    verify_l2 = fields.Selection(
        VERIFICATION_STATES, default='pending', string='L2: Format Validity'
    )
    verify_l2_toc = fields.Integer(string='TOC Entries')
    verify_l3 = fields.Selection(
        VERIFICATION_STATES, default='skipped', string='L3: Full Restore (Pro)'
    )
    verify_l3_note = fields.Char(default='Requires Pro version')
    error_message = fields.Text()
    error_traceback = fields.Text()
    file_path = fields.Char(string='Backup File Path')
    restore_instructions = fields.Html(
        compute='_compute_restore_instructions',
        string='Restore Instructions',
    )

    # -------------------------------------------------------------------------
    # D8: File existence check
    # -------------------------------------------------------------------------

    file_exists = fields.Selection(
        [('yes', 'Yes'), ('no', 'No'), ('unknown', 'Unknown')],
        compute='_compute_file_exists', string='File Exists',
    )

    def _compute_file_exists(self):
        for rec in self:
            if rec.state != 'success' or not rec.file_path:
                rec.file_exists = 'unknown'
            elif rec.backup_id and rec.backup_id.storage_type == 'sftp':
                rec.file_exists = 'unknown'
            elif os.path.exists(rec.file_path):
                rec.file_exists = 'yes'
            else:
                rec.file_exists = 'no'

    def _append_error(self, message):
        """Append a line to error_message without clobbering existing content."""
        current = self.error_message or ''
        self.error_message = f'{current}\n{message}' if current else message

    def _compute_display_name(self):
        for rec in self:
            name = rec.backup_id.name or ''
            ts = rec.started_at.strftime('%Y-%m-%d %H:%M') if rec.started_at else ''
            rec.display_name = f'{name} [{ts}] ({rec.state})'

    @api.depends('started_at', 'finished_at')
    def _compute_duration(self):
        for rec in self:
            if rec.started_at and rec.finished_at:
                rec.duration = (rec.finished_at - rec.started_at).total_seconds()
            else:
                rec.duration = 0.0

    def _compute_restore_instructions(self):
        for rec in self:
            if rec.state != 'success' or not rec.file_path:
                rec.restore_instructions = False
                continue
            fp = rec.file_path
            db = rec.backup_id.database_name or 'mydb'
            lines = []
            # SFTP: need to download first
            if rec.backup_id and rec.backup_id.storage_type == 'sftp':
                host = rec.backup_id.sftp_host or 'server'
                user = rec.backup_id.sftp_username or 'user'
                port = rec.backup_id.sftp_port or 22
                lines.append('<p><b>1. Download from SFTP:</b></p>')
                lines.append(f'<pre>sftp -P {port} {user}@{host}:{fp} .</pre>')
                fp = os.path.basename(fp)

            if fp.endswith('.zip'):
                lines.append('<p><b>Restore with Odoo CLI (recommended):</b></p>')
                lines.append(f'<pre>odoo-bin db load {db}_restored {fp}</pre>')
                lines.append('<p>Or overwrite existing:</p>')
                lines.append(f'<pre>odoo-bin db load {db} {fp} -f</pre>')
                lines.append('<p>Or use the Odoo Database Manager at <code>/web/database/manager</code></p>')
            else:
                if fp.endswith('.gz'):
                    lines.append('<p><b>Decompress first:</b></p>')
                    lines.append(f'<pre>gunzip {fp}</pre>')
                    fp = fp[:-3]  # remove .gz
                lines.append('<p><b>Restore with Odoo CLI (recommended):</b></p>')
                lines.append(f'<pre>odoo-bin db load {db}_restored {fp}</pre>')
                lines.append('<p>Or with pg_restore:</p>')
                lines.append(f'<pre>createdb {db}_restored\npg_restore -d {db}_restored {fp}</pre>')
                lines.append(
                    '<p class="text-warning">&#9888; This backup contains the database only (no filestore). '
                    'Restore your filestore separately if needed.</p>'
                )

            lines.append('<p class="text-muted">Need one-click restore? Upgrade to Pro.</p>')
            rec.restore_instructions = '\n'.join(lines)
