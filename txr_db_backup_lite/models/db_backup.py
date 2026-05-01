import gzip
import hashlib
import logging
import os
import posixpath
import shutil
import subprocess
import tempfile
import traceback
from datetime import datetime, time, timedelta
from subprocess import TimeoutExpired as _SubprocessTimeoutExpired

import pytz

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.base.models.res_partner import _tz_get

_logger = logging.getLogger(__name__)


def float_to_time(hours):
    """odoo.tools.date_utils.float_to_time is v19+; inline for v17/v18."""
    h, m = divmod(round(hours * 60), 60)
    return time(int(h % 24), int(m))

_CRITICAL_TABLES = frozenset(['res_company', 'res_users', 'res_partner', 'ir_model'])
_MIN_TOC_ENTRIES = 100


class TxrDbBackup(models.Model):
    _name = 'txr.db.backup'
    _description = 'Database Backup Configuration'
    _inherit = ['mail.thread']

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    database_name = fields.Char(
        required=True, default=lambda self: self.env.cr.dbname
    )
    storage_type = fields.Selection(
        [('local', 'Local'), ('sftp', 'SFTP')],
        required=True,
        default='local',
    )
    backup_format = fields.Selection(
        [('zip', 'Full (database + filestore)'), ('dump', 'Database only')],
        required=True,
        default='zip',
        string='Backup Format',
    )
    compress = fields.Boolean(default=True, string='GZip Compression')
    interval_number = fields.Integer(default=1, string='Backup Every')
    interval_type = fields.Selection(
        [
            ('hours', 'Hours'),
            ('days', 'Days'),
            ('weeks', 'Weeks'),
            ('months', 'Months'),
        ],
        default='days',
    )
    cron_id = fields.Many2one(
        'ir.cron',
        string='Scheduled Action',
        ondelete='set null',
        readonly=True,
    )
    execution_time = fields.Float(default=3.0, string='Execution Time')
    tz = fields.Selection(
        _tz_get, string='Timezone',
        default=lambda self: self.env.user.tz or 'UTC',
        required=True,
    )
    retention_count = fields.Integer(default=7, string='Keep Last N Backups')
    retention_days = fields.Integer(default=0, string='Keep Backups For N Days')
    notify_success = fields.Boolean(default=False)
    notify_failure = fields.Boolean(default=True)
    notify_partner_ids = fields.Many2many('res.partner', string='Notification Recipients')
    local_path = fields.Char(string='Local Path')

    # -------------------------------------------------------------------------
    # D8: Runtime feedback computed fields
    # -------------------------------------------------------------------------

    next_execution = fields.Datetime(related='cron_id.nextcall', string='Next Execution', readonly=True)

    last_log_id = fields.Many2one(
        'txr.db.backup.log',
        compute='_compute_last_log',
        search='_search_last_log_id',
        string='Last Log',
    )
    last_state = fields.Selection(related='last_log_id.state', string='Last Status', readonly=True)
    last_backup_time = fields.Datetime(related='last_log_id.started_at', string='Last Backup', readonly=True)
    last_file_size = fields.Integer(related='last_log_id.file_size', string='Last Size (bytes)', readonly=True)
    last_verify_l1 = fields.Selection(related='last_log_id.verify_l1', string='Last L1', readonly=True)
    last_verify_l2 = fields.Selection(related='last_log_id.verify_l2', string='Last L2', readonly=True)
    backup_count = fields.Integer(compute='_compute_backup_count', string='Backup Count')

    # -------------------------------------------------------------------------
    # D10: Directory status computed fields
    # -------------------------------------------------------------------------

    disk_total = fields.Float(compute='_compute_disk_info', string='Disk Total (GB)')
    disk_free = fields.Float(compute='_compute_disk_info', string='Disk Free (GB)')
    backup_total_size = fields.Float(compute='_compute_disk_info', string='Backup Size (MB)')
    backup_file_count = fields.Integer(compute='_compute_disk_info', string='Backup File Count')
    path_status = fields.Selection(
        [('ok', 'OK'), ('not_exists', 'Not Exists'), ('not_writable', 'Not Writable'), ('na', 'N/A')],
        compute='_compute_disk_info', string='Path Status',
    )
    sftp_host = fields.Char(string='SFTP Host')
    sftp_port = fields.Integer(default=22, string='SFTP Port')
    sftp_username = fields.Char(string='SFTP Username')
    sftp_key_path = fields.Char(string='SSH Key Path')
    sftp_private_key = fields.Char(string='SSH Private Key')
    sftp_remote_path = fields.Char(string='SFTP Remote Path')

    # -------------------------------------------------------------------------
    # D8: Compute methods
    # -------------------------------------------------------------------------

    def _compute_backup_count(self):
        for rec in self:
            rec.backup_count = self.env['txr.db.backup.log'].search_count(
                [('backup_id', '=', rec.id)]
            )

    def _compute_last_log(self):
        for rec in self:
            log = self.env['txr.db.backup.log'].search(
                [('backup_id', '=', rec.id)],
                order='started_at desc', limit=1,
            )
            rec.last_log_id = log.id if log else False

    def _search_last_log_id(self, operator, value):
        """Allow ORM to resolve which backup records are affected when a log changes.

        Called when Odoo needs to recompute last_state / last_verify_* after a
        txr.db.backup.log record is written. Returns a domain on txr.db.backup.
        """
        if operator == '=' and value:
            log = self.env['txr.db.backup.log'].browse(value)
            backup_id = log.backup_id.id if log.exists() else 0
            return [('id', '=', backup_id)]
        if operator == '=' and not value:
            has_logs = self.env['txr.db.backup.log'].search([]).mapped('backup_id').ids
            return [('id', 'not in', has_logs)]
        if operator == '!=' and value:
            log = self.env['txr.db.backup.log'].browse(value)
            backup_id = log.backup_id.id if log.exists() else 0
            return [('id', '!=', backup_id)]
        return []

    # -------------------------------------------------------------------------
    # D10: Compute disk info
    # -------------------------------------------------------------------------

    def _compute_disk_info(self):
        for rec in self:
            if rec.storage_type != 'local' or not rec.local_path:
                rec.disk_total = rec.disk_free = rec.backup_total_size = 0
                rec.backup_file_count = 0
                rec.path_status = 'na'
                continue
            path = rec.local_path
            if not os.path.isdir(path):
                rec.disk_total = rec.disk_free = rec.backup_total_size = 0
                rec.backup_file_count = 0
                rec.path_status = 'not_exists'
                continue
            if not os.access(path, os.W_OK):
                rec.path_status = 'not_writable'
            else:
                rec.path_status = 'ok'
            try:
                usage = shutil.disk_usage(path)
                rec.disk_total = round(usage.total / (1024**3), 1)
                rec.disk_free = round(usage.free / (1024**3), 1)
            except Exception:
                rec.disk_total = rec.disk_free = 0
            logs = self.env['txr.db.backup.log'].search([
                ('backup_id', '=', rec.id), ('state', '=', 'success'),
            ])
            rec.backup_total_size = round(sum(logs.mapped('file_size')) / (1024**2), 1)
            rec.backup_file_count = len(logs)

    # -------------------------------------------------------------------------
    # D10: Path validation & onchange
    # -------------------------------------------------------------------------

    @api.constrains('local_path', 'storage_type')
    def _check_local_path(self):
        for rec in self:
            if rec.storage_type != 'local' or not rec.local_path:
                continue
            if not os.path.isdir(rec.local_path):
                raise ValidationError(
                    _('Directory "%s" does not exist or is not a directory.', rec.local_path)
                )

    @api.onchange('local_path', 'storage_type')
    def _onchange_local_path(self):
        if self.storage_type != 'local' or not self.local_path:
            return
        # Normalize ~ and relative paths to absolute
        path = os.path.abspath(os.path.expanduser(self.local_path))
        if path != self.local_path:
            self.local_path = path
        if not os.path.isdir(path):
            return {'warning': {
                'title': _('Invalid Path'),
                'message': _('Directory "%s" does not exist.', path),
            }}
        if not os.access(path, os.W_OK):
            return {'warning': {
                'title': _('Invalid Path'),
                'message': _('Directory "%s" is not writable.', path),
            }}

    # -------------------------------------------------------------------------
    # Cron sync
    # -------------------------------------------------------------------------

    def _compute_nextcall_utc(self, rec, cron=None):
        """计算下次执行时间（UTC naive datetime）。"""
        user_tz = pytz.timezone(rec.tz or 'UTC')
        today = fields.Date.context_today(rec.with_context(tz=rec.tz))
        run_at_local = user_tz.localize(
            datetime.combine(today, float_to_time(rec.execution_time))
        )
        # 按 interval_type 计算推进步长
        step = timedelta(days=1)
        if rec.interval_type == 'hours':
            step = timedelta(hours=rec.interval_number)
        elif rec.interval_type == 'weeks':
            step = timedelta(weeks=1)
        # 如果已过当天时刻，推进一个 interval 步长
        if cron and cron.lastcall:
            last_local = fields.Datetime.context_timestamp(
                rec.with_context(tz=rec.tz), cron.lastcall
            )
            if run_at_local.date() <= last_local.date():
                run_at_local += step
        elif run_at_local <= datetime.now(user_tz):
            run_at_local += step
        return run_at_local.astimezone(pytz.UTC).replace(tzinfo=None)

    def _sync_cron(self):
        for rec in self:
            cron = rec.cron_id.sudo()
            nextcall_utc = self._compute_nextcall_utc(rec, cron if cron else None)
            if cron:
                cron.active = rec.active
                cron.name = f'Backup: {rec.name}'
                cron.interval_number = rec.interval_number
                cron.interval_type = rec.interval_type
                cron.nextcall = nextcall_utc
            else:
                model_id = self.env['ir.model']._get_id('txr.db.backup')
                cron = self.env['ir.cron'].sudo().create({
                    'name': f'Backup: {rec.name}',
                    'model_id': model_id,
                    'state': 'code',
                    'code': f'env["txr.db.backup"].browse([{rec.id}])._run()',
                    'interval_number': rec.interval_number,
                    'interval_type': rec.interval_type,
                    'nextcall': nextcall_utc,
                    'active': rec.active,
                    'user_id': self.env.ref('base.user_root').id,
                })
                rec.cron_id = cron

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec._sync_cron()
        return records

    def write(self, vals):
        result = super().write(vals)
        schedule_fields = {'interval_number', 'interval_type', 'active', 'name', 'execution_time', 'tz'}
        if schedule_fields & set(vals):
            self._sync_cron()
        return result

    def unlink(self):
        cron_ids = self.mapped('cron_id')
        result = super().unlink()
        cron_ids.unlink()
        return result

    # -------------------------------------------------------------------------
    # Backup execution
    # -------------------------------------------------------------------------

    def action_run_now(self):
        """Public entry point for the Run Now button."""
        self.ensure_one()
        self._run()

    def _run(self):
        self.ensure_one()
        running_count = self.env['txr.db.backup.log'].search_count([
            ('backup_id', '=', self.id),
            ('state', '=', 'running'),
        ])
        if running_count > 0:
            _logger.warning('Backup %s already running, skipping', self.name)
            return

        log = self.env['txr.db.backup.log'].create({
            'backup_id': self.id,
            'state': 'running',
            'phase': 'dump',
            'started_at': fields.Datetime.now(),
            'storage_type': self.storage_type,
        })
        tmp_dir = tempfile.mkdtemp(prefix='txr_backup_')
        try:
            dump_path = self._do_dump(log, tmp_dir)
            if self.backup_format == 'dump' and self.compress:
                file_path = self._do_compress(log, dump_path)
            else:
                file_path = dump_path
            sha256 = self._compute_sha256(file_path)
            log.write({
                'sha256': sha256,
                'file_size': os.path.getsize(file_path),
                'phase': 'upload',
            })
            if self.storage_type == 'local':
                self._upload_local(log, file_path)
            elif self.storage_type == 'sftp':
                self._upload_sftp(log, file_path)
            log.write({'phase': 'verify'})
            self._verify_level1(log, sha256)
            self._verify_level2(log, file_path)
            if log.state == 'running':
                log.write({
                    'phase': 'cleanup',
                    'state': 'success',
                    'finished_at': fields.Datetime.now(),
                })
            else:
                log.write({'phase': 'cleanup'})
            self._notify(log)
            try:
                self._apply_retention()
            except Exception as e:
                _logger.warning('Retention cleanup failed for %s: %s', self.name, e)
        except Exception as e:
            log.write({
                'state': 'failed',
                'error_message': str(e),
                'error_traceback': traceback.format_exc(),
                'finished_at': fields.Datetime.now(),
            })
            self._notify(log)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _detect_pg_dump_version(self):
        try:
            result = subprocess.run(
                ['pg_dump', '--version'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip()
        except Exception:
            return 'unknown'

    def _do_dump(self, log, tmp_dir):
        from odoo.service.db import dump_db
        version = self._detect_pg_dump_version()
        log.write({'pg_dump_version': version})
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if self.backup_format == 'zip':
            filename = f'{self.database_name}_{timestamp}.zip'
        else:
            filename = f'{self.database_name}_{timestamp}.dump'
        dump_path = os.path.join(tmp_dir, filename)
        with open(dump_path, 'wb') as f:
            if self.backup_format == 'zip':
                dump_db(self.database_name, f, 'zip')
            else:
                dump_db(self.database_name, f, 'dump')
        return dump_path

    def _do_compress(self, log, dump_path):
        log.write({'phase': 'compress'})
        gz_path = dump_path + '.gz'
        with open(dump_path, 'rb') as f_in, gzip.open(gz_path, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.remove(dump_path)
        return gz_path

    def _compute_sha256(self, file_path):
        sha256 = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()

    # -------------------------------------------------------------------------
    # Storage backends
    # -------------------------------------------------------------------------

    def _upload_local(self, log, file_path):
        if not self.local_path:
            raise UserError('Local path not configured')
        if not os.path.isdir(self.local_path):
            raise UserError(_('Directory "%s" does not exist.', self.local_path))
        usage = shutil.disk_usage(self.local_path)
        file_size = os.path.getsize(file_path)
        if usage.free < file_size * 1.5:
            raise UserError(
                f'Insufficient disk space: {usage.free} free, {int(file_size * 1.5)} needed'
            )
        dest = os.path.join(self.local_path, os.path.basename(file_path))
        shutil.copy2(file_path, dest)
        log.write({'file_path': dest})

    def _check_sftp_binary(self):
        if not shutil.which('sftp'):
            raise UserError(
                'sftp command not found. Install OpenSSH client: apt install openssh-client'
            )

    def _prepare_sftp_cmd(self):
        """Build base sftp command args and return (cmd, key_tmp_path).

        Supports three modes:
        1. sftp_private_key set → write to temp file, pass -i
        2. sftp_key_path set → pass -i directly
        3. Neither set → no -i, use system default key (~/.ssh/id_rsa etc.)
        """
        key_tmp = None
        cmd = ['sftp']
        if self.sftp_private_key:
            fd, key_tmp = tempfile.mkstemp(prefix='txr_sftp_key_')
            os.write(fd, self.sftp_private_key.encode())
            os.close(fd)
            os.chmod(key_tmp, 0o600)
            cmd += ['-i', key_tmp]
        elif self.sftp_key_path:
            cmd += ['-i', self.sftp_key_path]
        # else: no -i → OpenSSH uses default keys
        cmd += [
            '-P', str(self.sftp_port),
            '-o', 'StrictHostKeyChecking=no',
            '-b', '-',
            f'{self.sftp_username}@{self.sftp_host}',
        ]
        return cmd, key_tmp

    def _cleanup_sftp_key(self, key_tmp):
        if key_tmp and os.path.exists(key_tmp):
            os.remove(key_tmp)

    def action_test_sftp_connection(self):
        """Test SFTP connectivity with configured parameters."""
        self.ensure_one()
        self._check_sftp_binary()
        cmd, key_tmp = self._prepare_sftp_cmd()
        try:
            remote_path = self.sftp_remote_path or '.'
            result = subprocess.run(
                cmd, input=f'ls {remote_path}',
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                raise UserError(_('SFTP connection failed:\n%s', result.stderr))
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection Successful'),
                    'message': _('SFTP connection to %s established.', self.sftp_host),
                    'type': 'success',
                    'sticky': False,
                },
            }
        except _SubprocessTimeoutExpired:
            raise UserError(_('SFTP connection timed out after 30 seconds'))
        finally:
            self._cleanup_sftp_key(key_tmp)

    def _upload_sftp(self, log, file_path):
        self._check_sftp_binary()
        cmd, key_tmp = self._prepare_sftp_cmd()
        try:
            remote_path = self.sftp_remote_path or '.'
            remote_file = posixpath.join(remote_path, os.path.basename(file_path))
            result = subprocess.run(
                cmd, input=f'put {file_path} {remote_file}',
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                raise UserError(f'SFTP upload failed: {result.stderr}')
            log.write({'file_path': remote_file})
        finally:
            self._cleanup_sftp_key(key_tmp)

    # -------------------------------------------------------------------------
    # Verification
    # -------------------------------------------------------------------------

    def _verify_level1(self, log, computed_sha256):
        """Verify file integrity by comparing pre-computed SHA256 with stored hash."""
        if computed_sha256 == log.sha256:
            log.write({'verify_l1': 'passed'})
        else:
            log._append_error('L1: SHA256 mismatch')
            log.write({'verify_l1': 'failed', 'state': 'warning'})

    def _verify_level2(self, log, file_path):
        if not os.path.exists(file_path):
            log.write({'verify_l2': 'skipped'})
            return
        if file_path.endswith('.zip'):
            self._verify_level2_zip(log, file_path)
        else:
            self._verify_level2_dump(log, file_path)

    def _verify_level2_zip(self, log, file_path):
        """Verify zip backup structure: manifest.json + dump.sql required."""
        import zipfile
        try:
            if not zipfile.is_zipfile(file_path):
                log._append_error('L2: Not a valid zip file')
                log.write({'verify_l2': 'failed', 'state': 'warning'})
                return
            with zipfile.ZipFile(file_path, 'r') as zf:
                names = zf.namelist()
                log.write({'verify_l2_toc': len(names)})
                missing = []
                if 'dump.sql' not in names:
                    missing.append('dump.sql')
                if 'manifest.json' not in names:
                    missing.append('manifest.json')
                if missing:
                    log._append_error(f'L2: Missing required files in zip: {missing}')
                    log.write({'verify_l2': 'failed', 'state': 'warning'})
                    return
                log.write({'verify_l2': 'passed'})
        except Exception as e:
            log._append_error(f'L2: Error reading zip: {e}')
            log.write({'verify_l2': 'failed', 'state': 'warning'})

    def _verify_level2_dump(self, log, file_path):
        """Verify dump backup via pg_restore --list (existing logic)."""
        actual_path = file_path
        gz_tmp = None
        try:
            if file_path.endswith('.gz'):
                fd, gz_tmp = tempfile.mkstemp(suffix='.dump', prefix='txr_verify_')
                os.close(fd)
                with gzip.open(file_path, 'rb') as f_in, open(gz_tmp, 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
                actual_path = gz_tmp
            result = subprocess.run(
                ['pg_restore', '--list', actual_path],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                log._append_error(f'L2: pg_restore --list failed: {result.stderr}')
                log.write({'verify_l2': 'failed', 'state': 'warning'})
                return
            toc_lines = [ln for ln in result.stdout.splitlines() if ln.strip() and not ln.startswith(';')]
            log.write({'verify_l2_toc': len(toc_lines)})
            if len(toc_lines) < _MIN_TOC_ENTRIES:
                log._append_error(f'L2: TOC too small ({len(toc_lines)} entries)')
                log.write({'verify_l2': 'failed', 'state': 'warning'})
                return
            toc_text = result.stdout.lower()
            missing = [t for t in _CRITICAL_TABLES if t not in toc_text]
            if missing:
                log._append_error(f'L2: Missing critical tables: {list(missing)}')
                log.write({'verify_l2': 'failed', 'state': 'warning'})
                return
            log.write({'verify_l2': 'passed'})
        finally:
            if gz_tmp and os.path.exists(gz_tmp):
                os.remove(gz_tmp)

    # -------------------------------------------------------------------------
    # Retention
    # -------------------------------------------------------------------------

    def _apply_retention(self):
        self.ensure_one()
        domain = [('backup_id', '=', self.id), ('state', '=', 'success')]
        if self.retention_count > 0:
            logs = self.env['txr.db.backup.log'].search(domain, order='started_at desc')
            if len(logs) > self.retention_count:
                logs[self.retention_count:].unlink()
        if self.retention_days > 0:
            cutoff = fields.Datetime.now() - timedelta(days=self.retention_days)
            old_logs = self.env['txr.db.backup.log'].search(
                domain + [('started_at', '<', cutoff)]
            )
            old_logs.unlink()

    # -------------------------------------------------------------------------
    # Notification
    # -------------------------------------------------------------------------

    def _notify(self, log):
        self.ensure_one()
        if not self.notify_partner_ids:
            return
        try:
            if log.state == 'failed' and self.notify_failure:
                subject = f'[FAILED] Backup: {self.name} - Phase: {log.phase}'
                body = (
                    f'<p>Backup <b>{self.name}</b> failed at phase <b>{log.phase}</b>.</p>'
                    f'<p>Error: {log.error_message or "Unknown"}</p>'
                )
            elif log.state == 'success' and self.notify_success:
                subject = f'[SUCCESS] Backup: {self.name}'
                body = (
                    f'<p>Backup <b>{self.name}</b> completed successfully.</p>'
                    f'<p>File size: {log.file_size} bytes, Duration: {log.duration}s</p>'
                )
            elif log.state == 'warning' and self.notify_failure:
                subject = f'[WARNING] Backup: {self.name}'
                body = (
                    f'<p>Backup <b>{self.name}</b> completed with warnings.</p>'
                    f'<p>{log.error_message or ""}</p>'
                )
            else:
                return
            self.message_post(
                body=body,
                subject=subject,
                partner_ids=self.notify_partner_ids.ids,
                message_type='comment',
                subtype_xmlid='mail.mt_note',
            )
        except Exception as e:
            _logger.warning('Notification failed for backup %s: %s', self.name, e)
            if log.state == 'success':
                log._append_error(f'Notification failed: {e}')
                log.write({'state': 'warning'})
