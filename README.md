# MSXPLAYer Game Cartidge Adapter GUI

MSXPLAYer Game Cartidge Adapter を USB CDC シリアルデバイスとして扱う、Python + `tkinter` ベースの GUI ツールです。

全部AIに作ってもらいました。

仕様が公開されているので、より完成度の高いツールが出てくるまでの繋ぎとして、自分が使いたい機能を実装していきます。

AI agent に読ませるためのドキュメントも docs に入れてありますので、AIを使って自由にカスタマイズしてみてください。

## 動作環境

- Python 3.10 以上
- `tkinter`
- `pyserial`

Linux で動作確認しています。クロスプラットフォームを意識して作っていますが、Windows / macOS では未確認です。

## セットアップ

`pyserial` をインストールします。

```bash
python3 -m pip install pyserial
```

## 起動方法

```bash
python3 app.py
```

起動直後はシリアル未接続の状態で開始し、HEX 画面には `00` で初期化したデータを表示します。

接続は `設定 -> シリアル` またはツールバーの歯車ボタンから行います。

`--device` を指定した場合は、起動時にそのデバイスへ自動接続を試みます。

```bash
python3 app.py --device=/dev/ttyACM0
```

## 主な機能

- Flat 表示
  - 64kB 全体の HEX 表示
  - 固定ルーラ付きの 16 バイト幅表示
- Mapper 表示
  - 1バンク単位の HEX 表示
  - 上部で mapper type などを設定可能
- HEX 編集
  - 1 バイト確定時に即時書き込み
  - Flat は 64kB 再読込
  - Mapper は該当 Bank のみ再読込
- Mapper Map
  - `Map` ダイアログで 256 Bank を解析
  - 新規データ / ミラーバンクの可視化
  - 推定イメージサイズ表示
  - セルクリックでメイン画面の Bank を切替
- 保存
  - Flat 保存
  - Mapper 保存
- 手動コマンド実行ウィンドウ
- アクセスログ表示
- シリアルデバイス選択ダイアログ

## Mapper 表示

ツールバーの下の Mapper バーで以下を操作できます。

- `Mapper` チェックボックス - チェックを入れると mapper モードになります
- `Type`
  - `ASCII 8K`
  - `ASCII 16K`
  - `Konami`
  - `Custom`
- `Window`
  - `4000-5FFF(8k)`
  - `4000-7FFF(16k)`
  - `8000-9FFF(8k)`
  - `8000-BFFF(16k)`
- `Switch`
- `Bank`
- `Map`

`Type` を変更すると `Window` と `Switch` にプリセット値を反映します。
`Window` または `Switch` を手動変更すると `Type=Custom` になります。

有名な Mapper Type をプリセットしていますが、Window と Switch の指定である程度のマッパーはカバーできると思います。
※ 16bit mapper (NEO-8, NEO-16 など) は非対応

## Mapper Map

`Map` ボタンで `Mapper Map` ダイアログを開きます。
Analyze を押すと全バンクを走査して、ミラーの検出を行います。
ROMサイズの調査や、SRAMバンクの特定に使えます。

- `16 x 16` のグリッドで `00-FF` Bank を表示
- 新規データは赤文字
- ミラーは `(XX)` 形式のグレー文字
   - XX にはミラー元のバンク番号が入ります

## キー操作

**Flat 表示:**

- `Left` / `Right` / `Up` / `Down`
- `Home` / `End`
- `PageUp` / `PageDown`: `-0100h` / `+0100h`
- `Ctrl+PageUp` / `Ctrl+PageDown`: `-0800h` / `+0800h`

**Mapper 表示:**

Flat表示のキー操作に加え、カーソル移動で Bank 境界をまたいで遷移します

## 保存機能

保存ダイアログでは `Flat` / `Mapper` を選べます。

### Flat 保存

- 保存ファイル名
- 開始アドレス
- 終了アドレス

### Mapper 保存

- 保存ファイル名
- `Type`
- `Window`
- `Switch`
- `StartBank`
- `EndBank`

既定の拡張子は `.rom` です。
`Analyze` を一度でも実行している場合、`EndBank` の既定値は最後に検知したサイズに応じて変わります。

## 注意

- Mapper 保存は、表示中バッファをそのまま保存するのではなく、指定した条件で Adapter から読み直して保存します
