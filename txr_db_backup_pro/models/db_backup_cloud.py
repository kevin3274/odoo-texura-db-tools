import logging
import os
import shutil
import subprocess
import tempfile
from subprocess import TimeoutExpired as _SubprocessTimeoutExpired

from odoo import _, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

_RCLONE_TYPE_SELECTION = [
    ('s3', 'Amazon S3 / Compatible'),
    ('gcs', 'Google Cloud Storage'),
    ('azure', 'Azure Blob Storage'),
    ('b2', 'Backblaze B2'),
    ('webdav', 'Nextcloud / WebDAV'),
    ('custom', 'Custom rclone config'),
]

_WEBDAV_VENDOR_SELECTION = [
    ('nextcloud', 'Nextcloud'),
    ('owncloud', 'ownCloud'),
    ('sharepoint', 'SharePoint'),
    ('other', 'Other (generic WebDAV)'),
]


class TxrDbBackupCloud(models.Model):
    _name = 'txr.db.backup.cloud'
    _description = 'Cloud Storage Remote (rclone)'

    name = fields.Char(required=True, string='Remote Name')
    rclone_type = fields.Selection(
        _RCLONE_TYPE_SELECTION,
        required=True,
        default='s3',
        string='Storage Type',
    )

    s3_provider = fields.Selection(
        [
            ('AWS', 'Amazon Web Services'),
            ('Cloudflare', 'Cloudflare R2'),
            ('Minio', 'MinIO'),
            ('Other', 'Other S3-compatible'),
        ],
        string='S3 Provider',
        default='AWS',
    )
    s3_access_key = fields.Char(string='Access Key ID')
    s3_secret_key = fields.Char(string='Secret Access Key', groups='base.group_system')
    s3_bucket = fields.Char(string='Bucket Name')
    s3_region = fields.Char(string='Region', default='us-east-1')
    s3_endpoint = fields.Char(string='Endpoint URL (for S3-compatible)')

    gcs_service_account_json = fields.Text(
        string='Service Account JSON',
        groups='base.group_system',
    )
    gcs_bucket = fields.Char(string='GCS Bucket Name')

    azure_account = fields.Char(string='Storage Account Name')
    azure_key = fields.Char(string='Storage Account Key', groups='base.group_system')
    azure_container = fields.Char(string='Container Name')

    b2_account = fields.Char(string='Application Key ID')
    b2_key = fields.Char(string='Application Key', groups='base.group_system')
    b2_bucket = fields.Char(string='Bucket Name')

    webdav_url = fields.Char(string='WebDAV URL')
    webdav_vendor = fields.Selection(
        _WEBDAV_VENDOR_SELECTION,
        string='WebDAV Vendor',
        default='nextcloud',
    )
    webdav_username = fields.Char(string='Username')
    webdav_password = fields.Char(string='Password', groups='base.group_system')

    rclone_config_raw = fields.Text(
        string='Custom rclone Config',
        help=(
            'Paste the full rclone config section here, e.g.:\n'
            '[myremote]\ntype = drive\n...\n\n'
            'For WebDAV with obscured password, use custom mode and run '
            '"rclone obscure <password>" locally to get the obscured value.'
        ),
    )

    def _generate_rclone_config(self):
        """Generate rclone ini config for self and return as string.

        WebDAV: rclone requires pass to be obscured via `rclone obscure`.
        Plaintext pass is rejected. Use custom mode for obscured password.
        """
        self.ensure_one()
        name = self.name
        rtype = self.rclone_type

        if rtype == 's3':
            lines = [
                f'[{name}]',
                'type = s3',
                f'provider = {self.s3_provider or "AWS"}',
                f'access_key_id = {self.s3_access_key or ""}',
                f'secret_access_key = {self.s3_secret_key or ""}',
                f'region = {self.s3_region or "us-east-1"}',
            ]
            if self.s3_endpoint:
                lines.append(f'endpoint = {self.s3_endpoint}')

        elif rtype == 'gcs':
            lines = [
                f'[{name}]',
                'type = google cloud storage',
                f'bucket = {self.gcs_bucket or ""}',
                f'service_account_credentials = {self.gcs_service_account_json or ""}',
            ]

        elif rtype == 'azure':
            lines = [
                f'[{name}]',
                'type = azureblob',
                f'account = {self.azure_account or ""}',
                f'key = {self.azure_key or ""}',
            ]

        elif rtype == 'b2':
            lines = [
                f'[{name}]',
                'type = b2',
                f'account = {self.b2_account or ""}',
                f'key = {self.b2_key or ""}',
            ]

        elif rtype == 'webdav':
            vendor = self.webdav_vendor or 'other'
            lines = [
                f'[{name}]',
                'type = webdav',
                f'url = {self.webdav_url or ""}',
                f'vendor = {vendor}',
                f'user = {self.webdav_username or ""}',
                f'pass = {self.webdav_password or ""}',
            ]

        elif rtype == 'custom':
            return self.rclone_config_raw or ''

        else:
            raise UserError(_('Unknown rclone type: %s', rtype))

        return '\n'.join(lines) + '\n'

    def _check_rclone_binary(self):
        if not shutil.which('rclone'):
            raise UserError(
                'rclone command not found. '
                'Install: apt install rclone or brew install rclone'
            )

    def _write_tmp_config(self):
        config_content = self._generate_rclone_config()
        fd, cfg_path = tempfile.mkstemp(suffix='.conf', prefix='txr_rclone_')
        try:
            os.write(fd, config_content.encode())
        finally:
            os.close(fd)
        os.chmod(cfg_path, 0o600)
        return cfg_path

    def _cleanup_tmp_config(self, cfg_path):
        try:
            os.remove(cfg_path)
        except FileNotFoundError:
            pass

    def action_test_connection(self):
        self.ensure_one()
        self._check_rclone_binary()
        cfg_path = self._write_tmp_config()
        try:
            result = subprocess.run(
                ['rclone', '--config', cfg_path, 'ls', '--max-depth', '1',
                 f'{self.name}:'],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise UserError(
                    _('Connection test failed:\n%s', result.stderr or result.stdout)
                )
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Connection Successful'),
                    'message': _('rclone remote "%s" is reachable.', self.name),
                    'type': 'success',
                    'sticky': False,
                },
            }
        except _SubprocessTimeoutExpired:
            raise UserError(_('Connection test timed out after 30 seconds.'))
        finally:
            self._cleanup_tmp_config(cfg_path)

    def _download_from_remote(self, remote_path, local_dir):
        """Download remote_path from this remote into local_dir, return local file path."""
        self.ensure_one()
        self._check_rclone_binary()
        cfg_path = self._write_tmp_config()
        try:
            result = subprocess.run(
                [
                    'rclone',
                    '--config', cfg_path,
                    'copy',
                    f'{self.name}:{remote_path}',
                    local_dir,
                ],
                capture_output=True,
                text=True,
                timeout=3600,
            )
            if result.returncode != 0:
                raise UserError(
                    _('rclone download failed:\n%s', result.stderr or result.stdout)
                )
            return os.path.join(local_dir, os.path.basename(remote_path))
        finally:
            self._cleanup_tmp_config(cfg_path)
