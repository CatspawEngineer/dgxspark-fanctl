#!/usr/bin/env python3
"""fanctl — DGX Spark 2 ノードの GPU 消費電力 (W) に合わせてファン回転数 (%) を決め、
ESPHome の赤外線送信機 (fanblaster) に送る常駐プログラム。

依存: Python 3.8+ 標準ライブラリのみ。
設定: JSON ファイル (既定 /etc/fanctl.json)。無ければ内蔵の既定値で動く。

動き:
  1. 各ノードで nvidia-smi を読み、GPU 消費電力 (W) と温度 (℃) を取る
  2. 電力ベースのファンカーブで目標 % を決める (10 刻み)
  3. 下げるときは即時、上げるときは一定時間待ってから 10% ずつ
     (急に静かにするのは気にならないが、急に大きくなる音は気になるため)
  4. 送信は % が変わった時だけ (毎回送ると赤外線 LED が光って気になるため)。
     ただし信号取りこぼしの自己修復として resend_interval_s ごとに送り直す。
     安全弁作動中だけは、届いているか特に重要なので毎周期送る
  5. 安全弁: 温度が safety_temp_c 以上なら 電力値を無視して即座に 100%
     (騒音より安全優先。ここだけは待たない)
  6. 電力が読めなければ 100%。停止時 (SIGTERM) も 100% を送ってから終わる
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("fanctl")

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict = {
    # 温度・電力を読むノード。"ssh" が null なら自ノードで直接実行
    "nodes": [
        {"name": "OmoikaneOkami", "ssh": None},
        {"name": "node2", "ssh": "user@node2"},
    ],
    # /sys/class/thermal (acpitz 等、マザーボード/チップセット系でGPU温度とは限らない)
    # も安全弁・DGX温度報告の候補に混ぜるか。false なら GPU 温度 (nvidia-smi) だけに絞る
    "include_thermal_zones": False,
    # ssh / nvidia-smi のタイムアウト (秒)
    "read_timeout_s": 8,

    # fanblaster の URL (ESPHome web_server)
    "blaster_url": "http://fanblaster.local",
    "blaster_timeout_s": 5,
    # ATOM Lite 自身のチップ内蔵温度センサーの名前。ログに乗せて機器自体の熱も見張る。
    # null/空文字にすると読みに行かない (fanblaster.yaml 側にセンサーが無い旧ファーム互換用)
    "m5_temp_sensor_name": "ATOM internal temperature",
    # DGX Spark 側の温度・消費電力を M5 (ATOM Lite) にも送って向こうのログ/Web UI に出す。
    # ファン制御そのものには使わない (daemon 側で完結)。false で送らない
    "report_dgx_metrics_to_m5": True,
    # M5 自身の内蔵温度読み取り・DGX 側の値の送信 (上記2つ) をどの間隔で行うか (秒)。
    # ファン制御に使う Fan duty 送信とは無関係の、単なる表示・ログ用のおまけなので、
    # interval_s 毎周期やると ESP32 側の HTTP 接続数 (ソケット) を圧迫して
    # "httpd_accept_conn: error in accept" のような枯渇を招く。resend_interval_s 程度で十分
    "m5_report_interval_s": 60,

    # ファンカーブ: [消費電力W, 回転数%] の折れ線。間は直線補間して 10 刻みに切り上げ
    # 較正値: アイドル ~4W, 高負荷 ~90W (2026-09-17 実測)
    "curve": [[10, 30], [40, 50], [70, 80], [90, 100]],
    "min_duty": 30,
    "max_duty": 100,

    # 安全弁: 電力値によらず、温度がこれ以上なら即座に強制 100%
    "safety_temp_c": 80,

    # 制御周期 (秒)。短すぎると ESP32 側の HTTP 接続数を圧迫してソケット枯渇
    # ("httpd_accept_conn: error in accept") を招きやすくなるため 30 秒に
    "interval_s": 30,
    # % が変わらない限り送らない。ただし赤外線が届かなかった時の自己修復のため、この間隔
    # (秒) が経ったら変化がなくても送り直す。安全弁作動中はこれに関係なく毎周期送る
    "resend_interval_s": 60,
    # 上げる前に「上げるべき状態」が続くべき時間 (秒)。下げは即時なのでこちらだけ待つ。
    # 急な負荷上昇に対して長く待ちすぎると温度上昇に追いつかない (安全弁はあるが、その手前で追従したい)
    "up_hold_s": 60,
    # 上げ始めてからの 1 段 (10%) ごとの間隔 (秒)。interval_s と同じにして毎周期上げる
    "up_step_s": 30,

    # 100% なのに温度がこれ以上の周期が alert_cycles 回続いたら警告 (信号未達の疑い)
    "alert_temp": 85,
    "alert_cycles": 6,
    # 警告時に実行するコマンド (空なら何もしない)。例: "curl -fsS -X POST https://ntfy.sh/xxx -d 'fan alert'"
    "alert_cmd": "",
}


def load_config(path: Optional[str]) -> Dict:
    cfg = dict(DEFAULT_CONFIG)
    if path:
        with open(path, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


# ---------------------------------------------------------------------------
# 電力・温度の読み取り
# ---------------------------------------------------------------------------

# ノード上で実行するシェル片。GPU 消費電力・GPU温度・thermal zone を区切り付きで出す
READ_SCRIPT = (
    "echo POWER; nvidia-smi --query-gpu=power.draw --format=csv,noheader,nounits 2>/dev/null; "
    "echo GPU; nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits 2>/dev/null; "
    "echo ZONES; cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null; echo END"
)


def parse_read_output(text: str) -> Tuple[List[float], List[float], List[float]]:
    """READ_SCRIPT の出力を (消費電力[W], GPU温度[℃], zone温度[℃]) に分ける。"""
    power: List[float] = []
    gpus: List[float] = []
    zones: List[float] = []
    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if line == "POWER":
            section = "power"
            continue
        if line == "GPU":
            section = "gpu"
            continue
        if line == "ZONES":
            section = "zones"
            continue
        if line == "END":
            break
        if not line:
            continue
        try:
            v = float(line)
        except ValueError:
            continue
        if section == "power":
            power.append(v)
        elif section == "gpu":
            gpus.append(v)
        elif section == "zones":
            zones.append(v / 1000.0)
    return power, gpus, zones


def read_node_metrics(node: Dict, cfg: Dict) -> Tuple[Optional[float], Optional[float]]:
    """1 ノードの (代表消費電力W, 代表温度℃) を返す。読めなければ None。"""
    if node.get("ssh"):
        cmd = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            node["ssh"], READ_SCRIPT,
        ]
    else:
        cmd = ["sh", "-c", READ_SCRIPT]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=cfg["read_timeout_s"], check=False
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("%s: read failed: %s", node["name"], e)
        return None, None
    power, gpus, zones = parse_read_output(out.stdout)
    if not power and not gpus:
        log.warning("%s: no nvidia-smi output (rc=%s, stderr=%s)",
                    node["name"], out.returncode, out.stderr.strip()[:200])
        return None, None
    p = max(power) if power else None
    temp_candidates = list(gpus)
    if cfg.get("include_thermal_zones") and zones:
        temp_candidates.extend(zones)
    t = max(temp_candidates) if temp_candidates else None
    log.debug("%s: power=%s gpu_temp=%s zones_max=%s -> P=%s T=%s",
              node["name"], power, gpus, max(zones) if zones else None, p, t)
    return p, t


def read_cluster_metrics(
    cfg: Dict,
) -> Tuple[Optional[float], Dict[str, Optional[float]], Optional[float], Dict[str, Optional[float]]]:
    """全ノードの (最大消費電力, ノード別電力, 最高温度, ノード別温度) を返す。全滅なら None。"""
    per_power: Dict[str, Optional[float]] = {}
    per_temp: Dict[str, Optional[float]] = {}
    for node in cfg["nodes"]:
        p, t = read_node_metrics(node, cfg)
        per_power[node["name"]] = p
        per_temp[node["name"]] = t
    known_power = [v for v in per_power.values() if v is not None]
    known_temp = [v for v in per_temp.values() if v is not None]
    max_power = max(known_power) if known_power else None
    max_temp = max(known_temp) if known_temp else None
    return max_power, per_power, max_temp, per_temp


# ---------------------------------------------------------------------------
# 制御ロジック (副作用なし。テストしやすいように分離)
# ---------------------------------------------------------------------------

def round_up_10(x: float) -> int:
    return int(-(-x // 10) * 10)


def curve_duty(value: float, curve: List[List[float]], lo: int, hi: int) -> int:
    """折れ線カーブから目標 % を求め、10 刻みに切り上げて [lo, hi] に収める。"""
    pts = sorted((float(x), float(d)) for x, d in curve)
    if value <= pts[0][0]:
        d = pts[0][1]
    elif value >= pts[-1][0]:
        d = pts[-1][1]
    else:
        d = pts[-1][1]
        for (x0, d0), (x1, d1) in zip(pts, pts[1:]):
            if x0 <= value <= x1:
                d = d0 + (d1 - d0) * (value - x0) / (x1 - x0) if x1 > x0 else d1
                break
    return max(lo, min(hi, round_up_10(d)))


@dataclass
class Controller:
    cfg: Dict
    duty: int = 100                      # 現在送っている %
    raise_since: Optional[float] = None  # 「上げるべき状態」が始まった時刻
    next_up_at: Optional[float] = None
    hot_cycles: int = 0
    alerted: bool = False
    safety_active: bool = False          # 直近が温度安全弁による強制 100% だったか
    history: List[Tuple[float, Optional[float], int]] = field(default_factory=list)

    def step(self, power: Optional[float], temp: Optional[float], now: float) -> int:
        """消費電力W (None=不明) と温度℃ (安全弁用)、現在時刻から今回送る % を決める。"""
        cfg = self.cfg
        lo, hi = int(cfg["min_duty"]), int(cfg["max_duty"])

        # 安全弁: 電力によらず、温度が閾値以上なら即座に全開へ (騒音より安全優先、待たない)
        safety_temp = cfg.get("safety_temp_c")
        if temp is not None and safety_temp is not None and temp >= float(safety_temp):
            if not self.safety_active:
                log.warning("safety: temp=%.1f >= %.1f -> forcing %d%% regardless of power",
                            temp, float(safety_temp), hi)
            self.safety_active = True
            self.duty = hi
            self.raise_since = None
            self.next_up_at = None
            self._update_alert(temp, hi)
            return self.duty
        self.safety_active = False

        if power is None:
            # 電力不明 → 安全側
            self.duty = hi
            self.raise_since = None
            self.next_up_at = None
            self._update_alert(temp, hi)
            return self.duty

        desired = curve_duty(power, cfg["curve"], lo, hi)

        if desired < self.duty:
            # 下げは即時 (静かになる方向は待つ必要がない)
            self.duty = desired
            self.raise_since = None
            self.next_up_at = None
        elif desired > self.duty:
            # 上げは一定時間の様子見をしてから 10% ずつ (急な騒音の立ち上がりを避ける)
            if self.raise_since is None:
                self.raise_since = now
                self.next_up_at = now + float(cfg["up_hold_s"])
            elif self.next_up_at is not None and now >= self.next_up_at:
                self.duty = min(desired, self.duty + 10)
                self.next_up_at = now + float(cfg["up_step_s"])
        else:
            self.raise_since = None
            self.next_up_at = None

        self._update_alert(temp, hi)
        return self.duty

    def _update_alert(self, temp: Optional[float], hi: int) -> None:
        # 信号未達の見張り: 全開なのに熱いまま
        if temp is not None and self.duty >= hi and temp >= float(self.cfg["alert_temp"]):
            self.hot_cycles += 1
        else:
            self.hot_cycles = 0
            self.alerted = False

    def should_alert(self) -> bool:
        if self.hot_cycles >= int(self.cfg["alert_cycles"]) and not self.alerted:
            self.alerted = True
            return True
        return False


# ---------------------------------------------------------------------------
# 送信
# ---------------------------------------------------------------------------

class BlasterSender:
    def __init__(self, base_url: str, timeout: float):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    def send_number(self, entity_name: str, value: float) -> bool:
        """ESPHome の number entity に値を送る共通処理 (Fan duty, DGX temp, DGX power で共用)。

        ESPHome の REST API は number の "id:" でなく "name:" をそのまま使う。
        スペース入りなのでパス側は個別に quote する (urlencode はクエリ用)。
        """
        entity = urllib.parse.quote(entity_name, safe="")
        url = f"{self.base}/number/{entity}/set?" + urllib.parse.urlencode({"value": value})
        req = urllib.request.Request(url, method="POST", data=b"")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
            return True
        except (urllib.error.URLError, OSError) as e:
            log.warning("blaster: send %s=%s failed: %s", entity_name, value, e)
            return False

    def send(self, pct: int) -> bool:
        return self.send_number("Fan duty", pct)

    def read_sensor(self, name: str) -> Optional[float]:
        """ESPHome の REST API から sensor の現在値を読む (例: ATOM Lite 自身の内部温度)。"""
        entity = urllib.parse.quote(name, safe="")
        url = f"{self.base}/sensor/{entity}"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
            return float(data["value"])
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
            log.warning("blaster: read sensor '%s' failed: %s", name, e)
            return None


class DryRunSender:
    def send_number(self, entity_name: str, value: float) -> bool:
        log.info("[dry-run] would send %s=%s", entity_name, value)
        return True

    def send(self, pct: int) -> bool:
        return self.send_number("Fan duty", pct)

    def read_sensor(self, name: str) -> Optional[float]:
        return None


def call_with_hard_timeout(fn, timeout_s: float, default=None):
    """fn() を別スレッドで実行し、timeout_s 経っても終わらなければ諦めて default を返す。

    urllib の timeout= はソケット接続後にしか効かず、mDNS 名前解決 (fanblaster.local) が
    ハングするケースをカバーしない。ATOM Lite の再起動・OTA 書き込み中などで名前解決が
    固まると、素の呼び出しではメインループ全体 (温度読み取り・安全弁判定を含む) が
    止まってしまうため、ここで別スレッド + join タイムアウトにより強制的に見切りを付ける。
    ハングしたスレッド自体は daemon スレッドのまま放置する (プロセス終了はブロックしない)。
    fanblaster への送信・センサー読み取りなど、HTTP を叩く処理全般で共通に使う。
    """
    result: Dict[str, object] = {"value": default}

    def _run() -> None:
        try:
            result["value"] = fn()
        except Exception as e:  # noqa: BLE001
            log.warning("call_with_hard_timeout: %s", e)
            result["value"] = default

    t = threading.Thread(target=_run, daemon=True, name="blaster-call")
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        log.warning(
            "call_with_hard_timeout: hard-timeout (%.0fs 超) — 名前解決か接続がハングしている疑い。"
            "このスレッドは諦めて次周期へ進む",
            timeout_s,
        )
        return default
    return result["value"]


def send_with_hard_timeout(sender, pct: int, timeout_s: float) -> bool:
    return bool(call_with_hard_timeout(lambda: sender.send(pct), timeout_s, default=False))


def should_transmit(
    duty: int,
    last_sent_duty: Optional[int],
    last_sent_at: Optional[float],
    now: float,
    resend_interval_s: float,
    safety_active: bool,
) -> bool:
    """今回 % を実際に送信するか。

    変化した時・まだ一度も送っていない時・安全弁作動中は必ず送る。
    それ以外は resend_interval_s 経つまで送らない (赤外線 LED が毎周期光るのを避けつつ、
    信号取りこぼしに対しては resend_interval_s ごとに送り直して自己修復する)。
    """
    if last_sent_duty is None or last_sent_at is None:
        return True
    if safety_active:
        return True
    if duty != last_sent_duty:
        return True
    return (now - last_sent_at) >= resend_interval_s


# ---------------------------------------------------------------------------
# メインループ
# ---------------------------------------------------------------------------

def run_alert(cfg: Dict, temp: float, duty: int) -> None:
    log.error("ALERT: duty=%d%% but temp=%.1f°C for %d cycles — IR signal may not be reaching the controller",
              duty, temp, cfg["alert_cycles"])
    cmd = cfg.get("alert_cmd") or ""
    if cmd:
        try:
            subprocess.run(shlex.split(cmd), timeout=15, check=False)
        except Exception as e:  # noqa: BLE001
            log.warning("alert_cmd failed: %s", e)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", help="JSON 設定ファイル (既定: /etc/fanctl.json があれば読む)")
    ap.add_argument("--dry-run", action="store_true", help="送信せずログに出すだけ")
    ap.add_argument("--once", action="store_true", help="1 周期だけ実行して終了")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    cfg_path = args.config
    if cfg_path is None:
        try:
            open("/etc/fanctl.json").close()
            cfg_path = "/etc/fanctl.json"
        except OSError:
            cfg_path = None
    cfg = load_config(cfg_path)
    log.info("config: %s", cfg_path or "(built-in defaults)")

    sender = DryRunSender() if args.dry_run else BlasterSender(cfg["blaster_url"], cfg["blaster_timeout_s"])
    ctl = Controller(cfg)
    resend_interval = float(cfg.get("resend_interval_s", 60))
    # urlopen の timeout= は名前解決 (mDNS) をカバーしないため、それより少し長めの
    # ハードタイムアウトを別スレッドで強制する (send_with_hard_timeout / call_with_hard_timeout 参照)
    send_hard_timeout = float(cfg["blaster_timeout_s"]) + 3.0
    m5_sensor_name = cfg.get("m5_temp_sensor_name") or None
    report_to_m5 = bool(cfg.get("report_dgx_metrics_to_m5", True))
    m5_report_interval = float(cfg.get("m5_report_interval_s", 60))
    last_sent_duty: Optional[int] = None
    last_sent_at: Optional[float] = None
    last_m5_report_at: Optional[float] = None
    last_m5_temp: Optional[float] = None

    stopping = {"flag": False}

    def on_signal(signum, _frame):
        log.info("signal %d: stopping", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        while not stopping["flag"]:
            t0 = time.monotonic()
            power, per_power, temp, per_temp = read_cluster_metrics(cfg)
            duty = ctl.step(power, temp, t0)
            pw_s = " ".join(f"{k}={'?' if v is None else f'{v:.0f}W'}" for k, v in per_power.items())
            tp_s = " ".join(f"{k}={'?' if v is None else f'{v:.0f}C'}" for k, v in per_temp.items())

            if should_transmit(duty, last_sent_duty, last_sent_at, t0, resend_interval, ctl.safety_active):
                ok = send_with_hard_timeout(sender, duty, send_hard_timeout)
                sent_tag = "sent" if ok else "send-failed"
                if ok:
                    last_sent_duty, last_sent_at = duty, t0
            else:
                sent_tag = "unchanged"

            # M5 自身の内蔵温度読み取り・DGX 側の値の送信は、Fan duty 送信とは別に
            # m5_report_interval_s 間隔だけ行う (毎周期だと ESP32 側の HTTP 接続数を
            # 圧迫し "httpd_accept_conn: error in accept" のようなソケット枯渇を招くため)
            if last_m5_report_at is None or (t0 - last_m5_report_at) >= m5_report_interval:
                if m5_sensor_name:
                    last_m5_temp = call_with_hard_timeout(
                        lambda: sender.read_sensor(m5_sensor_name), send_hard_timeout, default=None
                    )
                if report_to_m5:
                    if power is not None:
                        call_with_hard_timeout(
                            lambda: sender.send_number("DGX power", round(power, 1)), send_hard_timeout, default=None
                        )
                    if temp is not None:
                        call_with_hard_timeout(
                            lambda: sender.send_number("DGX temp", round(temp, 1)), send_hard_timeout, default=None
                        )
                last_m5_report_at = t0
            m5_temp = last_m5_temp

            log.info("power=%s temp=%s duty=%d%% (%s) m5_temp=%s [%s | %s]",
                      "?" if power is None else f"{power:.1f}",
                      "?" if temp is None else f"{temp:.1f}",
                      duty, sent_tag,
                      "?" if m5_temp is None else f"{m5_temp:.1f}",
                      pw_s, tp_s)
            if temp is not None and ctl.should_alert():
                run_alert(cfg, temp, duty)
            if args.once:
                break
            # 周期の残りを待つ (停止シグナルには 1 秒以内に反応)
            deadline = t0 + float(cfg["interval_s"])
            while not stopping["flag"] and time.monotonic() < deadline:
                time.sleep(min(1.0, deadline - time.monotonic()))
    finally:
        # 停止時は必ず全開に戻す (これもハングで止まらないようハードタイムアウト付き)
        log.info("exit: sending %d%%", int(cfg["max_duty"]))
        send_with_hard_timeout(sender, int(cfg["max_duty"]), send_hard_timeout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
