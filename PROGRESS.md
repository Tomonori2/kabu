# 進捗メモ

## 現在の機能（2026-06-10更新）

- 🏠 ホーム: サーフィン画像＋実現損益・含み損益・保有数のダッシュボード
- ➕ 追加: 取引の記録（証券コードは任意入力）
- 📋 履歴: 取引一覧（新しい順）
- 📊 保有: 保有株と、証券コード登録銘柄は現在株価・含み損益
- 📈 分析: 勝率・累計損益・月別/銘柄別の成績・自動コメント
- テーマ: 海の青系、スマホ最適化済み

## 🎉 アプリ完成・公開済み（2026-06-10）

**アプリURL: https://kabu-tomonori.streamlit.app/**

スマホのホーム画面に追加しておくと、アプリのようにすぐ開けます。

## 構成

| 役割 | サービス | 場所 |
|---|---|---|
| コード置き場 | GitHub | このリポジトリ（ブランチ: `claude/continuation-from-yesterday-y47arv`） |
| アプリ実行 | Streamlit Community Cloud | https://kabu-tomonori.streamlit.app/ |
| データ保存 | Supabase | https://nnsctxvscnkbctylxrxs.supabase.co の `trades` テーブル |

- `trades` テーブルは Table Editor から手作業で作成（RLSはオフ = UNRESTRICTEDで正常）
- 列: `id`, `created_at`, `date`, `name`, `baibai`, `shares`, `price`
- Streamlit Cloud の Secrets に `SUPABASE_URL` / `SUPABASE_KEY` 設定済み

## 運用メモ

- 無料プランのため、しばらく使わないとアプリがスリープする。開いて1分ほど待てば起動する（故障ではない）
- Supabaseのダッシュボードを見るときは**ブラウザの翻訳機能をオフ**にする（オンだと画面が壊れる）
- データの中身は Supabase の Table Editor からも直接見られる

## ⚠️ 現在株価機能を使うための作業（1回だけ）

`trades` テーブルに `code` 列を追加する必要がある:

1. Table Editor を開く: https://supabase.com/dashboard/project/nnsctxvscnkbctylxrxs/editor
2. 左のリストの `trades` の横の「…」→「**Edit table**」
3. Columns の一番下の「**Add column**」→ Name: `code` ／ Type: `text`
4. 「**Save**」

これをやると、取引追加のときに証券コード（例: トヨタ=7203）を入力でき、
保有状況タブに現在株価と含み損益が表示される。

## 今後のアイデア（やりたくなったら）

- 取引の編集・削除機能（誤入力の修正）
- 損益のグラフ表示
- 現在株価の自動取得と含み損益の表示
- コードを `main` ブランチにマージして整理
