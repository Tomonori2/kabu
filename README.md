# 📈 株 取引損益アプリ

Streamlit + Supabase で作った、株の取引を記録して実現損益（税引き後の手取りまで）を計算するアプリです。

## セットアップ手順

### 1. Supabase プロジェクトを作る

1. [supabase.com](https://supabase.com) にログインし、**New Project** でプロジェクトを作成
2. プロジェクトが起動するまで1〜2分待つ

### 2. テーブルを作る

1. 左メニューの **SQL Editor** を開く
2. このリポジトリの [`schema.sql`](schema.sql) の中身を貼り付けて **Run**
3. 「Success. No rows returned」と出れば成功（**Table Editor** に `trades` テーブルができています）

> `schema.sql` はテーブル作成に加えて、anon キーで読み書きできるように
> RLS（行レベルセキュリティ）のポリシーも設定します。
> これを忘れると「アプリは動くのにデータが保存・表示されない」状態になります。

### 3. 接続情報を取得する

Supabase ダッシュボードの **Settings → API** から以下をコピー:

| 項目 | 場所 |
|---|---|
| `SUPABASE_URL` | Project URL（`https://xxxx.supabase.co`） |
| `SUPABASE_KEY` | Project API keys の **anon public** キー |

### 4. 接続情報をアプリに渡す

どちらかの方法で設定します。

**方法A: 環境変数**

```bash
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_KEY="eyJhbGci..."
```

**方法B: Streamlit secrets（Streamlit Community Cloud にデプロイする場合はこちら）**

`.streamlit/secrets.toml` を作成:

```toml
SUPABASE_URL = "https://xxxx.supabase.co"
SUPABASE_KEY = "eyJhbGci..."
```

> ⚠️ `secrets.toml` は `.gitignore` 済みです。キーを GitHub にコミットしないでください。

### 5. 起動する

```bash
pip install -r requirements.txt
streamlit run app.py
```

ブラウザで `http://localhost:8501` が開きます。

## トラブルシューティング

- **「Supabase の接続情報が設定されていません」と出る**
  → 手順4の環境変数 or `secrets.toml` を確認。設定後はアプリを再起動してください。
- **記録しても履歴に何も出ない／エラーになる**
  → `schema.sql` を最後まで実行したか確認（RLS ポリシーまで含めて Run する必要があります）。
- **`relation "trades" does not exist` というエラー**
  → テーブルが未作成です。手順2を実行してください。
