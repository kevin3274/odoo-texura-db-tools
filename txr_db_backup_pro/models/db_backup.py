"""Pro extensions to txr.db.backup.

Adds: cloud (rclone) storage, AES-256-GCM encryption, L3 verification
(with preflight, scheduling window, low-priority resource use), Split and
Streaming strategies for large databases, and recommendation/sizing helpers.
"""
import base64
import gzip
import logging
import os
import posixpath
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import traceback
import zipfile
from datetime import datetime, timedelta

import psycopg2
import psycopg2.sql
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config

_logger = logging.getLogger(__name__)

# Encryption file format (Standard / Split landed file & Streaming pipe):
#   [4B little-endian chunk_size]
#   repeat: [12B nonce][4B ciphertext_len][N B ciphertext]
_ENCRYPT_CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB
_ENCRYPT_NONCE_LEN = 12

_DEFAULT_L3_TIMEOUT_MIN = 30
_DEFAULT_STREAMING_TIMEOUT_MIN = 120

# Strategy recommendation thresholds (D11)
_GB = 1024 ** 3
_FILESTORE_LARGE_THRESHOLD = 5 * _GB

# Filestore size estimation deadline (s) before giving up
_FILESTORE_ESTIMATE_DEADLINE = 5.0

# L3 low-priority wrappers: check once at import time (constant per host)
_HAS_IONICE = bool(shutil.which('ionice'))
_HAS_NICE = bool(shutil.which('nice'))


class TxrDbBackup(models.Model):
    _inherit = 'txr.db.backup'

    # ------------------------------------------------------------------ #
    #  子段 A: Pro 基础字段（cloud / encryption / L3 config）
    # ------------------------------------------------------------------ #

    storage_type = fields.Selection(
        selection_add=[('rclone', 'Cloud (rclone)')],
        ondelete={'rclone': 'set default'},
    )

    cloud_remote_id = fields.Many2one(
        'txr.db.backup.cloud',
        string='Cloud Remote',
        help='rclone remote used when storage_type = Cloud (rclone).',
    )
    cloud_path = fields.Char(
        string='Cloud Path',
        help='Path prefix inside the remote (e.g. backups/odoo).',
    )

    encrypt_backup = fields.Boolean(
        default=False,
        string='Encrypt Backup',
        help='Encrypt the backup file with AES-256-GCM before upload.',
    )
    encrypt_key = fields.Char(
        string='Encryption Key (base64, 32 bytes)',
        groups='base.group_system',
        help='AES-256 key, base64 encoded. Use Generate Key to create one.',
    )

    # L3 verification configuration
    verify_l3_enabled = fields.Boolean(
        default=False,
        string='L3: Full Restore Verification',
    )
    verify_l3_frequency = fields.Selection(
        [('every', 'Every backup'), ('weekly', 'Weekly'), ('monthly', 'Monthly')],
        default='weekly',
        string='L3 Frequency',
    )
    verify_l3_last_run = fields.Datetime(
        readonly=True,
        string='L3 Last Run',
    )
    verify_l3_window_enabled = fields.Boolean(
        default=False,
        string='L3 Maintenance Window',
        help='Restrict L3 to a daily time window (e.g. nightly only).',
    )
    verify_l3_window_start = fields.Float(
        default=2.0,
        string='Window Start (hour)',
    )
    verify_l3_window_end = fields.Float(
        default=4.0,
        string='Window End (hour)',
    )
    verify_l3_low_priority = fields.Boolean(
        default=True,
        string='L3 Low Priority',
        help='Wrap L3 subprocesses with ionice/nice and lower PG work_mem.',
    )

    # ------------------------------------------------------------------ #
    #  子段 D: Split / Streaming strategy fields
    # ------------------------------------------------------------------ #

    backup_strategy = fields.Selection(
        [
            ('standard', 'Standard (DB+Filestore zip)'),
            ('split', 'Split (DB dump + Filestore sync)'),
            ('streaming', 'Streaming (DB pipe, no temp file)'),
        ],
        default='standard',
        required=True,
        string='Backup Strategy',
    )

    filestore_sync_enabled = fields.Boolean(
        default=False,
        string='Sync Filestore',
        help='In Split mode, sync filestore to cloud via rclone sync.',
    )
    filestore_path = fields.Char(
        string='Filestore Path',
        help='Override the auto-detected filestore path.',
    )
    filestore_cloud_remote_id = fields.Many2one(
        'txr.db.backup.cloud',
        string='Filestore Cloud Remote',
        help='Cloud remote for filestore sync. Falls back to Cloud Remote.',
    )
    filestore_cloud_path = fields.Char(string='Filestore Cloud Path')

    pre_backup_disk_check = fields.Boolean(
        default=True,
        string='Pre-flight Disk Check',
    )
    pre_backup_disk_min_gb = fields.Float(
        default=10.0,
        string='Min Free Disk (GB)',
    )

    # ------------------------------------------------------------------ #
    #  子段 E: Instance size & recommendation fields
    # ------------------------------------------------------------------ #

    filestore_size_bytes = fields.Integer(
        string='Filestore Size (bytes, cached)',
        help='Cached value updated by Refresh button or after each backup.',
    )
    filestore_size_updated_at = fields.Datetime(
        string='Filestore Size Updated',
    )
    db_size_bytes = fields.Integer(
        compute='_compute_db_size_bytes',
        string='DB Size (bytes)',
        store=False,
    )

    estimated_duration_seconds = fields.Float(
        compute='_compute_estimated_duration',
        string='Estimated Duration (s)',
        store=False,
    )
    estimated_duration_label = fields.Char(
        compute='_compute_estimated_duration',
        string='Estimated Duration',
        store=False,
    )

    recommended_strategy = fields.Char(
        compute='_compute_strategy_recommendation',
        string='Recommended Strategy',
        store=False,
    )
    strategy_recommendation_reason = fields.Char(
        compute='_compute_strategy_recommendation',
        string='Recommendation Reason',
        store=False,
    )

    # ------------------------------------------------------------------ #
    #  子段 A: Encryption key actions
    # ------------------------------------------------------------------ #

    @api.onchange('encrypt_backup')
    def _onchange_encrypt_backup_security_warning(self):
        """Warn once when encrypt_backup is first enabled in the form."""
        if self.encrypt_backup and not self._origin.encrypt_backup:
            return {
                'warning': {
                    'title': _('Security Notice'),
                    'message': _(
                        'The encryption key is stored in this database. If the '
                        'database is compromised, the key and your encrypted backups '
                        'are exposed simultaneously.\n\n'
                        "Use the 'Export Key' button to save a copy offline."
                    ),
                }
            }

    def action_generate_key(self):
        """Generate a fresh 256-bit AES key (base64).

        Called by two view buttons:
        - "Generate Key"    (invisible when key already set, no confirm)
        - "Regenerate Key"  (invisible when no key, view-level confirm dialog)
        Both call this same method; the view controls which appears.
        """
        self.ensure_one()
        self.encrypt_key = base64.b64encode(os.urandom(32)).decode()
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def action_export_key(self):
        """Create a downloadable attachment containing the raw key string."""
        self.ensure_one()
        if not self.encrypt_key:
            raise UserError(_('No encryption key to export.'))
        attachment = self.env['ir.attachment'].sudo().create({
            'name': f'backup_key_{self.name}.txt',
            'datas': base64.b64encode(self.encrypt_key.encode()),
            'res_model': self._name,
            'res_id': self.id,
            'type': 'binary',
            'mimetype': 'text/plain',
        })
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    # ------------------------------------------------------------------ #
    #  子段 A: Encryption helpers
    # ------------------------------------------------------------------ #

    def _decode_encrypt_key(self):
        """Decode the base64 key stored on this record, returning 32 bytes."""
        self.ensure_one()
        if not self.encrypt_key:
            raise UserError(_('Encryption is enabled but no key is set. '
                              'Generate or paste a key first.'))
        try:
            key_bytes = base64.b64decode(self.encrypt_key)
        except Exception as exc:
            raise UserError(_('Encryption key is not valid base64: %s', exc))
        if len(key_bytes) != 32:
            raise UserError(_(
                'Encryption key must decode to 32 bytes (got %d).',
                len(key_bytes),
            ))
        return key_bytes

    def _aes_encrypt_stream(self, source, dest, log=None, deadline=None, procs=()):
        """Encrypt source -> dest in 16MB AES-GCM chunks, common to file/pipe.

        Writes the 4-byte chunk-size header then a sequence of
        [nonce | ct_len | ciphertext] chunks. If `deadline` is given, the
        deadline check is invoked between chunks (used by streaming mode).
        Updates `log.phase_detail` with cumulative MB when log is provided.
        """
        aes = AESGCM(self._decode_encrypt_key())
        dest.write(_ENCRYPT_CHUNK_SIZE.to_bytes(4, 'little'))
        total = 0
        while True:
            if deadline is not None:
                self._check_streaming_deadline(deadline, procs)
            plaintext = source.read(_ENCRYPT_CHUNK_SIZE)
            if not plaintext:
                break
            nonce = os.urandom(_ENCRYPT_NONCE_LEN)
            ciphertext = aes.encrypt(nonce, plaintext, None)
            dest.write(nonce)
            dest.write(len(ciphertext).to_bytes(4, 'little'))
            dest.write(ciphertext)
            total += len(plaintext)
            if log is not None:
                self._safe_set_phase_detail(
                    log, f'{total // (1024 * 1024)} MB transferred',
                )

    def _encrypt_file(self, file_path):
        """Encrypt file in 16MB AES-GCM chunks. Returns new path (.enc).

        Memory peak is one chunk regardless of file size. Source file is
        removed once the encrypted output is fully written.
        """
        self.ensure_one()
        out_path = file_path + '.enc'
        with open(file_path, 'rb') as f_in, open(out_path, 'wb') as f_out:
            self._aes_encrypt_stream(f_in, f_out)
        os.remove(file_path)
        return out_path

    # ------------------------------------------------------------------ #
    #  子段 A: rclone upload backend
    # ------------------------------------------------------------------ #

    def _upload_rclone(self, log, file_path):
        """Upload file_path to the configured cloud remote using rclone copy."""
        self.ensure_one()
        if not self.cloud_remote_id:
            raise UserError(_('Cloud Remote is not configured.'))
        remote = self.cloud_remote_id
        remote._check_rclone_binary()
        cfg_path = remote._write_tmp_config()
        try:
            cloud_path = self.cloud_path or ''
            dest = f'{remote.name}:{cloud_path}' if cloud_path else f'{remote.name}:'
            result = subprocess.run(
                ['rclone', '--config', cfg_path, 'copy', file_path, dest],
                capture_output=True, text=True, timeout=3600,
            )
            if result.returncode != 0:
                raise UserError(_(
                    'rclone upload failed:\n%s',
                    result.stderr or result.stdout,
                ))
            remote_file = posixpath.join(cloud_path, os.path.basename(file_path))
            log.write({'file_path': remote_file})
        finally:
            remote._cleanup_tmp_config(cfg_path)

    # ------------------------------------------------------------------ #
    #  子段 A: _run() override – routes by strategy, integrates encryption
    # ------------------------------------------------------------------ #

    def _run(self):
        """Pro override: routes by backup_strategy and inserts encryption."""
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
            'backup_strategy_snapshot': self.backup_strategy,
        })

        # Pre-flight summary (always written, never raises)
        self._write_preflight_info(log)

        tmp_dir = tempfile.mkdtemp(prefix='txr_backup_')
        try:
            if self.pre_backup_disk_check:
                self._pre_flight_disk_check(tmp_dir)

            strategy = self.backup_strategy
            if strategy == 'streaming':
                self._run_streaming(log)
            elif strategy == 'split':
                self._run_split(log, tmp_dir)
            else:
                self._run_standard(log, tmp_dir)

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
            except Exception as exc:
                _logger.warning('Retention cleanup failed for %s: %s', self.name, exc)

            # Silent filestore size cache refresh; never fails the backup
            try:
                size = self._estimate_filestore_size()
                if size is not None:
                    self.write({
                        'filestore_size_bytes': size,
                        'filestore_size_updated_at': fields.Datetime.now(),
                    })
            except Exception as exc:
                _logger.warning('Filestore size refresh failed for %s: %s', self.name, exc)

        except Exception as exc:
            log.write({
                'state': 'failed',
                'error_message': str(exc),
                'error_traceback': traceback.format_exc(),
                'finished_at': fields.Datetime.now(),
            })
            self._notify(log)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _run_standard(self, log, tmp_dir):
        """Standard strategy: dump (DB+filestore zip) → finalize landed file."""
        dump_path = self._do_dump(log, tmp_dir)
        compress = self.backup_format == 'dump' and self.compress
        self._finalize_landed_artifact(log, dump_path, compress=compress)

    def _run_split(self, log, tmp_dir):
        """Split strategy: DB-only dump → finalize → filestore rclone sync."""
        from odoo.service.db import dump_db
        log.write({'pg_dump_version': self._detect_pg_dump_version()})
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        dump_path = os.path.join(tmp_dir, f'{self.database_name}_{timestamp}.dump')
        with open(dump_path, 'wb') as f_out:
            dump_db(self.database_name, f_out, 'dump')

        self._finalize_landed_artifact(log, dump_path, compress=self.compress)

        if self.filestore_sync_enabled:
            self._sync_filestore(log)

    def _finalize_landed_artifact(self, log, dump_path, compress):
        """Compress (optional) → encrypt (optional) → sha256 → upload → verify."""
        file_path = self._do_compress(log, dump_path) if compress else dump_path
        if self.encrypt_backup:
            file_path = self._encrypt_file(file_path)
            log.encrypted = True

        sha256 = self._compute_sha256(file_path)
        log.write({
            'sha256': sha256,
            'file_size': os.path.getsize(file_path),
            'phase': 'upload',
        })
        self._dispatch_upload(log, file_path)
        log.write({'phase': 'verify'})
        self._verify_level1(log, sha256)
        self._verify_level2(log, file_path)
        self._maybe_schedule_l3(log, file_path)

    def _run_streaming(self, log):
        """Streaming strategy: pg_dump | encrypt | rclone rcat. No L1/L2/L3."""
        log.write({
            'phase': 'upload',
            'verify_l1': 'skipped',
            'verify_l2': 'skipped',
            'verify_l3': 'skipped',
            'verify_l3_note': 'streaming mode: no local file',
        })
        self._streaming_backup(log)
        if self.encrypt_backup:
            log.encrypted = True

    def _dispatch_upload(self, log, file_path):
        """Route to the right upload backend based on storage_type."""
        st = self.storage_type
        if st == 'local':
            self._upload_local(log, file_path)
        elif st == 'sftp':
            self._upload_sftp(log, file_path)
        elif st == 'rclone':
            self._upload_rclone(log, file_path)
        else:
            raise UserError(_('Unknown storage_type: %s', st))

    # ------------------------------------------------------------------ #
    #  子段 B: L3 verification – scheduling + execution
    # ------------------------------------------------------------------ #

    def _should_run_l3(self):
        """Return True if L3 should run for this backup right now."""
        self.ensure_one()
        if not self.verify_l3_enabled:
            return False
        if self.verify_l3_frequency == 'every':
            return True
        if not self.verify_l3_last_run:
            return True
        delta_days = (fields.Datetime.now() - self.verify_l3_last_run).days
        return delta_days >= (7 if self.verify_l3_frequency == 'weekly' else 30)

    def _maybe_schedule_l3(self, log, file_path):
        """If L3 conditions are met, mark log pending and schedule cron."""
        if not self._should_run_l3():
            return
        log.write({'verify_l3': 'pending', 'verify_l3_note': 'scheduled'})
        self._schedule_l3_cron(log, file_path)

    def _schedule_l3_cron(self, log, file_path):
        """Compute window-aware nextcall and create one-shot ir.cron for L3."""
        self.ensure_one()
        self._l3_preflight(log, file_path)
        now = fields.Datetime.now()
        if not self.verify_l3_window_enabled:
            nextcall = now
        else:
            window_start = self._next_window_start(
                self.verify_l3_window_start,
                self.verify_l3_window_end,
                now,
            )
            nextcall = now if window_start is None else window_start
        self._append_l3_preflight(log, f'L3 scheduled at {nextcall} UTC')
        model_id = self.env['ir.model']._get_id('txr.db.backup')
        self.env['ir.cron'].sudo().create({
            'name': f'L3 verify: {self.name} #{log.id}',
            'model_id': model_id,
            'state': 'code',
            'code': (f'env["txr.db.backup"].browse([{self.id}])'
                     f'._verify_level3_cron({log.id})'),
            'interval_number': 1,
            'interval_type': 'days',
            'nextcall': nextcall,
            'active': True,
            'user_id': self.env.ref('base.user_root').id,
        })

    def _verify_level3_cron(self, log_id):
        """Cron entry point for deferred L3 verification."""
        cron = self.env.context.get('cron_id')
        if cron:
            self.env['ir.cron'].sudo().browse(cron).active = False
        log = self.env['txr.db.backup.log'].browse(log_id)
        if not log.exists():
            _logger.warning('L3 cron: log %s no longer exists', log_id)
            return
        file_path = log.file_path
        if not file_path:
            log.write({'verify_l3': 'skipped'})
            log._append_error('L3: no file_path on log')
            return
        try:
            self._verify_level3(log, file_path)
        except Exception as exc:
            _logger.exception('L3 verification failed for log %s', log_id)
            log.write({'verify_l3': 'failed'})
            log._append_error(f'L3: {exc}')

    def _verify_level3(self, log, file_path):
        """Restore backup into a temp DB and check key tables. Always dropdb."""
        self.ensure_one()
        try:
            timeout_min = int(self.env['ir.config_parameter'].sudo().get_param(
                'txr.backup.l3_timeout_minutes', str(_DEFAULT_L3_TIMEOUT_MIN),
            ))
        except ValueError:
            timeout_min = _DEFAULT_L3_TIMEOUT_MIN
        timeout_sec = max(60, timeout_min * 60)

        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        tmp_db = f'txr_verify_{self.database_name}_{ts}'
        # Strip any unsafe characters (db names: lowercase, alnum, underscore)
        tmp_db = ''.join(c for c in tmp_db.lower() if c.isalnum() or c == '_')[:60]

        created = False
        work_tmp = tempfile.mkdtemp(prefix='txr_l3_')
        try:
            # createdb with low-priority wrapper if requested
            create_cmd = self._build_l3_cmd(['createdb', tmp_db])
            try:
                cp = subprocess.run(
                    create_cmd, capture_output=True, text=True, timeout=timeout_sec,
                )
            except subprocess.TimeoutExpired:
                log.write({'verify_l3': 'skipped'})
                log._append_error(f'L3: createdb timed out after {timeout_min} min')
                return
            if cp.returncode != 0:
                stderr = (cp.stderr or '').lower()
                if 'permission denied' in stderr or 'must be superuser' in stderr:
                    log.write({'verify_l3': 'skipped'})
                    log._append_error('L3: insufficient createdb privilege')
                else:
                    log.write({'verify_l3': 'failed'})
                    log._append_error(f'L3: createdb failed: {cp.stderr}')
                return
            created = True

            try:
                if file_path.endswith('.zip'):
                    self._l3_restore_zip(log, file_path, tmp_db, work_tmp, timeout_sec)
                else:
                    self._l3_restore_dump(log, file_path, tmp_db, work_tmp, timeout_sec)
            except subprocess.TimeoutExpired:
                log.write({'verify_l3': 'skipped'})
                log._append_error(f'L3: restore timed out after {timeout_min} min')
                return
            except RuntimeError as exc:
                log.write({'verify_l3': 'failed'})
                log._append_error(f'L3: restore failed: {exc}')
                return

            # Critical-table query on the restored DB
            try:
                if not self._l3_check_tables(tmp_db):
                    log.write({'verify_l3': 'failed'})
                    log._append_error('L3: critical tables empty or missing')
                    return
            except psycopg2.errors.InsufficientPrivilege:
                log.write({'verify_l3': 'skipped'})
                log._append_error('L3: insufficient privilege to query restored DB')
                return
            except Exception as exc:
                log.write({'verify_l3': 'failed'})
                log._append_error(f'L3: query failed: {exc}')
                return

            log.write({'verify_l3': 'passed', 'verify_l3_note': ''})
            self.write({'verify_l3_last_run': fields.Datetime.now()})

        finally:
            if created:
                drop_cmd = self._build_l3_cmd(['dropdb', '--if-exists', tmp_db])
                try:
                    subprocess.run(drop_cmd, capture_output=True, text=True, timeout=120)
                except Exception as exc:
                    _logger.warning('L3 dropdb failed for %s: %s', tmp_db, exc)
            shutil.rmtree(work_tmp, ignore_errors=True)

    def _l3_restore_dump(self, log, file_path, tmp_db, work_tmp, timeout_sec):
        """Restore a custom-format dump (.dump or .dump.gz) into tmp_db."""
        actual_path = file_path
        if file_path.endswith('.gz'):
            actual_path = os.path.join(work_tmp, 'l3_restore.dump')
            with gzip.open(file_path, 'rb') as f_in, open(actual_path, 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
        cmd = self._build_l3_cmd(['pg_restore', '-d', tmp_db, actual_path])
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        # pg_restore frequently exits non-zero on benign warnings; treat
        # tmp_db being populated as success-side check via _l3_check_tables.
        if cp.returncode != 0 and 'errors ignored' not in (cp.stderr or '').lower():
            # Surface stderr for diagnosis but only fail hard if obvious.
            log._append_error(f'L3 pg_restore stderr: {(cp.stderr or "")[:500]}')

    def _l3_restore_zip(self, log, file_path, tmp_db, work_tmp, timeout_sec):
        """Extract dump.sql from Odoo zip and load via psql."""
        sql_path = os.path.join(work_tmp, 'dump.sql')
        with zipfile.ZipFile(file_path, 'r') as zf:
            if 'dump.sql' not in zf.namelist():
                raise RuntimeError('zip contains no dump.sql')
            with zf.open('dump.sql') as src, open(sql_path, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        cmd = self._build_l3_cmd(['psql', '-d', tmp_db, '-f', sql_path, '-q'])
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        if cp.returncode != 0:
            raise RuntimeError(f'psql exit {cp.returncode}: {(cp.stderr or "")[:500]}')

    def _l3_check_tables(self, tmp_db):
        """Return True if critical tables (res_users etc.) have rows."""
        conn = psycopg2.connect(dbname=tmp_db)
        try:
            with conn.cursor() as cur:
                if self.verify_l3_low_priority:
                    cur.execute("SET work_mem = '64MB'")
                    cur.execute("SET maintenance_work_mem = '64MB'")
                for table in ('res_users', 'res_company', 'ir_model'):
                    cur.execute(
                        psycopg2.sql.SQL('SELECT count(*) FROM {}').format(
                            psycopg2.sql.Identifier(table)
                        )
                    )
                    n = cur.fetchone()[0]
                    if not n:
                        return False
            return True
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  子段 C: L3 preflight + window + low-priority resource use
    # ------------------------------------------------------------------ #

    def _l3_preflight(self, log, file_path):
        """Collect L3 preflight info into log.l3_preflight_info."""
        self.ensure_one()
        lines = []
        # 1. PG data directory free space vs DB size × 1.3
        try:
            conn = psycopg2.connect(dbname=self.database_name)
            try:
                with conn.cursor() as cur:
                    cur.execute('SHOW data_directory')
                    data_dir = cur.fetchone()[0]
                    cur.execute(
                        'SELECT pg_database_size(%s)', (self.database_name,),
                    )
                    db_size = cur.fetchone()[0]
            finally:
                conn.close()
            free_pg = shutil.disk_usage(data_dir).free
            needed = db_size * 1.3
            mark = '⚠️ ' if free_pg < needed else ''
            lines.append(
                f'{mark}PG data dir {data_dir}: '
                f'{free_pg / 1e9:.1f}GB free, '
                f'~{needed / 1e9:.1f}GB needed'
            )
        except Exception as exc:
            lines.append(f'(SHOW data_directory failed: {exc}; continuing)')

        # 2. Tmp dir free vs zip-extract overhead (only for zip from cloud)
        if self.storage_type == 'rclone' and file_path and file_path.endswith('.zip'):
            try:
                free_tmp = shutil.disk_usage(tempfile.gettempdir()).free
                needed_tmp = (log.file_size or 0) * 1.2
                mark = '⚠️ ' if free_tmp < needed_tmp else ''
                lines.append(
                    f'{mark}Tmp dir: {free_tmp / 1e9:.1f}GB free, '
                    f'~{needed_tmp / 1e9:.1f}GB needed (zip extract)'
                )
            except Exception as exc:
                lines.append(f'(tmp disk check failed: {exc})')

        # 3. Resource priority mode used
        if self.verify_l3_low_priority:
            tools = []
            if shutil.which('ionice'):
                tools.append('ionice -c 3')
            if shutil.which('nice'):
                tools.append('nice -n 10')
            tools.append("PG: SET work_mem='64MB'")
            lines.append('Priority: ' + (', '.join(tools) if tools else 'none available'))
        else:
            lines.append('Priority: standard (low_priority=False)')

        log.write({'l3_preflight_info': '\n'.join(lines)})

    def _append_l3_preflight(self, log, line):
        """Append a line to existing l3_preflight_info."""
        existing = log.l3_preflight_info or ''
        log.write({'l3_preflight_info': f'{existing}\n{line}' if existing else line})

    @staticmethod
    def _next_window_start(window_start, window_end, now=None):
        """Return next datetime when the window opens, or None if currently in.

        window_start/window_end are float hours [0, 24). Supports cross-midnight
        windows (e.g. start=22.0, end=6.0 means 22:00 → next-day 06:00).
        Returned datetime is naive UTC, matching Odoo's ir.cron.nextcall.
        """
        now = now or fields.Datetime.now()
        hour_float = now.hour + now.minute / 60.0
        cross_midnight = window_start >= window_end

        if cross_midnight:
            in_window = hour_float >= window_start or hour_float < window_end
        else:
            in_window = window_start <= hour_float < window_end
        if in_window:
            return None

        # Compute today_start at window_start hour
        sh = int(window_start)
        sm = int(round((window_start - sh) * 60))
        today_start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if today_start <= now:
            today_start += timedelta(days=1)
        return today_start

    def _build_l3_cmd(self, base_cmd):
        """Wrap base_cmd with ionice/nice when low-priority mode is enabled."""
        self.ensure_one()
        if not self.verify_l3_low_priority:
            return list(base_cmd)
        prefix = []
        if _HAS_IONICE:
            prefix += ['ionice', '-c', '3']
        if _HAS_NICE:
            prefix += ['nice', '-n', '10']
        return prefix + list(base_cmd)

    # ------------------------------------------------------------------ #
    #  子段 D: Split / Streaming helpers
    # ------------------------------------------------------------------ #

    def _get_filestore_path(self):
        """Return the filestore directory for this backup, validated."""
        self.ensure_one()
        if self.filestore_path:
            path = self.filestore_path
        else:
            path = os.path.join(config['data_dir'], 'filestore', self.database_name)
        if not os.path.isdir(path) or not os.access(path, os.R_OK):
            raise UserError(_(
                'Filestore path %s does not exist or is not readable.', path,
            ))
        return path

    def _pre_flight_disk_check(self, tmp_dir):
        """Raise if tmp_dir lacks the disk space required by current strategy."""
        self.ensure_one()
        strategy = self.backup_strategy
        if strategy == 'streaming':
            return
        try:
            free = shutil.disk_usage(tmp_dir).free
        except Exception:
            return
        db_size = self._estimate_db_size() or 0
        if strategy == 'split':
            needed = db_size * 1.5
        else:
            fs_size = self.filestore_size_bytes or self._estimate_filestore_size() or 0
            needed = (db_size + fs_size) * 1.5
        min_required = max(needed, self.pre_backup_disk_min_gb * _GB)
        if free < min_required:
            raise UserError(_(
                'Insufficient temp disk space: %.1fGB free, %.1fGB needed '
                '(strategy=%s).',
                free / _GB, min_required / _GB, strategy,
            ))

    def _sync_filestore(self, log):
        """rclone sync filestore directory to remote, drain stderr in thread."""
        self.ensure_one()
        log.write({'phase': 'filestore_sync'})
        self._safe_set_phase_detail(log, 'rclone sync')
        filestore_path = self._get_filestore_path()
        remote = self.filestore_cloud_remote_id or self.cloud_remote_id
        if not remote:
            raise UserError(_('No cloud remote configured for filestore sync.'))
        remote._check_rclone_binary()
        cfg_path = remote._write_tmp_config()
        cloud_path = self.filestore_cloud_path or 'filestore'
        dest = f'{remote.name}:{posixpath.join(cloud_path, self.database_name)}'

        cmd = [
            'rclone', '--config', cfg_path, 'sync',
            filestore_path, dest,
            '--transfers', '8', '--immutable',
            '--stats-one-line', '--stats-log-level', 'NOTICE',
        ]
        start = time.monotonic()
        stderr_lines = []
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1,
            )
            drain = threading.Thread(
                target=self._drain_stream,
                args=(proc.stderr, stderr_lines),
                daemon=True,
            )
            drain.start()
            rc = proc.wait()
            drain.join(timeout=2)
            duration = time.monotonic() - start
            files_synced, bytes_synced = self._parse_rclone_stats(stderr_lines)
            log.write({
                'filestore_files_synced': files_synced,
                'filestore_bytes_synced': bytes_synced,
                'filestore_sync_duration': duration,
            })
            if rc != 0:
                tail = '\n'.join(stderr_lines[-5:])
                raise RuntimeError(f'rclone sync exit {rc}: {tail}')
        finally:
            remote._cleanup_tmp_config(cfg_path)

    @staticmethod
    def _safe_set_phase_detail(log, value):
        """Write phase_detail to log if the field exists; never raise."""
        if 'phase_detail' not in log._fields:
            return
        try:
            log.write({'phase_detail': value})
        except Exception:
            pass

    @staticmethod
    def _drain_stream(stream, sink):
        """Drain a subprocess pipe (text or bytes) into a list."""
        try:
            for line in stream:
                if isinstance(line, bytes):
                    sink.append(line.rstrip(b'\n'))
                else:
                    sink.append(line.rstrip('\n'))
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _parse_rclone_stats(lines):
        """Best-effort parse of rclone --stats-one-line output.

        Looks for the most recent 'Transferred:' summary line. Returns
        (files_synced, bytes_synced) ints (zeros if not parseable).
        """
        files = 0
        nbytes = 0
        for line in reversed(lines):
            if 'Transferred:' not in line:
                continue
            # Examples seen in rclone output:
            #   "Transferred: 12.345 MiB / 12.345 MiB, 100%, ... 5 / 5"
            #   "Transferred:        2.345k / 2.345 kBytes, 100%, ..."
            try:
                # "files:" form (newer rclone)
                if 'files:' in line.lower():
                    files_part = line.lower().split('files:')[1].strip()
                    files = int(files_part.split(',')[0].split('/')[0].strip())
            except Exception:
                pass
            break
        return files, nbytes

    def _streaming_backup(self, log):
        """pg_dump | encrypt | rclone rcat. Subject to streaming_timeout cap."""
        self.ensure_one()
        if not self.cloud_remote_id:
            raise UserError(_('Streaming requires a Cloud Remote.'))
        remote = self.cloud_remote_id
        remote._check_rclone_binary()
        cfg_path = remote._write_tmp_config()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        suffix = '.dump.enc' if self.encrypt_backup else '.dump'
        filename = f'{self.database_name}_{timestamp}{suffix}'
        cloud_path = self.cloud_path or ''
        remote_path = posixpath.join(cloud_path, filename) if cloud_path else filename
        dest = f'{remote.name}:{remote_path}'

        timeout_min = int(self.env['ir.config_parameter'].sudo().get_param(
            'txr.backup.streaming_timeout_minutes',
            str(_DEFAULT_STREAMING_TIMEOUT_MIN),
        ))
        timeout_sec = max(60, timeout_min * 60)

        p_dump = None
        p_rclone = None
        dump_err, rclone_err = [], []
        bridge_exc = None
        try:
            p_dump = subprocess.Popen(
                ['pg_dump', '-Fc', self.database_name],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=False, bufsize=0,
            )
            p_rclone = subprocess.Popen(
                ['rclone', '--config', cfg_path, 'rcat', dest],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False, bufsize=0,
            )

            t_dump = threading.Thread(
                target=self._drain_stream,
                args=(p_dump.stderr, dump_err),
                daemon=True,
            )
            t_rclone = threading.Thread(
                target=self._drain_stream,
                args=(p_rclone.stderr, rclone_err),
                daemon=True,
            )
            t_dump.start()
            t_rclone.start()

            try:
                if self.encrypt_backup:
                    self._encrypt_pipe_with_timeout(
                        p_dump.stdout, p_rclone.stdin, log,
                        deadline=time.monotonic() + timeout_sec,
                        procs=(p_dump, p_rclone),
                    )
                else:
                    self._copy_pipe_with_timeout(
                        p_dump.stdout, p_rclone.stdin, log,
                        deadline=time.monotonic() + timeout_sec,
                        procs=(p_dump, p_rclone),
                    )
            except Exception as exc:
                bridge_exc = exc

            try:
                p_rclone.stdin.close()
            except Exception:
                pass

            rc_rclone = p_rclone.wait()
            rc_dump = p_dump.wait()

            t_dump.join(timeout=2)
            t_rclone.join(timeout=2)

            if bridge_exc or rc_rclone != 0 or rc_dump != 0:
                msg_parts = []
                if bridge_exc:
                    msg_parts.append(f'bridge: {bridge_exc}')
                if rc_dump != 0:
                    tail = b'\n'.join(dump_err[-5:]).decode('utf-8', errors='replace')
                    msg_parts.append(f'pg_dump exit {rc_dump}: {tail}')
                if rc_rclone != 0:
                    tail = b'\n'.join(rclone_err[-5:]).decode('utf-8', errors='replace')
                    msg_parts.append(f'rclone exit {rc_rclone}: {tail}')
                raise RuntimeError('; '.join(msg_parts))

            log.write({'file_path': remote_path})
            self._safe_set_phase_detail(log, 'completed')
        finally:
            for proc in (p_dump, p_rclone):
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            remote._cleanup_tmp_config(cfg_path)

    def _check_streaming_deadline(self, deadline, procs):
        if time.monotonic() > deadline:
            for p in procs:
                if p and p.poll() is None:
                    try:
                        p.kill()
                    except Exception:
                        pass
            raise UserError(_('Streaming backup timed out'))

    def _encrypt_pipe_with_timeout(self, source, dest, log, deadline, procs):
        """Encrypted bridging with periodic deadline checks."""
        self._aes_encrypt_stream(
            source, dest, log=log, deadline=deadline, procs=procs,
        )

    def _copy_pipe_with_timeout(self, source, dest, log, deadline, procs):
        """Plain copy bridging (no encryption) with periodic deadline checks."""
        chunk = 1024 * 1024
        total = 0
        while True:
            self._check_streaming_deadline(deadline, procs)
            data = source.read(chunk)
            if not data:
                break
            dest.write(data)
            total += len(data)
            if total % (16 * 1024 * 1024) < chunk:
                self._safe_set_phase_detail(
                    log, f'{total // (1024 * 1024)} MB transferred',
                )

    # ------------------------------------------------------------------ #
    #  子段 E: Sizing & strategy recommendation
    # ------------------------------------------------------------------ #

    def _estimate_db_size(self):
        """Return PG database size in bytes, or None on failure."""
        self.ensure_one()
        try:
            with self.env.cr.savepoint():
                self.env.cr.execute(
                    'SELECT pg_database_size(%s)', (self.database_name,),
                )
                row = self.env.cr.fetchone()
                return row[0] if row else None
        except Exception:
            return None

    def _estimate_filestore_size(self):
        """Recursively sum filestore size, abort after 5s. Returns bytes or None."""
        self.ensure_one()
        try:
            path = self._get_filestore_path()
        except UserError:
            return None
        deadline = time.monotonic() + _FILESTORE_ESTIMATE_DEADLINE
        total = 0
        stack = [path]
        try:
            while stack:
                if time.monotonic() > deadline:
                    return None
                current = stack.pop()
                with os.scandir(current) as it:
                    for entry in it:
                        if time.monotonic() > deadline:
                            return None
                        try:
                            if entry.is_file(follow_symlinks=False):
                                total += entry.stat(follow_symlinks=False).st_size
                            elif entry.is_dir(follow_symlinks=False):
                                stack.append(entry.path)
                        except OSError:
                            continue
        except OSError:
            return None
        return total

    def _get_available_tmp_disk(self):
        """Return free bytes on tempdir, or None on failure."""
        try:
            return shutil.disk_usage(tempfile.gettempdir()).free
        except Exception:
            return None

    def _compute_recommended_strategy(self):
        """Return (strategy, reason). (None, None) when no info available."""
        self.ensure_one()
        if self.storage_type != 'local' and not self.cloud_remote_id:
            return (None, None)
        db_size = self._estimate_db_size()
        fs_size = self.filestore_size_bytes or self._estimate_filestore_size()
        tmp_disk = self._get_available_tmp_disk()
        if db_size is None or fs_size is None or tmp_disk is None:
            return (None, None)

        if tmp_disk > 2 * (db_size + fs_size):
            return (
                'standard',
                f'Available disk {tmp_disk / _GB:.1f}GB covers full backup '
                f'(~{(db_size + fs_size) / _GB:.1f}GB needed)',
            )
        if fs_size >= _FILESTORE_LARGE_THRESHOLD:
            return (
                'split',
                f'Filestore {fs_size / _GB:.1f}GB too large for standard; '
                f'split keeps temp disk under {db_size * 1.2 / _GB:.1f}GB',
            )
        if tmp_disk <= db_size * 1.5:
            return (
                'streaming',
                'Disk too limited for any temp file; streaming pipes '
                'directly (no L1/L2/L3)',
            )
        return (
            'split',
            f'Disk constrained; split avoids temp staging the {fs_size / _GB:.1f}GB filestore',
        )

    @api.depends('backup_strategy', 'local_path', 'filestore_path',
                 'database_name', 'storage_type', 'cloud_remote_id',
                 'filestore_size_bytes')
    def _compute_strategy_recommendation(self):
        # Heavy compute (SQL + 5s disk scan). Skip on list / kanban loads
        # where this field isn't displayed; the form view re-reads via
        # invalidate_recordset on its onchange.
        if self.env.context.get('skip_strategy_compute'):
            for rec in self:
                rec.recommended_strategy = False
                rec.strategy_recommendation_reason = False
            return
        for rec in self:
            try:
                strategy, reason = rec._compute_recommended_strategy()
            except Exception as exc:
                _logger.warning('Strategy recommendation failed for %s: %s',
                                rec.name, exc)
                strategy, reason = (None, None)
            rec.recommended_strategy = strategy or False
            rec.strategy_recommendation_reason = reason or False

    def action_use_recommended_strategy(self):
        self.ensure_one()
        if not self.recommended_strategy:
            raise UserError(_('No recommendation available right now.'))
        self.backup_strategy = self.recommended_strategy
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    def _write_preflight_info(self, log):
        """Write the pre-flight summary into log.preflight_info."""
        try:
            db_size = self._estimate_db_size()
            fs_size = self.filestore_size_bytes or self._estimate_filestore_size()
            tmp_disk = self._get_available_tmp_disk()
            strategy = self.backup_strategy

            def _fmt(n):
                return f'{n / _GB:.2f} GB' if n is not None else 'N/A (estimation timed out)'

            if strategy == 'standard':
                needed = ((db_size or 0) + (fs_size or 0)) * 1.5
            elif strategy == 'split':
                needed = (db_size or 0) * 1.5
            else:
                needed = 0
            lines = [
                f'Strategy: {strategy}',
                f'DB size:        {_fmt(db_size)}',
                f'Filestore size: {_fmt(fs_size)}',
                f'Tmp disk free:  {_fmt(tmp_disk)}',
                f'Est. tmp need:  {_fmt(needed) if needed else "N/A (streaming)"}',
            ]
            log.write({'preflight_info': '\n'.join(lines)})
        except Exception as exc:
            _logger.warning('preflight_info write failed: %s', exc)

    # ------------------------------------------------------------------ #
    #  子段 E: Computed display fields
    # ------------------------------------------------------------------ #

    @api.depends('database_name')
    def _compute_db_size_bytes(self):
        # Live SQL on every form load is intentional (D11). Skip on list /
        # kanban contexts to avoid N×SELECT pg_database_size().
        if self.env.context.get('skip_strategy_compute'):
            for rec in self:
                rec.db_size_bytes = 0
            return
        for rec in self:
            rec.db_size_bytes = rec._estimate_db_size() or 0

    @api.depends()
    def _compute_estimated_duration(self):
        Log = self.env['txr.db.backup.log']
        for rec in self:
            logs = Log.search([
                ('backup_id', '=', rec.id),
                ('state', '=', 'success'),
            ], order='started_at desc', limit=5)
            durations = [d for d in logs.mapped('duration') if d and d > 0]
            if not durations:
                rec.estimated_duration_seconds = 0.0
                rec.estimated_duration_label = _('No estimate yet (first backup)')
                continue
            mean = sum(durations) / len(durations)
            rec.estimated_duration_seconds = mean
            if len(durations) >= 2:
                stdev = statistics.pstdev(durations)
            else:
                stdev = 0.0
            if mean > 0 and stdev / mean > 0.30 and len(durations) >= 2:
                low = max(1, int(min(durations) / 60))
                high = max(low, int(max(durations) / 60))
                rec.estimated_duration_label = f'{low}–{high} min'
            else:
                rec.estimated_duration_label = f'~{max(1, int(mean / 60))} min'

    def action_refresh_filestore_size(self):
        """Recompute filestore size and cache it. Notify on timeout."""
        self.ensure_one()
        size = self._estimate_filestore_size()
        if size is None:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Filestore Size'),
                    'message': _(
                        'Estimation took too long or path unreadable. '
                        'Cached size not updated.'),
                    'type': 'warning',
                    'sticky': False,
                },
            }
        self.write({
            'filestore_size_bytes': size,
            'filestore_size_updated_at': fields.Datetime.now(),
        })
        return {'type': 'ir.actions.client', 'tag': 'reload'}

    # ------------------------------------------------------------------ #
    #  Onchange triggers (recommendation refresh)
    # ------------------------------------------------------------------ #

    @api.onchange('backup_strategy', 'local_path', 'filestore_path',
                  'database_name', 'storage_type')
    def _onchange_strategy_inputs(self):
        # non-stored compute fields auto-recompute when their @api.depends
        # fields change in onchange context — invalidate to be explicit.
        self.invalidate_recordset(
            ['recommended_strategy', 'strategy_recommendation_reason'],
        )
