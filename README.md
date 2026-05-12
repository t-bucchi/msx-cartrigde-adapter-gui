# MSXPLAYer Game Cartidge Adapter GUI

MSXPLAYer Game Cartidge Adapter を USB CDC シリアルデバイスとして扱う、Python + `tkinter` ベースの GUI ツールです。

全部AIに作ってもらいました。

仕様がオープンなので便利で優秀なツールが出てくると思うので、それまでの繋ぎとして自分の使いたい機能を実装していきます。

## 動作環境

- Python 3.10 以上
- `tkinter`
- `pyserial`

Linuxで動作確認しています。クロスプラットフォームを意識して作っていますが、Windows, Mac では動作確認していません。

## セットアップ

`pyserial` をインストールします。

```bash
python3 -m pip install pyserial
```

## 起動方法

```bash
python3 app.py
```

起動直後はシリアル未接続の状態で開始し、HEX 画面には 64kB 全体を `00` で表示します。

接続は `設定 -> シリアル` またはツールバーの歯車ボタンから行ってください。

`--device` を指定した場合は、シリアル選択ダイアログでそのデバイスを事前選択します。

```bash
python3 app.py --device=/dev/ttyACM0
```

## 主な機能

- 64kB 全体の HEX 表示
- 固定ルーラ付きの 16 バイト幅表示
- HEX 編集
- 範囲指定付きのバッファ保存 (mapper 非対応)
- シリアルデバイス選択ダイアログ
- 手動コマンド実行ウィンドウ
- アクセスログ表示

## キー操作

- `Left` / `Right` / `Up` / `Down`: カーソル移動
- `Home` / `End`: 行頭 / 行末
- `PageUp` / `PageDown`: `-0100h` / `+0100h`
- `Ctrl+PageUp` / `Ctrl+PageDown`: `-0800h` / `+0800h`

## 保存機能

保存ダイアログでは以下を指定できます。

- 保存ファイル名
- 開始アドレス
- 終了アドレス

mapper に対応したダンプは対応していません。
