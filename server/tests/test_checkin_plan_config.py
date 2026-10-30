"""random 签到计划：配置校验 + /status 账号字段映射。"""
from __future__ import annotations

import unittest

from server.services import wb2api


class CheckinPlanConfigTest(unittest.TestCase):
    def test_sanitize_valid(self) -> None:
        out = wb2api._sanitize_section('schedule', {
            'checkin_mode': ' RANDOM ',
            'checkin_window_start': '08:00',
            'checkin_window_end': '09:30',
            'checkin_retry_start': '09:30',
            'checkin_retry_end': '10:00',
            'checkin_state_file': ' data/checkin-plan.json ',
        })
        self.assertEqual(out['checkin_mode'], 'random')
        self.assertEqual(out['checkin_window_start'], '08:00')
        self.assertEqual(out['checkin_state_file'], 'data/checkin-plan.json')

    def test_sanitize_rejects_bad_mode(self) -> None:
        with self.assertRaises(ValueError):
            wb2api._sanitize_section('schedule', {'checkin_mode': 'nope'})

    def test_sanitize_rejects_bad_time(self) -> None:
        for key in ('checkin_window_start', 'checkin_window_end',
                    'checkin_retry_start', 'checkin_retry_end'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                wb2api._sanitize_section('schedule', {key: '25:00'})

    def test_merge_pool_status_copies_plan(self) -> None:
        accounts = [{'uid': 'u1'}]
        status = {'accounts': [{
            'uid': 'u1',
            'checkin_plan_at': '2026-10-30T08:12:00+08:00',
            'checkin_attempts': 1,
            'checkin_state': 'failed',
        }]}
        out = wb2api.merge_pool_status(accounts, status)
        self.assertEqual(out[0]['checkin_plan_at'], '2026-10-30T08:12:00+08:00')
        self.assertEqual(out[0]['checkin_attempts'], 1)
        self.assertEqual(out[0]['checkin_state'], 'failed')

    def test_merge_pool_status_defaults_none(self) -> None:
        out = wb2api.merge_pool_status([{'uid': 'u1'}], {'accounts': []})
        self.assertIsNone(out[0]['checkin_plan_at'])
        self.assertIsNone(out[0]['checkin_attempts'])
        self.assertIsNone(out[0]['checkin_state'])


if __name__ == '__main__':
    unittest.main()
