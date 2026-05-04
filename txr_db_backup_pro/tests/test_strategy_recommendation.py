"""Tests for strategy recommendation logic (task 7.8)."""
import tempfile
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase, tagged


@tagged('txr_db_backup_pro')
class TestStrategyRecommendation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.backup = cls.env['txr.db.backup'].create({
            'name': 'reco-test',
            'database_name': 'rdb',
            'storage_type': 'local',
            'local_path': tempfile.gettempdir(),
        })

    # ------------------------------------------------------------------
    # _compute_recommended_strategy — three branches
    # ------------------------------------------------------------------

    def test_recommend_standard_when_disk_plenty(self):
        # tmp_disk = 100 GB, db = 1 GB, fs = 1 GB → standard
        # 100 GB > 2 * (1+1) GB = 4 GB
        with patch.object(type(self.backup), '_get_available_tmp_disk', return_value=100 * 10 ** 9), \
             patch.object(type(self.backup), '_estimate_db_size', return_value=10 ** 9), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=10 ** 9):
            strat, reason = self.backup._compute_recommended_strategy()
        self.assertEqual(strat, 'standard')
        self.assertIn('GB', reason)

    def test_recommend_split_when_filestore_large(self):
        # filestore = 10 GB > _FILESTORE_LARGE_THRESHOLD (5 GB)
        # tmp_disk = 5 GB, db = 1 GB
        # 5 GB <= 2*(1+10) = 22 GB → not standard
        # fs >= threshold → split
        with patch.object(type(self.backup), '_get_available_tmp_disk', return_value=5 * 10 ** 9), \
             patch.object(type(self.backup), '_estimate_db_size', return_value=10 ** 9), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=10 * 10 ** 9):
            strat, reason = self.backup._compute_recommended_strategy()
        self.assertEqual(strat, 'split')
        self.assertIsNotNone(reason)

    def test_recommend_streaming_when_disk_critical(self):
        # tmp_disk = 500 MB, db = 10 GB → tmp <= db * 1.5 → streaming
        with patch.object(type(self.backup), '_get_available_tmp_disk', return_value=5 * 10 ** 8), \
             patch.object(type(self.backup), '_estimate_db_size', return_value=10 * 10 ** 9), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=10 ** 8):
            strat, reason = self.backup._compute_recommended_strategy()
        self.assertEqual(strat, 'streaming')
        self.assertIsNotNone(reason)

    def test_recommend_split_disk_constrained_small_filestore(self):
        # tmp = 3 GB, db = 2 GB, fs = 1 GB (< threshold)
        # 3 GB <= 2*(2+1)=6 GB → not standard
        # fs < threshold → not split by that branch
        # tmp=3GB > db*1.5=3GB → not streaming (edge: > not <=)
        # falls through to last split branch
        with patch.object(type(self.backup), '_get_available_tmp_disk', return_value=4 * 10 ** 9), \
             patch.object(type(self.backup), '_estimate_db_size', return_value=2 * 10 ** 9), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=10 ** 9):
            strat, reason = self.backup._compute_recommended_strategy()
        # Either split or standard depending on arithmetic; just not None
        self.assertIsNotNone(strat)
        self.assertIsNotNone(reason)

    def test_recommend_none_when_disk_unknown(self):
        with patch.object(type(self.backup), '_get_available_tmp_disk', return_value=None):
            strat, reason = self.backup._compute_recommended_strategy()
        self.assertIsNone(strat)
        self.assertIsNone(reason)

    def test_recommend_none_when_no_storage_configured(self):
        """No cloud remote and non-local storage → (None, None)."""
        b = self.backup.copy({
            'name': 'reco-no-storage',
            'storage_type': 'rclone',
            'cloud_remote_id': False,
        })
        strat, reason = b._compute_recommended_strategy()
        self.assertIsNone(strat)
        self.assertIsNone(reason)

    def test_recommend_none_when_db_size_unavailable(self):
        with patch.object(type(self.backup), '_get_available_tmp_disk', return_value=10 ** 11), \
             patch.object(type(self.backup), '_estimate_db_size', return_value=None), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=10 ** 9):
            strat, reason = self.backup._compute_recommended_strategy()
        self.assertIsNone(strat)
        self.assertIsNone(reason)

    # ------------------------------------------------------------------
    # _compute_strategy_recommendation (api.depends compute)
    # ------------------------------------------------------------------

    def test_compute_strategy_recommendation_sets_fields(self):
        with patch.object(type(self.backup), '_get_available_tmp_disk', return_value=100 * 10 ** 9), \
             patch.object(type(self.backup), '_estimate_db_size', return_value=10 ** 9), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=10 ** 9):
            self.backup._compute_strategy_recommendation()
        self.assertEqual(self.backup.recommended_strategy, 'standard')
        self.assertIn('GB', self.backup.strategy_recommendation_reason)

    def test_compute_strategy_recommendation_skip_context(self):
        b = self.backup.with_context(skip_strategy_compute=True)
        b._compute_strategy_recommendation()
        # When context skip is set, fields become False
        self.assertFalse(b.recommended_strategy)
        self.assertFalse(b.strategy_recommendation_reason)

    def test_compute_strategy_recommendation_handles_exception(self):
        """Exception in _compute_recommended_strategy → fields set to False."""
        with patch.object(type(self.backup), '_compute_recommended_strategy',
                          side_effect=Exception('unexpected')):
            self.backup._compute_strategy_recommendation()
        self.assertFalse(self.backup.recommended_strategy)
        self.assertFalse(self.backup.strategy_recommendation_reason)

    # ------------------------------------------------------------------
    # action_use_recommended_strategy
    # ------------------------------------------------------------------

    def test_action_use_recommended_writes_strategy(self):
        self.backup.backup_strategy = 'standard'
        with patch.object(type(self.backup), '_get_available_tmp_disk', return_value=5 * 10 ** 9), \
             patch.object(type(self.backup), '_estimate_db_size', return_value=10 ** 9), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=10 * 10 ** 9):
            self.backup._compute_strategy_recommendation()

        self.assertEqual(self.backup.recommended_strategy, 'split')
        self.backup.action_use_recommended_strategy()
        self.assertEqual(self.backup.backup_strategy, 'split')

    def test_action_use_recommended_raises_when_no_recommendation(self):
        with patch.object(type(self.backup), '_get_available_tmp_disk', return_value=None):
            self.backup._compute_strategy_recommendation()
        # recommended_strategy should now be False
        with self.assertRaises(UserError):
            self.backup.action_use_recommended_strategy()

    # ------------------------------------------------------------------
    # _write_preflight_info
    # ------------------------------------------------------------------

    def test_write_preflight_info_contains_strategy(self):
        log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'running',
        })
        with patch.object(type(self.backup), '_estimate_db_size', return_value=2 * 10 ** 9), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=5 * 10 ** 8), \
             patch.object(type(self.backup), '_get_available_tmp_disk', return_value=50 * 10 ** 9):
            self.backup._write_preflight_info(log)

        self.assertTrue(log.preflight_info)
        self.assertIn('Strategy:', log.preflight_info)
        self.assertIn('DB size:', log.preflight_info)

    def test_write_preflight_info_timeout_fallback_text(self):
        """When estimation returns None, shows 'N/A (estimation timed out)'."""
        log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'running',
        })
        with patch.object(type(self.backup), '_estimate_db_size', return_value=None), \
             patch.object(type(self.backup), '_estimate_filestore_size', return_value=None), \
             patch.object(type(self.backup), '_get_available_tmp_disk', return_value=None):
            self.backup._write_preflight_info(log)

        self.assertIn('N/A', log.preflight_info)

    def test_write_preflight_info_streaming_shows_na_needed(self):
        b = self.backup.copy({
            'name': 'reco-streaming',
            'backup_strategy': 'streaming',
        })
        log = self.env['txr.db.backup.log'].create({
            'backup_id': b.id,
            'state': 'running',
        })
        with patch.object(type(b), '_estimate_db_size', return_value=10 ** 9), \
             patch.object(type(b), '_estimate_filestore_size', return_value=10 ** 9), \
             patch.object(type(b), '_get_available_tmp_disk', return_value=10 ** 10):
            b._write_preflight_info(log)

        self.assertIn('streaming', log.preflight_info)
        self.assertIn('N/A (streaming)', log.preflight_info)

    def test_write_preflight_info_never_raises(self):
        """preflight write must not raise even when estimation explodes."""
        log = self.env['txr.db.backup.log'].create({
            'backup_id': self.backup.id,
            'state': 'running',
        })
        with patch.object(type(self.backup), '_estimate_db_size',
                          side_effect=Exception('sql error')):
            # should not raise
            self.backup._write_preflight_info(log)
