"""Tests for backup verification (_verify_level1 / _verify_level2)."""
import os
import shutil
import tempfile
import zipfile
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests.common import TransactionCase

MODULE_PATH = 'odoo.addons.txr_db_backup_lite.models.db_backup'

_TEST_DIR = '/tmp/test_verify'


class TestBackupVerification(TransactionCase):
    """Test L1 (SHA256) and L2 (pg_restore --list) verification."""

    def setUp(self):
        super().setUp()
        os.makedirs(_TEST_DIR, exist_ok=True)
        self.backup = self.env['txr.db.backup'].create({
            'name': 'Test Verify',
            'storage_type': 'local',
            'local_path': _TEST_DIR,
        })
        self.log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'running',
            'phase': 'verify',
            'started_at': fields.Datetime.now(),
            'storage_type': 'local',
        })

    def tearDown(self):
        shutil.rmtree(_TEST_DIR, ignore_errors=True)
        super().tearDown()

    def _make_temp_file(self, content=b'test content'):
        fd, path = tempfile.mkstemp(suffix='.dump')
        os.write(fd, content)
        os.close(fd)
        return path

    # -------------------------------------------------------------------------
    # Level 1 tests
    # -------------------------------------------------------------------------

    def test_verify_l1_passed(self):
        """sha256 一致时，verify_l1='passed'。"""
        path = self._make_temp_file()
        try:
            sha256 = self.backup._compute_sha256(path)
            self.log.write({'sha256': sha256})
            self.backup._verify_level1(self.log, sha256)
            self.assertEqual(self.log.verify_l1, 'passed')
        finally:
            os.unlink(path)

    def test_verify_l1_failed(self):
        """sha256 不一致时，verify_l1='failed'，state 变 warning。"""
        self.log.write({'sha256': 'wrong_hash_value_000'})
        self.backup._verify_level1(self.log, 'different_hash_value')
        self.assertEqual(self.log.verify_l1, 'failed')
        self.assertEqual(self.log.state, 'warning')

    # -------------------------------------------------------------------------
    # Level 2 tests
    # -------------------------------------------------------------------------

    def _make_toc_output(self, count=150, include_critical=True):
        """生成 pg_restore --list 风格输出。"""
        lines = []
        if include_critical:
            lines += [
                '1; 2615 2200 SCHEMA - public postgres',
                '10; 0 0 TABLE public res_company postgres',
                '20; 0 0 TABLE public res_users postgres',
                '30; 0 0 TABLE public res_partner postgres',
                '40; 0 0 TABLE public ir_model postgres',
            ]
        while len(lines) < count:
            lines.append(f'{len(lines) + 100}; 0 0 TABLE public dummy_table_{len(lines)} postgres')
        return '\n'.join(lines)

    def test_verify_l2_passed(self):
        """mock pg_restore --list 返回 >100 行含关键表，verify_l2='passed'。"""
        path = self._make_temp_file()
        toc_output = self._make_toc_output(count=150, include_critical=True)
        try:
            with patch(f'{MODULE_PATH}.subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0, stdout=toc_output, stderr=''
                )
                self.backup._verify_level2(self.log, path)
            self.assertEqual(self.log.verify_l2, 'passed')
        finally:
            os.unlink(path)

    def test_verify_l2_failed_bad_format(self):
        """pg_restore --list returncode=1，verify_l2='failed'，state='warning'。"""
        path = self._make_temp_file()
        try:
            with patch(f'{MODULE_PATH}.subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=1, stdout='', stderr='invalid file format'
                )
                self.backup._verify_level2(self.log, path)
            self.assertEqual(self.log.verify_l2, 'failed')
            self.assertEqual(self.log.state, 'warning')
        finally:
            os.unlink(path)

    def test_verify_l2_failed_too_few_entries(self):
        """TOC < 100 行时，verify_l2='failed'。"""
        path = self._make_temp_file()
        toc_output = self._make_toc_output(count=50, include_critical=True)
        try:
            with patch(f'{MODULE_PATH}.subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0, stdout=toc_output, stderr=''
                )
                self.backup._verify_level2(self.log, path)
            self.assertEqual(self.log.verify_l2, 'failed')
            self.assertEqual(self.log.state, 'warning')
        finally:
            os.unlink(path)

    def test_verify_l2_failed_missing_critical_tables(self):
        """TOC 缺少 res_company 等关键表时，verify_l2='failed'。"""
        path = self._make_temp_file()
        toc_output = self._make_toc_output(count=150, include_critical=False)
        try:
            with patch(f'{MODULE_PATH}.subprocess') as mock_sub:
                mock_sub.run.return_value = MagicMock(
                    returncode=0, stdout=toc_output, stderr=''
                )
                self.backup._verify_level2(self.log, path)
            self.assertEqual(self.log.verify_l2, 'failed')
            self.assertEqual(self.log.state, 'warning')
        finally:
            os.unlink(path)

    def test_verify_l2_file_missing(self):
        """文件不存在时，verify_l2='skipped'。"""
        self.backup._verify_level2(self.log, '/nonexistent/path/file.dump')
        self.assertEqual(self.log.verify_l2, 'skipped')

    # -------------------------------------------------------------------------
    # Level 2 zip format tests
    # -------------------------------------------------------------------------

    def _make_valid_zip(self):
        """创建包含 dump.sql 和 manifest.json 的合法 zip 文件。"""
        fd, path = tempfile.mkstemp(suffix='.zip')
        os.close(fd)
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('dump.sql', 'fake sql content')
            zf.writestr('manifest.json', '{"version": "19.0"}')
        return path

    def _make_invalid_zip_missing_files(self):
        """创建缺少必要文件的 zip。"""
        fd, path = tempfile.mkstemp(suffix='.zip')
        os.close(fd)
        with zipfile.ZipFile(path, 'w') as zf:
            zf.writestr('other_file.txt', 'content')
        return path

    def _make_non_zip_file_with_zip_ext(self):
        """创建扩展名为 .zip 但内容不是 zip 的文件。"""
        fd, path = tempfile.mkstemp(suffix='.zip')
        os.write(fd, b'this is not a zip file')
        os.close(fd)
        return path

    def test_verify_l2_zip_passed(self):
        """合法 zip 含 dump.sql + manifest.json，verify_l2='passed'。"""
        path = self._make_valid_zip()
        try:
            self.backup._verify_level2(self.log, path)
            self.assertEqual(self.log.verify_l2, 'passed')
        finally:
            os.unlink(path)

    def test_verify_l2_zip_missing_required_files(self):
        """zip 缺少 dump.sql 或 manifest.json，verify_l2='failed'。"""
        path = self._make_invalid_zip_missing_files()
        try:
            self.backup._verify_level2(self.log, path)
            self.assertEqual(self.log.verify_l2, 'failed')
            self.assertEqual(self.log.state, 'warning')
        finally:
            os.unlink(path)

    def test_verify_l2_zip_not_valid_zip(self):
        """文件后缀是 .zip 但内容无效，verify_l2='failed'。"""
        path = self._make_non_zip_file_with_zip_ext()
        try:
            self.backup._verify_level2(self.log, path)
            self.assertEqual(self.log.verify_l2, 'failed')
            self.assertEqual(self.log.state, 'warning')
        finally:
            os.unlink(path)

    def test_verify_l2_zip_toc_count_recorded(self):
        """zip 验证通过后，verify_l2_toc 记录了文件数量。"""
        path = self._make_valid_zip()
        try:
            self.backup._verify_level2(self.log, path)
            self.assertEqual(self.log.verify_l2, 'passed')
            # 2 个文件：dump.sql + manifest.json
            self.assertEqual(self.log.verify_l2_toc, 2)
        finally:
            os.unlink(path)
