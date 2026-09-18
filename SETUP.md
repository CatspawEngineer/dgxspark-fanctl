# セットアップ手順書 — M5Stack (ATOM Lite / IR Unit) と fanctl daemon

DGX Spark 2 台 (node1, node2) の空冷ファンを、OwlTree の赤外線リモコン付きPWM コントローラー経由で自動制御するための、実際に行った手順のまとめ。
プロジェクト概要は `README.md` を参照。本書は「何を、どの順で、どこに注意して行ったか」の運用手順書として使う (再構築・別ノード追加・トラブル対応時の参照用)。

対象:
- Part A: M5Stack ATOM Lite + IR Unit を ESPHome で赤外線送信機 (fanblaster) にする
- Part B: node1 上で fanctl daemon をセットアップする

---

## 部品

- M5Stack ATOM Lite (ESP32、USB-C 給電)
- M5Stack IR Unit (U002、Grove 接続。赤外線の送信/受信の両方を持つが、送信は IR Unit 側の
  発光部 (クリアレンズ) を使う。ATOM Lite 内蔵 LED (GPIO12) は到達距離が不足し不採用)
- USB-C ケーブル、独立した 5V 電源 (DGX Spark の USB から取ってもよいが、両ノード停止時も見張り機能を生かすなら独立電源が望ましい)
- 作業用 PC (Windows で実施。ESPHome の初回書き込みに USB 接続が必要)

---

## Part A: M5Stack (ESPHome) セットアップ

### A-1. ESPHome の準備 (Windows)

```powershell
python3 -m venv C:\esphome\.esphome
C:\esphome\.esphome\Scripts\pip install esphome
```

**注意 (Windows 特有のはまりどころ)**

1. **Git Bash / MSYS 環境を使わない。** `esphome run` を Git Bash から実行すると
   `ERROR: MSys/Mingw is not supported.` で失敗する。PowerShell か cmd を使う。
   どうしても Git Bash から呼ぶ必要がある場合は、呼び出し前に `MSYSTEM` 環境変数を
   クリアするラッパースクリプトを挟む。

2. **ユーザー名が非 ASCII (日本語など) だとビルドが壊れる。** ESP-IDF のツールチェインが
   キャッシュパス (`%LOCALAPPDATA%\esphome\Cache\...`) にユーザー名を含むため、
   `ld.exe: cannot find ... crt0.o` のようなリンクエラーが出る (ninja のログが文字化けする
   のが目印)。対策として `ESPHOME_ESP_IDF_PREFIX` 環境変数で ASCII のみのパスを指定する:

   ```bat
   @echo off
   set MSYSTEM=
   set ESPHOME_ESP_IDF_PREFIX=C:\esphome\idf
   C:\esphome\.esphome\Scripts\esphome.exe %*
   ```

   これを `C:\esphome\esphome.cmd` として保存し、以後は `esphome.cmd run fanblaster.yaml`
   のように呼ぶ。パス変更後は古いビルドキャッシュを消してから再実行する。

### A-2. 設定ファイルの用意

```powershell
cd C:\esphome
copy secrets.yaml.example secrets.yaml
```

`secrets.yaml` に Wi-Fi の SSID/パスワード (2.4GHz のみ)、OTA パスワード、AP パスワードを記入。

構文確認:

```powershell
esphome.cmd config fanblaster.yaml
```

### A-3. 初回書き込み (USB)

ATOM Lite を USB で PC に接続し:

```powershell
esphome.cmd run fanblaster.yaml
```

以後は Wi-Fi 経由 (OTA) で同じコマンドで書き換えられる。
書き込み後 `http://fanblaster.local/` を開くと Web UI が表示される (Fan duty スライダー、
ボタン、センサー)。mDNS が通らない環境では `wifi.manual_ip` で固定 IP にし、
以降 `fanblaster.local` を IP 読み替え。

### A-4. IR Unit の配線と赤外線リモコン信号の記録

- IR Unit を Grove で接続。**発光部 (クリアレンズ) が送信 = GPIO26、受光部 (黒レンズ) が
  受信 = GPIO32。** 見分けにくいので写真で確認するとよい。
- `fanblaster.yaml` の `remote_transmitter.pin` は GPIO26 (IR Unit の発光部) を使う。
  ATOM Lite 内蔵 LED (GPIO12) は実測で反応せず不採用。

記録手順:

1. `esphome logs fanblaster.yaml` でログを流す。
2. OwlTree リモコンを IR Unit の受光部に向け、ボタンを 1 つずつ押す。
   ログに次のような行が出る:

   ```
   [remote.nec] Received NEC: address=0x5700, command=0x926D, command_repeats=1
   ```

3. 0/5/10/20/30/40/50/60/70/80/90/100 の各ボタンと、表示 点灯/消灯、+/− ボタンについて`address` と `command` を控える。
   同じボタンを 2 回以上押して値が一致することを確認する。
4. NEC プロトコルであることの確認: `command` の下位バイトと上位バイトが互いのビット反転になっているか (`(command & 0xFF) ^ 0xFF == command >> 8`) を確認すると、他プロトコルの誤検出と区別しやすい。
5. `fanblaster.yaml` の `substitutions` (`nec_address`, `nec_p0`〜`nec_p100`, `nec_display`,`nec_minus`, `nec_plus`) に実測値を書き込み、`esphome.cmd run fanblaster.yaml` で再書き込み。
6. Web UI の Fan duty スライダーを動かし、コントローラーの表示 % が追従するか確認する。
   届かない場合は ATOM Lite (IR Unit) の向きと距離を調整する。

記録が終わったら `remote_receiver.dump` を空リスト `[]` にしてログを静かにする(IR Unit 自体は外してよいが、今の構成では発光部を使い続けるので付けたままでよい)。

**ESPHome の既知の癖**: `logger.log:` に `%` を含む文字列 (`%%` エスケープ済みでも) を渡すと、ESPHome 自身の printf 検証が誤って `Found N printf-patterns, but 0 args were given!` とエラーを出すことがある。回避策は `%` をログ文言から削る (エスケープでは直らない)。

### A-5. 起動時・見張り (watchdog) の動作、機器自体の温度

- ATOM Lite (ESP32) のチップ内蔵温度センサーを `sensor: platform: internal_temperature`として追加済み (`fanblaster.yaml`)。
  REST API `GET /sensor/ATOM%20internal%20temperature`で読め、daemon 側もこれを毎周期読んでログの `m5_temp=` に出す (B-6 参照)。
  GPU 排気の近くに置く等、機器自体の発熱を見張りたい場合の目安になる。
- 逆に DGX Spark 側の温度・消費電力を M5 にも送るための入れ物として、`number:` に`"DGX temp"` / `"DGX power"` (`mode: box`、`optimistic: true`、`internal: true`) を追加済み。
  daemon が `POST /number/DGX%20temp/set?value=...` のように送り込み、ATOM Lite 側の `set_action` でログに出す (`esphome logs fanblaster.yaml` で見える)。
  `number` は仕様上どうしても上限/下限が Web UI に表示されてしまうため `internal: true`でダッシュボードから隠し (REST の送信先としては生きたまま)、代わりに現在値だけを表示する読み取り専用の `sensor: "DGX temp"` / `"DGX power"` をミラーとして追加してある。
  いずれもファン制御には無関係で、単に M5 側でも同じ数字を確認できるようにするだけ。
- 起動直後: 2 秒後に duty 30% を送信 (いきなり 100% は騒音、いきなり 0% は安全上避ける)。
- 見張り: `watchdog_timeout_ms` (既定 180 秒) の間 daemon から指示が来なければ、ATOM Lite 自身が 100% を送り、来ないあいだは繰り返し送り直す。
  daemon 側は % が変わらない限り`resend_interval_s` (既定60秒) おきにしか送らないので、watchdog はそれより十分長く(3倍程度) 取ってあり、無変化期間を「daemon が死んだ」と誤判定しない。

---

## Part B: fanctl daemon セットアップ (node1)

### B-1. 専用ユーザーとファイル配置

```sh
sudo useradd -r -m -s /usr/sbin/nologin fanctl

sudo mkdir -p /opt/fanctl
sudo cp daemon/fanctl.py /opt/fanctl/
sudo cp daemon/fanctl.json.example /etc/fanctl.json
sudo cp daemon/fanctl.service /etc/systemd/system/
sudo chmod 644 /etc/fanctl.json
```

### B-2. node2 への SSH 鍵認証

```sh
sudo -u fanctl ssh-keygen -t ed25519 -N '' -f /home/fanctl/.ssh/id_ed25519
sudo cat /home/fanctl/.ssh/id_ed25519.pub
# ↑ の公開鍵を node2 側の対象ユーザーの ~/.ssh/authorized_keys に追加 (600/700 権限で)

sudo -u fanctl ssh <user>@node2 nvidia-smi --query-gpu=power.draw --format=csv,noheader
# 一度手動で通し known_hosts を作る (service はホームを read-only で動かすため)
```

### B-3. 設定ファイル `/etc/fanctl.json` の編集

- `nodes[1].ssh` を実際の `user@node2` に。
- `blaster_url` を ATOM Lite の mDNS 名または固定 IP に。
- `fanctl.service` の `ExecStopPost` の URL も同じホストに合わせる。
- 既定の制御パラメータ (電力カーブ、安全弁温度など) は Part B-5 を参照して必要に応じて調整。

### B-4. 動作確認

送信せずログだけ確認:

```sh
sudo -u fanctl python3 /opt/fanctl/fanctl.py --config /etc/fanctl.json --dry-run --once -v
```

両ノードの消費電力・温度が出て `[dry-run] would send NN%` と表示されれば OK。
実際に 1 回送信:

```sh
sudo -u fanctl python3 /opt/fanctl/fanctl.py --config /etc/fanctl.json --once
```

### B-5. systemd への登録

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now fanctl
journalctl -u fanctl -f
```

**注意 (systemd の `%` エスケープ)**: unit ファイルの `ExecStart`/`ExecStopPost` 内で
URL に `%20` のような `%` を含む文字列を書くと、systemd が specifier として誤解釈し
`Invalid slot` で起動に失敗する。`%%20` のように 2 重にエスケープする
(実際にコントローラーへ送られる URL は `Fan%20duty` のまま)。

```sh
sudo sed -i 's/Fan%20duty/Fan%%20duty/' /etc/systemd/system/fanctl.service
sudo systemctl daemon-reload
sudo systemctl restart fanctl
```

`journalctl -u fanctl -f` で `Started fanctl.service` と、周期ごとに
`power=... temp=... duty=NN% (...) m5_temp=... [...]` のログが出ていれば稼働中。
`m5_temp` は ATOM Lite 自身のチップ内蔵温度 (B-6 参照)。

### B-6. 制御パラメータ (`/etc/fanctl.json`)

制御量は GPU **消費電力 (W)**。負荷との相関が温度より直接的なため(実測: アイドル ~4W、高負荷 ~90W、100W は未確認)。

- `curve`: `[消費電力W, %]` の折れ線。既定 `[[10,30],[40,50],[70,80],[90,100]]`。
  10W 以下で 30%、90W 以上で 100%、間は直線補間して 10% 刻みに切り上げ。
- `safety_temp_c` (既定 **80℃**): 電力カーブとは独立の安全弁。既定では GPU 温度(`nvidia-smi`) のみを見る。
  `include_thermal_zones: true` にすると `/sys/class/thermal`(acpitz 等、GPU 温度とは限らない) も候補に混ぜて最大値を採る。
  この値以上になったら、電力値を無視して即座に強制 100%。この温度は M5 に送る `DGX temp` とも共通。
- `up_hold_s` / `up_step_s`: 上げるときだけ待つ (下げは即時)。既定 60 秒待ってから30 秒 (制御周期と同じ) ごとに 10% ずつ上げる。
  急な負荷上昇に長く待ちすぎて追いつかないことのないよう短めにしてある。
  さらに温度が上がれば `safety_temp_c` の安全弁が別途即座に効く。
- `resend_interval_s` (既定 **60秒**): 実際に赤外線を送るのは % が変わった時だけ。無変化でもこの間隔で送り直して信号取りこぼしを自己修復する (安全弁作動中はこれを無視して毎周期送る)。
  ATOM Lite 側の `watchdog_timeout_ms` (A-5、既定180秒) はこれより十分長くしてあるので、無変化期間を「daemon が死んだ」と誤判定しない。
- `alert_temp` / `alert_cycles` / `alert_cmd`: 100% を送り続けているのに温度が下がらない状態が続いたら (赤外線が届いていない疑い)、ログに ERROR を出し `alert_cmd` があれば実行。
- `m5_temp_sensor_name` (既定 `"ATOM internal temperature"`): ATOM Lite 自身のチップ内蔵温度をログの `m5_temp=` に出す (A-4 で追加したセンサー)。機器自体の発熱の目安。
  空文字/`null` で無効化 (センサーが無い旧ファームでも動くように)。
- `report_dgx_metrics_to_m5` (既定 `true`): DGX Spark 側の温度・消費電力を M5 にも送る(裏の `number: "DGX temp"` / `"DGX power"` に送り、A-4 で追加した読み取り専用の`sensor: "DGX temp"` / `"DGX power"` に反映される)。ファン制御そのものには使わない。
- `m5_report_interval_s` (既定 60 秒): 上記2つと M5 自身の内蔵温度読み取りをこの間隔で行う。
  表示・ログ用のおまけなので `interval_s` 毎周期にする必要はなく、むしろ ESP32 側のHTTP 接続数を圧迫して `httpd_accept_conn: error in accept` のようなソケット枯渇を招く(実際に起きた不具合)。
  Fan duty の送信はこれと無関係に毎周期判定される。

パラメータ変更後は `sudo systemctl restart fanctl`。

### B-7. 動作テスト (ロジックのみ、実機不要)

```sh
cd daemon && python3 test_fanctl.py
```

---

## 既知の制約・運用メモ

- OwlTree コントローラーは「前回値保持」。ATOM Lite が完全に落ちると最後に送った % のまま固定される。
  ATOM Lite は独立電源にし、Web UI の `Seconds since last command` が伸び続けていないか時々確認する。
- 赤外線は一方通行で、コントローラーが受信できたかは分からない。B-6 の alert が唯一の検知手段。
- daemon 停止時 (SIGTERM) と ATOM Lite 側の見張りタイムアウトの両方が、最終的に 100% へ倒す安全側の設計になっている。
- ATOM Lite を OTA 書き込み中・再起動中は一時的に応答しなくなる。この間に daemon がfanblaster へ送信しようとすると、mDNS 名前解決がハングして制御ループ全体が止まりかねない
  (`urllib` の timeout はここをカバーしない) ため、送信は別スレッド + ハードタイムアウトで強制的に見切りを付ける実装にしてある。
  それでも書き込み中は当然 IR 信号は届かないので、ファームウェア更新は極力ノードが低負荷な時に行う。
