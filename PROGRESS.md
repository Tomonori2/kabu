# 進捗メモ

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

## 今後のアイデア（やりたくなったら）

- 取引の編集・削除機能（誤入力の修正）
- 損益のグラフ表示
- 現在株価の自動取得と含み損益の表示
- コードを `main` ブランチにマージして整理
