"""fanctl の制御ロジックのテスト。  python3 -m pytest または python3 test_fanctl.py"""

import json
import time
import unittest
from unittest import mock

import fanctl
from fanctl import (
    BlasterSender,
    Controller,
    DEFAULT_CONFIG,
    DryRunSender,
    call_with_hard_timeout,
    curve_duty,
    parse_read_output,
    round_up_10,
    send_with_hard_timeout,
    should_transmit,
)


def cfg(**over):
    c = dict(DEFAULT_CONFIG)
    c.update(over)
    return c


class CurveTest(unittest.TestCase):
    def test_round_up(self):
        self.assertEqual(round_up_10(30), 30)
        self.assertEqual(round_up_10(31), 40)
        self.assertEqual(round_up_10(39.9), 40)
        self.assertEqual(round_up_10(0), 0)

    def test_curve_ends(self):
        c = cfg()
        self.assertEqual(curve_duty(5, c["curve"], 30, 100), 30)
        self.assertEqual(curve_duty(10, c["curve"], 30, 100), 30)
        self.assertEqual(curve_duty(90, c["curve"], 30, 100), 100)
        self.assertEqual(curve_duty(95, c["curve"], 30, 100), 100)

    def test_curve_interp_rounds_up(self):
        c = cfg()
        # 25W: 10→40 (30%→50%) の中間 = 36.67 → 切り上げ 40
        self.assertEqual(curve_duty(25, c["curve"], 30, 100), 40)
        # 55W: 40→70 (50%→80%) の中間 = 65 → 切り上げ 70
        self.assertEqual(curve_duty(55, c["curve"], 30, 100), 70)
        # 80W: 70→90 (80%→100%) の中間 = 90 → そのまま 90
        self.assertEqual(curve_duty(80, c["curve"], 30, 100), 90)

    def test_min_clamp(self):
        self.assertEqual(curve_duty(10, [[50, 0], [80, 100]], 30, 100), 30)


class ControllerTest(unittest.TestCase):
    def test_down_is_immediate(self):
        # 起動直後は 100%。電力が低ければ待たずにすぐ下がる
        ctl = Controller(cfg(up_hold_s=180))
        self.assertEqual(ctl.step(20, 50, 0), 40)   # 20W の目標は 40%
        self.assertEqual(ctl.step(10, 50, 20), 30)  # さらに下がる

    def test_unknown_power_is_full(self):
        ctl = Controller(cfg(up_hold_s=0, up_step_s=0))
        ctl.duty = 30
        self.assertEqual(ctl.step(None, 50, 0), 100)
        self.assertIsNone(ctl.raise_since)
        # 復帰後は通常のロジックに戻る (下げは即時なので目標へ一気に下がる)
        self.assertEqual(ctl.step(20, 50, 20), 40)

    def test_up_waits_then_steps(self):
        c = cfg(up_hold_s=60, up_step_s=20, interval_s=20)
        ctl = Controller(c)
        ctl.duty = 30
        # t=0 で高負荷 (90W, 目標100%)。hold の間 (0..60) は据え置き
        t = 0
        while t < 60:
            self.assertEqual(ctl.step(90, 50, t), 30, f"t={t}")
            t += 20
        # hold 経過 → 1 段上がる
        self.assertEqual(ctl.step(90, 50, 60), 40)
        # 次の段は up_step_s (20s) 後
        self.assertEqual(ctl.step(90, 50, 80), 50)
        self.assertEqual(ctl.step(90, 50, 100), 60)
        for t in (120, 140, 160, 180):
            ctl.step(90, 50, t)
        self.assertEqual(ctl.duty, 100)

    def test_up_never_above_desired(self):
        c = cfg(up_hold_s=0, up_step_s=0)
        ctl = Controller(c)
        ctl.duty = 30
        ctl.step(70, 50, 1)   # desired = 80
        ctl.step(70, 50, 2)
        self.assertEqual(ctl.duty, 40)
        for i in range(10):
            ctl.step(70, 50, 3 + i)
        self.assertEqual(ctl.duty, 80)

    def test_cooldown_cancels_ascent(self):
        ctl = Controller(cfg(up_hold_s=100))
        ctl.duty = 60
        ctl.step(90, 50, 0)          # 上げ待ち開始 (目標100%)
        ctl.step(20, 50, 50)         # 負荷低下 (目標40%) → 下げは即時、待ちもリセット
        self.assertEqual(ctl.duty, 40)
        self.assertIsNone(ctl.raise_since)
        # 再度上げるにはまた hold から
        self.assertEqual(ctl.step(90, 50, 60), 40)   # まだ据え置き
        self.assertEqual(ctl.step(90, 50, 160), 50)

    def test_alert_after_hot_cycles(self):
        ctl = Controller(cfg(alert_temp=85, alert_cycles=3, safety_temp_c=1000))
        for i in range(2):
            ctl.step(90, 90, i)
            self.assertFalse(ctl.should_alert())
        ctl.step(90, 90, 2)
        self.assertTrue(ctl.should_alert())
        self.assertFalse(ctl.should_alert())   # 一度だけ
        ctl.step(90, 70, 3)                     # 冷えたらリセット
        self.assertEqual(ctl.hot_cycles, 0)

    def test_safety_override_forces_full_regardless_of_power(self):
        # 電力は低くても、温度が safety_temp_c 以上なら即座に 100% (待たない)
        ctl = Controller(cfg(safety_temp_c=80))
        ctl.duty = 30
        self.assertEqual(ctl.step(10, 80, 0), 100)
        self.assertTrue(ctl.safety_active)
        self.assertIsNone(ctl.raise_since)

    def test_safety_override_releases_below_threshold(self):
        c = cfg(safety_temp_c=80, up_hold_s=0, up_step_s=0)
        ctl = Controller(c)
        self.assertEqual(ctl.step(10, 85, 0), 100)   # 安全弁で強制 100%
        self.assertTrue(ctl.safety_active)
        # 温度が下がれば通常の電力ベースの制御に戻る。下げは即時なので 10W の目標 (30%) へ一気に下がる
        self.assertEqual(ctl.step(10, 70, 1), 30)
        self.assertFalse(ctl.safety_active)


class ShouldTransmitTest(unittest.TestCase):
    def test_first_send_always(self):
        self.assertTrue(should_transmit(50, None, None, 0, 60, False))

    def test_unchanged_within_interval_skips(self):
        self.assertFalse(should_transmit(50, 50, 0, 30, 60, False))

    def test_unchanged_after_interval_resends(self):
        self.assertTrue(should_transmit(50, 50, 0, 60, 60, False))

    def test_change_always_sends(self):
        self.assertTrue(should_transmit(60, 50, 0, 5, 60, False))

    def test_safety_active_ignores_interval(self):
        self.assertTrue(should_transmit(100, 100, 0, 1, 60, True))


class HardTimeoutSenderTest(unittest.TestCase):
    class _FastSender:
        def send(self, pct):
            return True

    class _HangingSender:
        def send(self, pct):
            time.sleep(5)  # テストのタイムアウト (0.1s) より十分長い
            return True

    class _RaisingSender:
        def send(self, pct):
            raise RuntimeError("boom")

    def test_normal_send_returns_promptly(self):
        self.assertTrue(send_with_hard_timeout(self._FastSender(), 50, 1.0))

    def test_hanging_send_times_out_and_returns_false(self):
        start = time.monotonic()
        ok = send_with_hard_timeout(self._HangingSender(), 50, 0.1)
        elapsed = time.monotonic() - start
        self.assertFalse(ok)
        self.assertLess(elapsed, 1.0)  # ハング元 (5秒) を待たされていないこと

    def test_raising_send_returns_false(self):
        self.assertFalse(send_with_hard_timeout(self._RaisingSender(), 50, 1.0))

    def test_generic_call_with_hard_timeout(self):
        self.assertEqual(call_with_hard_timeout(lambda: 42, 1.0), 42)
        self.assertIsNone(call_with_hard_timeout(lambda: (_ for _ in ()).throw(RuntimeError()), 1.0))


class ReadSensorTest(unittest.TestCase):
    def test_read_sensor_parses_value(self):
        s = BlasterSender("http://x", 1)
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = json.dumps({"id": "sensor-atom_internal_temp", "value": 42.5}).encode()
        fake_resp.__enter__.return_value = fake_resp
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            self.assertEqual(s.read_sensor("ATOM internal temperature"), 42.5)

    def test_read_sensor_network_error_returns_none(self):
        s = BlasterSender("http://x", 1)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
            self.assertIsNone(s.read_sensor("ATOM internal temperature"))

    def test_read_sensor_bad_json_returns_none(self):
        s = BlasterSender("http://x", 1)
        fake_resp = mock.MagicMock()
        fake_resp.read.return_value = b"not json"
        fake_resp.__enter__.return_value = fake_resp
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            self.assertIsNone(s.read_sensor("ATOM internal temperature"))

    def test_dry_run_sender_read_sensor_is_none(self):
        self.assertIsNone(DryRunSender().read_sensor("anything"))


class ParseTest(unittest.TestCase):
    def test_parse(self):
        out = "POWER\n55.5\nGPU\n44\nZONES\n45800\n43600\n\nEND\n"
        power, gpus, zones = parse_read_output(out)
        self.assertEqual(power, [55.5])
        self.assertEqual(gpus, [44.0])
        self.assertEqual(zones, [45.8, 43.6])

    def test_parse_no_power_or_gpu(self):
        power, gpus, zones = parse_read_output("GPU\nZONES\n50000\nEND\n")
        self.assertEqual(power, [])
        self.assertEqual(gpus, [])
        self.assertEqual(zones, [50.0])

    def test_parse_garbage(self):
        power, gpus, zones = parse_read_output("POWER\nN/A\nGPU\nN/A\nZONES\nEND\n")
        self.assertEqual(power, [])
        self.assertEqual(gpus, [])


class SenderTest(unittest.TestCase):
    def test_url(self):
        s = fanctl.BlasterSender("http://x/", 1)
        self.assertEqual(s.base, "http://x")

    def test_send_number_hits_expected_url(self):
        s = fanctl.BlasterSender("http://x", 1)
        fake_resp = mock.MagicMock()
        fake_resp.__enter__.return_value = fake_resp
        with mock.patch("urllib.request.urlopen", return_value=fake_resp) as m:
            self.assertTrue(s.send_number("DGX temp", 42.3))
        called_url = m.call_args[0][0].full_url
        self.assertIn("http://x/number/DGX%20temp/set?", called_url)
        self.assertIn("value=42.3", called_url)

    def test_send_delegates_to_send_number(self):
        s = fanctl.BlasterSender("http://x", 1)
        with mock.patch.object(s, "send_number", return_value=True) as m:
            self.assertTrue(s.send(70))
        m.assert_called_once_with("Fan duty", 70)

    def test_dry_run_send_number_returns_true(self):
        self.assertTrue(fanctl.DryRunSender().send_number("DGX power", 12.3))


if __name__ == "__main__":
    unittest.main()
