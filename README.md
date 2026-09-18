# spark-fanctl — DGX Spark の GPU 消費電力でファンを自動制御する

OwlTree の赤外線リモコン付き PWM ファンコントローラー (B0GKGJKFTY) を、M5Stack ATOM Lite (ESPHome) でなりすまし操作し、2 ノードの GPU 消費電力 (W) に連動させる。
温度は安全弁として併用 (`safety_temp_c` 以上で強制 100%)。

```
 [node1] ──ssh──> [node2]        (消費電力・温度取得)
       │ fanctl.py
       │ HTTP POST /number/fan_duty/set?value=70
       ▼
 [ATOM Lite / ESPHome] ──赤外線──> [OwlTree コントローラー] ──PWM──> NF-F12 ×2
       └ 見張り: 180 秒無通信で 100% を自送信、起動時も 100%
```

送信 (赤外線 LED 点灯) は % が変わった時だけ。無変化でも `resend_interval_s` (既定60秒)おきに自己修復のため送り直す。

## 構成

| ファイル | 置き場所 | 役割 |
|---|---|---|
| `esphome/fanblaster.yaml` | 手元の PC (ESPHome を動かす所) | ATOM Lite のファームウェア定義 |
| `esphome/secrets.yaml.example` | 同上 (`secrets.yaml` にコピー) | Wi-Fi などの秘密情報 |
| `daemon/fanctl.py` | node1 `/opt/fanctl/` | 温度→% を決めて送る常駐プログラム |
| `daemon/fanctl.json.example` | node1 `/etc/fanctl.json` | 設定 |
| `daemon/fanctl.service` | node1 `/etc/systemd/system/` | systemd ユニット |
| `daemon/test_fanctl.py` | 任意 | 制御ロジックのテスト |

## 部品

- M5Stack ATOM Lite (ESP32、USB-C 給電、赤外線 LED 内蔵)
- M5Stack IR Unit (Grove 接続、リモコン信号の記録に使う。記録後は外してよい)
- USB-C ケーブルと 5V 電源 (DGX Spark の USB から取ってもよいが、独立電源のほうが両ノード停止時も見張りが生きる)

## 1. ESPHome の準備 (手元 PC)

```sh
python3 -m venv ~/.esphome && ~/.esphome/bin/pip install esphome
cd esphome
cp secrets.yaml.example secrets.yaml   # SSID など記入。Wi-Fi は 2.4GHz のみ
~/.esphome/bin/esphome config fanblaster.yaml   # 構文確認
```

ATOM Lite を USB で PC につなぎ、初回書き込み:

```sh
~/.esphome/bin/esphome run fanblaster.yaml
```

以後は Wi-Fi 越し (OTA) に同じコマンドで書き換えられる。
書き込み後、`http://fanblaster.local/` を開くと Web UI が出る (Fan duty スライダー、ボタン、センサー)。
mDNS が通らないネットワークなら `wifi.manual_ip` で固定 IP にし、以降の `fanblaster.local` を IP に置き換える。

## 2. リモコン信号の記録

1. IR Unit を ATOM Lite の Grove に挿す (白線 = GPIO32 が受信)。
2. `esphome logs fanblaster.yaml` でログを流す。
3. リモコンを IR Unit に向け、ボタンを 1 つずつ押す。ログに次のような行が出る:

   ```
   [remote.nec] Received NEC: address=0x7F80, command=0xF10E, command_repeats=1
   ```

4. 0%〜100% (11 個) と「表示 点灯/消灯」の `address` と `command` を控える。同じボタンを 2〜3 回押して同じ値が出ることを確認する。
5. `fanblaster.yaml` の `substitutions` (`nec_address`, `nec_p0` … `nec_p100`, `nec_display`) に書き込み、`esphome run` で再書き込み。
6. Web UI の Fan duty スライダーを動かし、コントローラーの表示が追従することを確認する。届かなければ ATOM Lite の向きと距離を調整する (内蔵 LED の到達距離は 3m 程度。コントローラーの受光窓に正対させる)。

**NEC 以外だった場合**: ログの先頭が `[remote.nec]` でなく `[remote.raw]` や別の方式なら、`remote_transmitter.transmit_nec` を対応する `transmit_*` に、または `transmit_raw` (ログの `code:` 配列をそのまま貼る) に置き換える。
`send_pct` スクリプト内の配列方式は使えなくなるので、% ごとに `if:` で分岐させる形にする。

記録が終わったら `remote_receiver.dump` を `none` にするか、ブロックごとコメントアウトしてログを静かにする。IR Unit は外してよい。

## 3. daemon の配置 (node1)

```sh
# 専用ユーザー
sudo useradd -r -m -s /usr/sbin/nologin fanctl

# ファイル
sudo mkdir -p /opt/fanctl
sudo cp daemon/fanctl.py /opt/fanctl/
sudo cp daemon/fanctl.json.example /etc/fanctl.json
sudo cp daemon/fanctl.service /etc/systemd/system/
sudo chmod 644 /etc/fanctl.json

# もう片方のノードへ鍵認証で入れるようにする
sudo -u fanctl ssh-keygen -t ed25519 -N '' -f /home/fanctl/.ssh/id_ed25519
sudo cat /home/fanctl/.ssh/id_ed25519.pub   # → node2 側の対象ユーザーの authorized_keys に追加
sudo -u fanctl ssh <user>@node2 nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader
#   ↑ 一度手で通して known_hosts を作る (サービスはホームを read-only で動かすため)
```

`/etc/fanctl.json` を編集:

- `nodes[1].ssh` を実際の `user@host` に
- `blaster_url` を ATOM Lite の名前または IP に
- `fanctl.service` の `ExecStopPost` の URL も同じものに

動作確認 (送信せずログだけ):

```sh
sudo -u fanctl python3 /opt/fanctl/fanctl.py --config /etc/fanctl.json --dry-run --once -v
```

両ノードの温度が出て `[dry-run] would send NN%` と表示されれば OK。実送信を 1 回:

```sh
sudo -u fanctl python3 /opt/fanctl/fanctl.py --config /etc/fanctl.json --once
```

有効化:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now fanctl
journalctl -u fanctl -f
```

## 4. 制御の考え方と調整

- 制御量は GPU **消費電力 (W)**。負荷と温度より電力のほうが直結するため (アイドル ~4W、高負荷 ~90W 実測)。
- ファンカーブは `curve` の折れ線 (`[消費電力W, %]`)。間は直線補間し 10% 刻みに**切り上げ**。既定は 10W→30%、40W→50%、70W→80%、90W→100%。
- 電力は両ノードの最大値 (`nvidia-smi power.draw`)。
- **安全弁**: 電力カーブと別に、温度が `safety_temp_c` (既定 80℃) 以上になったら電力値を無視して即座に強制 100%。
  温度は既定で GPU 温度 (`nvidia-smi`) のみ。
  `include_thermal_zones: true` にすると `/sys/class/thermal` (acpitz 等、マザーボード/チップセット系でGPU温度とは限らない) も候補に混ぜて最大値を採る。ここだけは待たない。
- **下げは即時、上げは待つ**。急に静かになる分には気にならないが、急に大きくなる音は気になるため。
  上げるべき状態が `up_hold_s` (60 秒) 続いてから、`up_step_s` (30 秒 = 制御周期と同じ) ごとに 10% ずつ上げる。
  長く待ちすぎると急な負荷上昇に追いつかないため短めにしてある (温度がさらに上がれば `safety_temp_c` の安全弁が別途即座に効く)。
- 判定は `interval_s` (30 秒) ごとに行うが、実際の送信 (赤外線 LED 点灯) は **% が変わった時だけ**。
  無変化でも `resend_interval_s` (既定60秒) おきに送り直して信号取りこぼしを自己修復する。
  安全弁作動中はこの間隔を無視して毎周期送る (温度が下がるまで届いているか確認し続けたいため)。
- 電力が 1 ノードも読めないときは 100% (安全側)。片方だけ読めないときは読めた方で制御 (ログに `?` が出る)。
- daemon 停止時 (SIGTERM) と、異常終了時の `ExecStopPost` で 100% を送る。
- 送信 (fanblaster への HTTP POST) はハードタイムアウト付き。`urllib` の `timeout=` は
  mDNS 名前解決の途中でハングするケースをカバーしないため (ATOM Lite の OTA 書き込み中・
  再起動中などに実際に起きうる)、別スレッド + join タイムアウトで強制的に見切りを付け、
  制御ループ本体 (温度読み取り・安全弁判定) が巻き込まれて止まらないようにしてある。
- ATOM Lite 側の見張り: 180 秒指示が来なければ 100% を送り、来ないあいだ 180 秒ごとに送り直す。起動時にも 100% を送る。
  (daemon の `resend_interval_s` より長く取ってあるので、無変化期間を誤って「daemon が死んだ」と判定しない)
- `m5_temp_sensor_name` (既定 `"ATOM internal temperature"`): ATOM Lite (ESP32) 自身の
  チップ内蔵温度を毎周期 REST API 経由で読み、ログの `m5_temp=` に出す。GPU 排気の近くに
  置く等、機器自体が熱くなっていないか見張りたい場合用。空文字/`null` にすると読みに行かない
  (センサーの無い旧ファーム互換)。読めなくてもファン制御自体には影響しない。
- `report_dgx_metrics_to_m5` (既定 `true`): 逆に DGX Spark 側の温度・消費電力を M5 にも
  送っておく (裏の `number.DGX temp` / `number.DGX power` に送り、ダッシュボードには
  上限/下限の出ない読み取り専用の `sensor.DGX temp` / `sensor.DGX power` として表示)。
  ファン制御には使わない (制御は daemon 側で完結)。
- `m5_report_interval_s` (既定 60 秒): 上記2つ (M5 自身の内蔵温度読み取り・DGX 側の値の
  送信) を行う間隔。単なる表示・ログ用なので `interval_s` 毎周期は必要なく、むしろ
  ESP32 側の同時 HTTP 接続数を圧迫して `httpd_accept_conn: error in accept` のような
  ソケット枯渇を招きうる (実際に発生した)。Fan duty の送信判定はこれとは独立で
  毎周期のまま。
- `alert_temp` (85℃) 以上なのに 100% で `alert_cycles` (6 周期 = 2 分) 続いたら、赤外線が届いていない疑いとしてログに ERROR を出し、`alert_cmd` があれば実行する (ntfy や Slack の webhook を入れる)。

パラメータを変えたら `sudo systemctl restart fanctl`。

## 5. 制御ロジックのテスト

```sh
cd daemon && python3 test_fanctl.py
```

## 既知の制約

- コントローラーは「前回値保持」なので、ATOM Lite が完全に死ぬと最後に送った % のままになる。ATOM Lite を独立電源にし、Web UI の `Seconds since last command` が伸び続けていないかを時々見る。
- 赤外線は一方通行で、コントローラーが受け取ったかは分からない。上の alert が唯一の検知手段。より確実にしたい場合は、Pico で PWM 線を直接駆動する方式 (案 B) に移行する。daemon の送信部だけ差し替えれば済む。
- `fanblaster.yaml` は `esphome config` での構文検証まで実施済み。実機でのコンパイルと動作は未確認。
