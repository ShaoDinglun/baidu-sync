import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend.storage import BaiduStorage


class TargetDirectoryDedupTest(unittest.TestCase):
    def setUp(self):
        self.storage = BaiduStorage.__new__(BaiduStorage)
        self.storage.client = Mock()
        self.storage.config = {
            'baidu': {'current_user': 'test', 'users': {'test': {'cookies': 'unused'}}},
        }
        self.storage._list_dir_entries_with_fallback = Mock(return_value=[])

    def test_same_filename_in_other_market_does_not_hide_missing_file(self):
        cache = {}
        self.storage._list_dir_entries_with_fallback.side_effect = [
            [{'path': '/target/沪深/2026-11/20261116.zip', 'isdir': 0}], [],
        ]
        self.assertTrue(self.storage._target_file_exists('/target/沪深/2026-11/20261116.zip', cache))
        self.assertFalse(self.storage._target_file_exists('/target/京市/2026-11/20261116.zip', cache))
        self.assertEqual(self.storage._list_dir_entries_with_fallback.call_count, 2)

    def test_files_in_same_target_directory_share_one_listing(self):
        self.storage._list_dir_entries_with_fallback.return_value = [
            {'path': '/target/20261116.zip', 'isdir': 0},
        ]
        cache = {}
        self.assertTrue(self.storage._target_file_exists('/target/20261116.zip', cache))
        self.assertFalse(self.storage._target_file_exists('/target/20261117.zip', cache))
        self.storage._list_dir_entries_with_fallback.assert_called_once()

    def test_listing_failure_is_not_empty_and_is_not_cached(self):
        self.storage._list_dir_entries_with_fallback.side_effect = ValueError('invalid response')
        cache = {}
        with self.assertRaisesRegex(ValueError, 'invalid response'):
            self.storage._target_file_exists('/target/file.zip', cache)
        self.assertEqual(cache, {})

    def test_confirmed_missing_directory_is_empty(self):
        self.storage._list_dir_entries_with_fallback.side_effect = RuntimeError('error_code: 31066')
        self.assertFalse(self.storage._target_file_exists('/target/file.zip', {}))

    def test_legacy_recursive_scan_propagates_root_and_child_errors(self):
        for responses in [
            [ValueError('invalid response')],
            [[{'path': '/target/history', 'isdir': 1}], ValueError('invalid response')],
        ]:
            with self.subTest(responses=responses):
                self.storage._list_dir_entries_with_fallback.side_effect = responses
                with self.assertRaisesRegex(ValueError, 'invalid response'):
                    self.storage.list_local_files('/target')

    def test_legacy_scan_accepts_empty_and_confirmed_missing_root(self):
        self.assertEqual(self.storage.list_local_files('/target'), [])
        self.storage._list_dir_entries_with_fallback.side_effect = RuntimeError('error_code: 31066')
        self.assertEqual(self.storage.list_local_files('/target'), [])

    def test_newly_appeared_target_stops_transfer_but_explicit_overwrite_is_preserved(self):
        item = {'clean_path': 'month/file.zip', 'final_path': 'month/file.zip'}
        with self.assertRaisesRegex(RuntimeError, '重新同步'):
            self.storage._assert_transfer_targets_available([item], {'file.zip': '/target/file.zip'})
        self.storage._assert_transfer_targets_available(
            [{**item, 'overwrite_existing': True}], {'file.zip': '/target/file.zip'})

    def run_transfer(self, entries, dates=None, rename=False):
        storage = self.storage
        dates = dates or ['20261116']
        storage._parse_cookies = Mock(return_value={})
        storage._validate_cookies = Mock(return_value=True)
        storage._access_and_get_shared_paths_with_retry = Mock(return_value=[
            SimpleNamespace(is_dir=True, path='/root', uk=1, share_id=2, bdstoken='unused')])
        storage._list_shared_dir_files = Mock(return_value=[
            {'path': f'root/京市/2026-11/{day}.zip', 'fs_id': i}
            for i, day in enumerate(dates, start=1)])
        if rename:
            storage._apply_regex_rules = Mock(return_value=(True, '京市/2026-11/renamed.zip'))
        storage.list_local_files = Mock(side_effect=AssertionError('不应扫描整个历史目录'))
        storage._list_dir_entries_with_fallback.side_effect = entries
        storage._ensure_dir_tree_exists = Mock(return_value=True)
        storage._transfer_shared_paths_with_retry = Mock()
        with patch('backend.storage.BaiduPCSApi') as client:
            storage.test_client = client.return_value
            return storage.transfer_share(
                'https://example.invalid', save_dir='/target',
                task_config={'sync_mode': 'incremental', 'sync_scope_type': 'all', 'overwrite_policy': 'never'})

    def test_existing_target_skips_transfer_without_scanning_history(self):
        dates = ['20261102', '20261103', '20261104', '20261105', '20261106', '20261109',
                 '20261110', '20261111', '20261112', '20261113', '20261116']
        result = self.run_transfer([[
            {'path': f'/target/京市/2026-11/{day}.zip', 'isdir': 0} for day in dates]], dates=dates)
        self.assertTrue(result['success'], result)
        self.assertTrue(result['skipped'], result)
        self.storage._transfer_shared_paths_with_retry.assert_not_called()
        self.storage.list_local_files.assert_not_called()
        self.storage._list_dir_entries_with_fallback.assert_called_once()

    def test_target_listing_error_stops_transfer(self):
        result = self.run_transfer([ValueError('invalid response')])
        self.assertFalse(result['success'], result)
        self.storage._transfer_shared_paths_with_retry.assert_not_called()

    def test_target_appearing_before_submission_stops_transfer(self):
        result = self.run_transfer([[], [{'path': '/target/京市/2026-11/20261116.zip', 'isdir': 0}]])
        self.assertFalse(result['success'], result)
        self.assertIn('重新同步', result['error'])
        self.storage._transfer_shared_paths_with_retry.assert_not_called()

    def test_new_target_is_transferred_to_its_exact_directory(self):
        with patch('backend.storage.time.sleep'):
            result = self.run_transfer([[], []])
        self.assertTrue(result['success'], result)
        self.storage._transfer_shared_paths_with_retry.assert_called_once()
        call = self.storage._transfer_shared_paths_with_retry.call_args.kwargs
        self.assertEqual(call['remotedir'], '/target/京市/2026-11')
        self.assertEqual(call['fs_ids'], [1])

    def test_existing_nested_file_is_renamed_in_its_own_directory(self):
        with patch('backend.storage.time.sleep'):
            result = self.run_transfer(
                [[{'path': '/target/京市/2026-11/20261116.zip', 'isdir': 0}]], rename=True)
        self.assertTrue(result['success'], result)
        self.storage.test_client.rename.assert_called_once_with(
            '/target/京市/2026-11/20261116.zip', '/target/京市/2026-11/renamed.zip')
        self.storage._transfer_shared_paths_with_retry.assert_not_called()


if __name__ == '__main__':
    unittest.main()
