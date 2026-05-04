"""Tests for AES-256-GCM backup encryption and decryption."""
import base64
import io
import os
import tempfile
import time

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('txr_db_backup_pro')
class TestBackupEncryption(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.test_key_bytes = b'A' * 32
        cls.test_key_b64 = base64.b64encode(cls.test_key_bytes).decode()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'enc-test',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'encrypt_backup': True,
            'encrypt_key': cls.test_key_b64,
        })

    # ------------------------------------------------------------------ #
    #  _encrypt_file / _decrypt_if_needed round-trip
    # ------------------------------------------------------------------ #

    def test_encrypt_decrypt_round_trip(self):
        plaintext = b'Hello, this is a secret backup!' * 1000
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dump') as f:
            f.write(plaintext)
            in_path = f.name
        enc_path = None
        try:
            enc_path = self.backup._encrypt_file(in_path)
            self.assertTrue(enc_path.endswith('.enc'))
            self.assertTrue(os.path.exists(enc_path))
            self.assertFalse(os.path.exists(in_path), 'source file should be removed after encryption')

            log = self.env['txr.db.backup.log'].create({
                'backup_id': self.backup.id,
                'state': 'success',
                'file_path': enc_path,
            })
            job = self.env['txr.db.restore.job'].create({
                'backup_log_id': log.id,
                'target_db': 'testdb_restored',
            })
            decrypted_path = job._decrypt_if_needed(enc_path)
            with open(decrypted_path, 'rb') as fh:
                got = fh.read()
            self.assertEqual(got, plaintext)
        finally:
            for p in (in_path, enc_path):
                if p and os.path.exists(p):
                    os.remove(p)
            # also clean up decrypted file (same as in_path without .enc)
            decrypted = in_path  # strip .enc → original path
            if os.path.exists(decrypted):
                os.remove(decrypted)

    def test_encrypt_decrypt_large_data(self):
        """Verify chunked (>16MB) encrypt/decrypt works correctly."""
        chunk_16mb = 16 * 1024 * 1024
        plaintext = bytes(range(256)) * (chunk_16mb // 256 + 1000)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dump') as f:
            f.write(plaintext)
            in_path = f.name
        enc_path = None
        try:
            enc_path = self.backup._encrypt_file(in_path)
            log = self.env['txr.db.backup.log'].create({
                'backup_id': self.backup.id,
                'state': 'success',
                'file_path': enc_path,
            })
            job = self.env['txr.db.restore.job'].create({
                'backup_log_id': log.id,
                'target_db': 'testdb_large_restored',
            })
            decrypted_path = job._decrypt_if_needed(enc_path)
            with open(decrypted_path, 'rb') as fh:
                got = fh.read()
            self.assertEqual(got, plaintext)
        finally:
            for p in (in_path, enc_path, in_path):
                if p and os.path.exists(p):
                    os.remove(p)

    # ------------------------------------------------------------------ #
    #  _encrypt_file: naming and source removal
    # ------------------------------------------------------------------ #

    def test_enc_file_naming_convention(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dump.gz') as f:
            f.write(b'compressed_dump_content_here_quite_long' * 100)
            in_path = f.name
        enc_path = None
        try:
            enc_path = self.backup._encrypt_file(in_path)
            self.assertEqual(enc_path, in_path + '.enc')
        finally:
            for p in (in_path, enc_path):
                if p and os.path.exists(p):
                    os.remove(p)

    def test_encrypt_removes_source_file(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dump') as f:
            f.write(b'data to be encrypted and source removed')
            in_path = f.name
        enc_path = None
        try:
            enc_path = self.backup._encrypt_file(in_path)
            self.assertFalse(os.path.exists(in_path))
            self.assertTrue(os.path.exists(enc_path))
        finally:
            for p in (in_path, enc_path):
                if p and os.path.exists(p):
                    os.remove(p)

    def test_enc_output_is_different_from_input(self):
        plaintext = b'plaintext backup data'
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dump') as f:
            f.write(plaintext)
            in_path = f.name
        enc_path = None
        try:
            enc_path = self.backup._encrypt_file(in_path)
            with open(enc_path, 'rb') as fh:
                encrypted_bytes = fh.read()
            self.assertNotEqual(encrypted_bytes, plaintext)
        finally:
            for p in (in_path, enc_path):
                if p and os.path.exists(p):
                    os.remove(p)

    # ------------------------------------------------------------------ #
    #  _encrypt_file: error cases
    # ------------------------------------------------------------------ #

    def test_encrypt_requires_key(self):
        no_key_backup = self.env['txr.db.backup'].create({
            'name': 'no-key',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'encrypt_backup': True,
        })
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dump') as f:
            f.write(b'data')
            in_path = f.name
        try:
            with self.assertRaises(Exception):
                no_key_backup._encrypt_file(in_path)
        finally:
            for p in (in_path, in_path + '.enc'):
                if os.path.exists(p):
                    os.remove(p)

    def test_encrypt_invalid_base64_key_raises(self):
        bad_key_backup = self.env['txr.db.backup'].create({
            'name': 'bad-key',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'encrypt_backup': True,
            'encrypt_key': 'not-valid-base64!!!',
        })
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dump') as f:
            f.write(b'data')
            in_path = f.name
        try:
            with self.assertRaises(Exception):
                bad_key_backup._encrypt_file(in_path)
        finally:
            for p in (in_path, in_path + '.enc'):
                if os.path.exists(p):
                    os.remove(p)

    def test_encrypt_wrong_key_length_raises(self):
        short_key = base64.b64encode(b'tooshort').decode()
        bad_key_backup = self.env['txr.db.backup'].create({
            'name': 'short-key',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'encrypt_backup': True,
            'encrypt_key': short_key,
        })
        with tempfile.NamedTemporaryFile(delete=False, suffix='.dump') as f:
            f.write(b'data')
            in_path = f.name
        try:
            with self.assertRaises(UserError):
                bad_key_backup._encrypt_file(in_path)
        finally:
            for p in (in_path, in_path + '.enc'):
                if os.path.exists(p):
                    os.remove(p)

    # ------------------------------------------------------------------ #
    #  action_generate_key
    # ------------------------------------------------------------------ #

    def test_action_generate_key_when_empty(self):
        b = self.env['txr.db.backup'].create({
            'name': 'gen-test',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'encrypt_backup': True,
        })
        b.action_generate_key()
        self.assertTrue(b.encrypt_key)
        decoded = base64.b64decode(b.encrypt_key)
        self.assertEqual(len(decoded), 32, 'generated key must be 32 bytes')

    def test_action_generate_key_overwrites_when_set(self):
        """Overwriting an existing key is now allowed via confirmation in the view layer."""
        old_key = self.backup.encrypt_key
        self.backup.action_generate_key()
        self.assertTrue(self.backup.encrypt_key)
        self.assertNotEqual(self.backup.encrypt_key, old_key, 'key must change on regenerate')

    def test_action_generate_key_produces_unique_keys(self):
        b1 = self.env['txr.db.backup'].create({
            'name': 'unique-key-1',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'encrypt_backup': True,
        })
        b2 = self.env['txr.db.backup'].create({
            'name': 'unique-key-2',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'encrypt_backup': True,
        })
        b1.action_generate_key()
        b2.action_generate_key()
        self.assertNotEqual(b1.encrypt_key, b2.encrypt_key)

    # ------------------------------------------------------------------ #
    #  _decrypt_if_needed: pass-through for non-.enc files
    # ------------------------------------------------------------------ #

    def test_decrypt_passes_through_non_enc(self):
        log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'file_path': '/tmp/dummy.dump',
        })
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': log.id,
            'target_db': 'testdb_restored',
        })
        result = job._decrypt_if_needed('/tmp/some.dump')
        self.assertEqual(result, '/tmp/some.dump')

    def test_decrypt_passes_through_zip_file(self):
        log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'file_path': '/tmp/backup.zip',
        })
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': log.id,
            'target_db': 'testdb_restored',
        })
        result = job._decrypt_if_needed('/tmp/backup.zip')
        self.assertEqual(result, '/tmp/backup.zip')

    def test_decrypt_passes_through_dump_gz(self):
        log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'success',
            'file_path': '/tmp/backup.dump.gz',
        })
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': log.id,
            'target_db': 'testdb_restored',
        })
        result = job._decrypt_if_needed('/tmp/backup.dump.gz')
        self.assertEqual(result, '/tmp/backup.dump.gz')

    def test_decrypt_without_key_raises(self):
        no_key_backup = self.env['txr.db.backup'].create({
            'name': 'no-key-restore',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'encrypt_backup': True,
        })
        log = self.env['txr.db.backup.log'].create({
            'backup_id': no_key_backup.id,
            'state': 'success',
            'file_path': '/tmp/backup.dump.enc',
        })
        job = self.env['txr.db.restore.job'].create({
            'backup_log_id': log.id,
            'target_db': 'testdb_restored',
        })
        with self.assertRaises(UserError):
            job._decrypt_if_needed('/tmp/backup.dump.enc')


# ──────────────────────────────────────────────────────────────────────────────
#  _encrypt_pipe_with_timeout
# ──────────────────────────────────────────────────────────────────────────────

@tagged('txr_db_backup_pro')
class TestEncryptPipeWithTimeout(TransactionCase):
    """_encrypt_pipe_with_timeout: AES stream via BytesIO; deadline enforcement."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.key_b64 = base64.b64encode(os.urandom(32)).decode()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'pipe-timeout-test',
            'database_name': 'testdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
            'encrypt_backup': True,
            'encrypt_key': cls.key_b64,
        })

    def _pipe(self, plaintext, deadline):
        src = io.BytesIO(plaintext)
        dst = io.BytesIO()
        self.backup._encrypt_pipe_with_timeout(src, dst, log=None, deadline=deadline, procs=[])
        return dst.getvalue()

    def test_encrypt_pipe_produces_nonempty_ciphertext(self):
        result = self._pipe(b'hello pipe encryption', time.monotonic() + 60)
        self.assertGreater(len(result), 0)

    def test_encrypt_pipe_output_differs_from_input(self):
        plaintext = b'secret backup data for pipe test'
        result = self._pipe(plaintext, time.monotonic() + 60)
        self.assertNotEqual(result, plaintext)

    def test_encrypt_pipe_raises_user_error_on_deadline(self):
        with self.assertRaises(UserError):
            self._pipe(b'data', deadline=0.0)
