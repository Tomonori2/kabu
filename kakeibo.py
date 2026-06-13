# -*- coding: utf-8 -*-
import hmac
import json
import os
import re
from datetime import date, timedelta

import pandas as pd
import streamlit as st
from supabase import create_client, Client

CATEGORIES = ["食費", "外食", "日用品", "交通", "趣味・娯楽", "衣服・美容",
              "医療・健康", "光熱費", "通信", "その他"]

RESET_DAY = 16  # 家計簿の「ひと月」はこの日に始まる（16日〜翌月15日）


def period_key(d) -> str:
    """16日はじまりの『家計簿月』のキーを返す（開始する月で表す。例: 2026-06 = 6/16〜7/15）"""
    if not isinstance(d, date):
        d = date.fromisoformat(str(d)[:10])
    if d.day >= RESET_DAY:
        start = d.replace(day=1)
    else:
        start = (d.replace(day=1) - timedelta(days=1)).replace(day=1)
    return start.strftime("%Y-%m")


def period_label(key: str) -> str:
    """家計簿月のキーを「6/16〜7/15」のような表示にする"""
    y, m = map(int, key.split("-"))
    nm = 1 if m == 12 else m + 1
    return f"{m}/{RESET_DAY}〜{nm}/{RESET_DAY - 1}"


def get_config(key: str) -> str:
    # 環境変数 → .streamlit/secrets.toml の順に探す
    value = os.environ.get(key, "")
    if not value:
        try:
            value = st.secrets[key]
        except (KeyError, FileNotFoundError):
            value = ""
    return value


@st.cache_resource
def get_supabase() -> Client:
    url = get_config("SUPABASE_URL")
    key = get_config("SUPABASE_KEY")
    if not url or not key:
        st.error(
            "Supabase の接続情報が設定されていません。"
            "Secrets に `SUPABASE_URL` と `SUPABASE_KEY` を設定してください。"
        )
        st.stop()
    return create_client(url, key)


def load_expenses():
    """支出の記録を読む。テーブルが未作成なら None を返す"""
    try:
        res = get_supabase().table("expenses").select("*").order("date").order("id").execute()
        return res.data
    except Exception:
        return None


def save_expense(d: str, store: str, category: str, item: str, amount: int, memo: str = ""):
    row = {"date": d, "store": store, "category": category, "item": item, "amount": amount}
    if memo:
        row["memo"] = memo
    get_supabase().table("expenses").insert(row).execute()


def update_expense(expense_id, d: str, store: str, category: str, item: str, amount: int, memo: str = ""):
    get_supabase().table("expenses").update({
        "date": d, "store": store, "category": category,
        "item": item, "amount": amount, "memo": memo or None,
    }).eq("id", expense_id).execute()


def delete_expense(expense_id):
    get_supabase().table("expenses").delete().eq("id", expense_id).execute()


def load_setting(key: str):
    try:
        res = get_supabase().table("settings").select("value").eq("key", key).execute()
        return res.data[0]["value"] if res.data else ""
    except Exception:
        return None


def save_setting(key: str, value: str):
    get_supabase().table("settings").upsert({"key": key, "value": value}).execute()


def _gemini_clients():
    from google import genai

    api_key = get_config("GEMINI_API_KEY").strip()
    factories = [
        lambda: genai.Client(api_key=api_key),
        lambda: genai.Client(vertexai=True, api_key=api_key),
    ]
    if api_key.startswith("AQ."):
        factories.reverse()  # AQ.形式はVertex方式を先に試す
    return factories


def gemini_generate(messages: list) -> str:
    """会話履歴を渡してGeminiに回答してもらう（株アプリと同じ仕組み）"""
    from google.genai import types

    contents = [
        types.Content(
            role="user" if m["role"] == "user" else "model",
            parts=[types.Part.from_text(text=m["text"])],
        )
        for m in messages
    ]
    search_config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())]
    )
    last_err = None
    for make_client in _gemini_clients():
        try:
            client = make_client()
        except Exception as e:
            last_err = e
            continue
        for model in ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]:
            for config in (search_config, None):
                try:
                    resp = client.models.generate_content(model=model, contents=contents, config=config)
                    if resp.text:
                        return resp.text
                except Exception as e:
                    last_err = e
    raise last_err


def gemini_read_receipt(image_bytes: bytes, mime_type: str, per_item: bool = False) -> list:
    """レシートの写真をGeminiで読み取り、支出のリストにして返す"""
    from google.genai import types

    if per_item:
        prompt = f"""この画像はレシートの写真です。買った商品を1つずつ抽出して、JSONだけを出力してください。
形式: [{{"date": "YYYY-MM-DD"（不明ならnull）, "store": "店名", "category": "{'/'.join(CATEGORIES)} のどれか1つ（商品ごとに最適なもの）", "item": "商品名", "total": その商品の税込み価格の整数}}]
注意:
- 商品1つにつき配列の1要素にする（同じ商品が2個なら価格をまとめて1要素でよい）
- 割引があれば価格に反映する
- 小計・合計・消費税・ポイント・お預かり・お釣りなどの行は商品ではないので含めない
- JSON以外の文字は一切出力しない。"""
    else:
        prompt = f"""この画像はレシート（または支払い画面）の写真です。読み取れる支払いを抽出して、JSONだけを出力してください。
形式: [{{"date": "YYYY-MM-DD"（不明ならnull）, "store": "店名", "category": "{'/'.join(CATEGORIES)} のどれか1つ", "item": "主な品目を3つまで・で区切った文字列", "total": 合計金額の整数}}]
注意: 必ず配列で出力する。合計金額は税込みの支払い総額。JSON以外の文字は一切出力しない。"""
    contents = [types.Content(role="user", parts=[
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        types.Part.from_text(text=prompt),
    ])]

    text = None
    last_err = None
    for make_client in _gemini_clients():
        try:
            client = make_client()
        except Exception as e:
            last_err = e
            continue
        for model in ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"]:
            try:
                resp = client.models.generate_content(model=model, contents=contents)
                if resp.text:
                    text = resp.text
                    break
            except Exception as e:
                last_err = e
        if text:
            break
    if text is None:
        raise last_err

    m = re.search(r"\[.*\]|\{.*\}", text, re.S)
    if not m:
        return []
    data = json.loads(m.group(0))
    if isinstance(data, dict):
        data = [data]
    result = []
    for d in data:
        try:
            item = {
                "date": str(d.get("date") or date.today().isoformat())[:10],
                "store": str(d.get("store") or "").strip(),
                "category": d.get("category") if d.get("category") in CATEGORIES else "その他",
                "item": str(d.get("item") or "").strip(),
                "amount": int(round(float(d.get("total") or 0))),
            }
        except (TypeError, ValueError):
            continue
        if item["amount"] > 0:
            result.append(item)
    return result


def run_sensei(prompt: str):
    """高坂先生との会話を新しく始めて、最初の回答をもらう"""
    st.session_state.kakeibo_messages = [{"role": "user", "text": prompt, "hidden": True}]
    with st.spinner("高坂先生が考え中です…（30秒ほどかかることがあります）"):
        try:
            answer = gemini_generate(st.session_state.kakeibo_messages)
            st.session_state.kakeibo_messages.append({"role": "model", "text": answer})
        except Exception as e:
            st.session_state.kakeibo_messages = []
            st.error("AIの呼び出しに失敗しました。下のエラー内容を確認してください。")
            st.code(str(e))


# ---- ページ設定 ----
st.set_page_config(page_title="家計簿", page_icon="🧾", layout="centered")

# スマホアプリ風の見た目調整（株アプリと同じ・色はあたたかいオレンジ系）
st.markdown("""
<style>
#MainMenu, footer, header {visibility: hidden;}
.block-container {padding-top: 1.2rem; padding-bottom: 4rem; padding-left: 1rem; padding-right: 1rem;}
.stTabs [data-baseweb="tab-list"] {
    gap: 4px;
    position: sticky;
    top: 0;
    background: #ffffff;
    z-index: 99;
    border-radius: 0 0 12px 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
.stTabs [data-baseweb="tab"] {
    font-size: 15px;
    font-weight: 600;
    padding: 10px 6px;
    flex: 1;
}
.stForm button, .stButton button {
    height: 3.2rem;
    font-size: 17px;
    font-weight: 700;
    border-radius: 14px;
}
.stTextInput input, .stNumberInput input {
    font-size: 16px;
    height: 2.8rem;
    border-radius: 12px;
}
div[data-testid="stMetric"] {
    background: #fdf8f2;
    border: 1px solid #f3e5d3;
    border-radius: 16px;
    padding: 14px 18px;
    margin-bottom: 10px;
}
</style>
""", unsafe_allow_html=True)

# ---- パスワード保護（株アプリと同じパスワード）----
APP_PASSWORD = get_config("APP_PASSWORD")
if APP_PASSWORD and not st.session_state.get("authed"):
    st.title("🧾 家計簿")
    st.markdown("このアプリはパスワードで保護されています。")
    with st.form("login"):
        pw = st.text_input("パスワード", type="password")
        login = st.form_submit_button("ログイン", use_container_width=True, type="primary")
    if login:
        if hmac.compare_digest(pw, APP_PASSWORD):
            st.session_state.authed = True
            st.rerun()
        else:
            st.error("パスワードが違います。")
    st.stop()

st.title("🧾 家計簿")

SENSEI_AVATAR = "assets/sensei.png" if os.path.exists("assets/sensei.png") else "👨‍🏫"

expenses = load_expenses()
if expenses is None:
    st.error(
        "支出をしまうテーブルがまだありません。SupabaseのSQL Editorで "
        "PROGRESS.md にある expenses テーブル作成のSQLを実行してください。"
    )
    st.stop()

tab_home, tab_add, tab_list, tab_chart, tab_ai = st.tabs(
    ["🏠 ホーム", "➕ 追加", "📋 履歴", "📈 分析", "🤖 AI"]
)

this_month = period_key(date.today())

# ---- タブ: ホーム ----
with tab_home:
    if not expenses:
        st.info("ようこそ！「➕ 追加」タブでレシートを撮って読み込むか、手入力で最初の支出を記録してみましょう。")
    else:
        month_exp = [e for e in expenses if period_key(e["date"]) == this_month]
        month_total = sum(int(e["amount"]) for e in month_exp)

        budget_raw = load_setting("kakeibo_budget")
        budget = int(budget_raw) if budget_raw and str(budget_raw).isdigit() else 0

        c1, c2 = st.columns(2)
        c1.metric(f"今月の支出（{period_label(this_month)}）", f"{month_total:,}円")
        if budget > 0:
            nokori = budget - month_total
            c2.metric(
                "💰 残り使える額",
                f"{nokori:,}円",
                delta=f"予算 {budget:,}円",
                delta_color="off",
            )
        else:
            c2.metric("今月の記録", f"{len(month_exp)}件")
        st.caption(f"※ 家計簿の「ひと月」は毎月{RESET_DAY}日にリセットされます")

        # ---- 今月の予算 ----
        if budget_raw is not None:
            if budget > 0:
                pct = min(month_total / budget, 1.0)
                nokori = budget - month_total
                st.progress(pct, text=f"💰 予算 {budget:,}円 のうち {month_total:,}円 使用（{month_total / budget * 100:.0f}%）")
                if nokori < 0:
                    st.error(f"⚠️ 予算を {-nokori:,}円 オーバーしています！")
                elif pct >= 0.8:
                    st.warning(f"⚠️ 予算の8割を超えました。残り {nokori:,}円 です。")
            with st.expander("💰 毎月の予算を設定する"):
                new_budget = st.number_input(
                    "毎月の予算（円）", min_value=0, step=5000, value=budget,
                    help="0にすると予算ゲージは非表示になります",
                )
                if st.button("予算を保存", use_container_width=True):
                    try:
                        save_setting("kakeibo_budget", str(int(new_budget)))
                    except Exception as e:
                        st.error(
                            "保存に失敗しました。settings テーブルが書き込み禁止（RLS有効）に"
                            "なっている可能性があります。SQL Editorで "
                            "`alter table settings disable row level security;` を実行してください。"
                        )
                        st.code(str(e))
                    else:
                        st.rerun()

        # ---- 今月のカテゴリ別 ----
        if month_exp:
            st.markdown("##### 🧺 今月のカテゴリ別")
            dfm = pd.DataFrame(month_exp)
            by_cat = dfm.groupby("category")["amount"].sum().sort_values(ascending=False)
            st.bar_chart(by_cat, use_container_width=True)
            top = by_cat.index[0]
            st.caption(f"いちばん使っているのは **{top}**（{by_cat.iloc[0]:,.0f}円）です")

        # ---- 最近の支出 ----
        st.divider()
        st.markdown("##### 🕒 最近の支出")
        for e in list(reversed(expenses))[:3]:
            store = f'　{e["store"]}' if e.get("store") else ""
            st.markdown(f'- {e["date"]}{store}　**{int(e["amount"]):,}円**（{e["category"]}）')

# ---- タブ: 追加 ----
with tab_add:
    st.subheader("📷 レシートを読み込む")
    st.caption("レシートをスマホで撮って選ぶと、高坂先生が日付・店名・金額を読み取ってくれます")
    up = st.file_uploader("レシートの写真を選ぶ", type=["png", "jpg", "jpeg", "webp", "heic", "heif"],
                          label_visibility="collapsed")
    read_mode = st.radio(
        "記録のしかた",
        ["🧾 合計でまとめて1件", "🛒 一品ずつ"],
        horizontal=True,
        help="「一品ずつ」は商品1つ1つを別の記録にして、分類も商品ごとに自動で振り分けます",
    )
    if up is not None and st.button("📷 読み取る", use_container_width=True, type="primary"):
        if not get_config("GEMINI_API_KEY"):
            st.error("Geminiの設定がまだです（Secrets に GEMINI_API_KEY を追加してください）。")
        else:
            with st.spinner("高坂先生が読み取り中です…"):
                try:
                    found = gemini_read_receipt(
                        up.getvalue(), up.type or "image/jpeg",
                        per_item=read_mode.startswith("🛒"),
                    )
                    if found:
                        st.session_state.scan_receipt = found
                    else:
                        st.session_state.scan_receipt = None
                        st.warning("読み取れませんでした。金額が写るように撮り直してみてください。")
                except Exception as e:
                    st.error("読み取りに失敗しました。")
                    st.code(str(e))

    if st.session_state.get("scan_receipt"):
        st.markdown("**読み取り結果（確認してから記録してください）:**")
        st.dataframe(
            [{"日付": s["date"], "店": s["store"] or "—", "分類": s["category"],
              "品目": s["item"] or "—", "金額（円）": f'{s["amount"]:,}'}
             for s in st.session_state.scan_receipt],
            use_container_width=True, hide_index=True,
        )
        c1, c2 = st.columns(2)
        if c1.button("✅ この内容で記録する", use_container_width=True, type="primary"):
            for s in st.session_state.scan_receipt:
                try:
                    d = date.fromisoformat(s["date"]).isoformat()
                except ValueError:
                    d = date.today().isoformat()
                save_expense(d, s["store"], s["category"], s["item"], s["amount"])
            saved_count = len(st.session_state.scan_receipt)
            st.session_state.scan_receipt = None
            st.toast(f"{saved_count}件 記録しました 🧾")
            st.rerun()
        if c2.button("❌ やり直す", use_container_width=True):
            st.session_state.scan_receipt = None
            st.rerun()

    # ---- 手入力 ----
    st.divider()
    st.subheader("✍️ 手で入力する")
    with st.form("add_expense"):
        e_date = st.date_input("日付", value=date.today())
        e_store = st.text_input("店名（任意・例: スーパーマルエツ）")
        e_category = st.selectbox("分類", CATEGORIES)
        e_item = st.text_input("品目（任意・例: 牛乳・パン）")
        e_amount = st.number_input("金額（円）", min_value=1, step=100, value=1000)
        e_memo = st.text_input("メモ（任意）")
        sub = st.form_submit_button("記録する", use_container_width=True)
    if sub:
        save_expense(e_date.isoformat(), e_store.strip(), e_category,
                     e_item.strip(), int(e_amount), e_memo.strip())
        st.success(f"記録しました: {e_date} / {e_category} / {int(e_amount):,}円")

# ---- タブ: 履歴 ----
with tab_list:
    st.subheader("支出の履歴")
    if not expenses:
        st.info("まだ記録がありません。")
    else:
        rows = [
            {"日付": e["date"], "店": e.get("store") or "", "分類": e["category"],
             "品目": e.get("item") or "", "金額（円）": f'{int(e["amount"]):,}',
             "メモ": e.get("memo") or ""}
            for e in reversed(expenses)
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)

        # ---- CSVダウンロード ----
        df_csv = pd.DataFrame(expenses)
        cols = [c for c in ["date", "store", "category", "item", "amount", "memo"] if c in df_csv.columns]
        df_csv = df_csv[cols].rename(columns={
            "date": "日付", "store": "店", "category": "分類",
            "item": "品目", "amount": "金額", "memo": "メモ",
        })
        st.download_button(
            "📥 全データをダウンロード（Excel用CSV）",
            df_csv.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"家計簿_{date.today().isoformat()}.csv",
            mime="text/csv",
            use_container_width=True,
        )

        # ---- 修正・削除 ----
        st.divider()
        st.markdown("##### ✏️ 記録の修正・削除")
        options = {
            f'{e["date"]} ／ {e.get("store") or e["category"]} ／ {int(e["amount"]):,}円': e
            for e in reversed(expenses)
        }
        selected = st.selectbox("直したい記録を選んでください", list(options.keys()))
        e = options[selected]

        with st.form("edit_expense"):
            m_date = st.date_input("日付", value=date.fromisoformat(str(e["date"])))
            m_store = st.text_input("店名", value=e.get("store") or "")
            m_category = st.selectbox(
                "分類", CATEGORIES,
                index=CATEGORIES.index(e["category"]) if e["category"] in CATEGORIES else len(CATEGORIES) - 1,
            )
            m_item = st.text_input("品目", value=e.get("item") or "")
            m_amount = st.number_input("金額（円）", min_value=1, step=100, value=int(e["amount"]))
            m_memo = st.text_input("メモ", value=e.get("memo") or "")
            delete_check = st.checkbox("🗑 この記録を削除する（削除のときだけチェック）")
            c1, c2 = st.columns(2)
            save_btn = c1.form_submit_button("✏️ 修正を保存", use_container_width=True)
            delete_btn = c2.form_submit_button("🗑 削除する", use_container_width=True)

        if save_btn:
            update_expense(e["id"], m_date.isoformat(), m_store.strip(), m_category,
                           m_item.strip(), int(m_amount), m_memo.strip())
            st.toast("修正しました ✏️")
            st.rerun()
        if delete_btn:
            if delete_check:
                delete_expense(e["id"])
                st.toast("削除しました 🗑")
                st.rerun()
            else:
                st.warning("削除するには「この記録を削除する」にチェックを入れてから押してください。")

# ---- タブ: 分析 ----
with tab_chart:
    st.subheader("支出の分析")
    if not expenses:
        st.info("まだ記録がありません。")
    else:
        df = pd.DataFrame(expenses)
        df["月"] = df["date"].apply(period_key)

        st.markdown("##### 🗓 月別の支出")
        monthly = df.groupby("月")["amount"].sum()
        st.bar_chart(monthly, use_container_width=True)
        month_rows = [
            {"月": m, "期間": period_label(m), "支出": f"{int(v):,}円"}
            for m, v in monthly.sort_index(ascending=False).items()
        ]
        st.dataframe(month_rows, use_container_width=True, hide_index=True)
        st.caption(f"※ 月は{RESET_DAY}日はじまり（{RESET_DAY}日〜翌月{RESET_DAY - 1}日）で区切っています")

        st.markdown("##### 🧺 カテゴリ別（全期間）")
        by_cat = df.groupby("category")["amount"].sum().sort_values(ascending=False)
        st.bar_chart(by_cat, use_container_width=True)

        st.markdown("##### 🏪 よく使うお店（全期間）")
        stores = df[df["store"].astype(bool)]
        if not stores.empty:
            by_store = stores.groupby("store")["amount"].sum().sort_values(ascending=False)[:10]
            store_rows = [{"店": s, "合計": f"{int(v):,}円"} for s, v in by_store.items()]
            st.dataframe(store_rows, use_container_width=True, hide_index=True)
        else:
            st.caption("店名つきの記録がたまると表示されます。")

# ---- タブ: AI ----
with tab_ai:
    if os.path.exists("assets/sensei_hero.jpg"):
        st.image("assets/sensei_hero.jpg", use_container_width=True)
    st.subheader("👨‍🏫 高坂先生の家計相談")

    if st.button("👨‍🏫 今月の家計を見てもらう", use_container_width=True, type="primary"):
        if not get_config("GEMINI_API_KEY"):
            st.error("Geminiの設定がまだです（Secrets に GEMINI_API_KEY を追加してください）。")
        elif not expenses:
            st.error("まだ支出の記録がありません。")
        else:
            df = pd.DataFrame(expenses)
            df["月"] = df["date"].apply(period_key)
            recent_months = sorted(df["月"].unique())[-2:]
            lines = []
            for m in recent_months:
                dfm = df[df["月"] == m]
                lines.append(f"■ {period_label(m)} 合計 {int(dfm['amount'].sum()):,}円")
                for cat, v in dfm.groupby("category")["amount"].sum().sort_values(ascending=False).items():
                    lines.append(f"  - {cat}: {int(v):,}円")
            budget_raw = load_setting("kakeibo_budget")
            budget_text = f"毎月の予算: {int(budget_raw):,}円" if budget_raw and str(budget_raw).isdigit() and int(budget_raw) > 0 else "予算は未設定"

            prompt = f"""あなたは「高坂先生」。親しみやすく頼れる家計の先生です。やさしい日本語で、この生徒の家計を見てあげてください。

【最近の支出（月別・カテゴリ別）】
{chr(10).join(lines)}

【予算】
{budget_text}

以下の構成で、マークダウンで簡潔に（全体で500字程度）:
1. **今月のようす** — 支出の全体感をひとことで
2. **よくできているところ** — ほめられる点
3. **見直せそうなところ** — 削れそうなカテゴリと具体的なコツを1〜2個
4. **高坂先生からのひとこと** — 前向きな応援

このあと生徒から追加の質問が来たら、高坂先生として同じ調子で会話を続けてください。"""
            run_sensei(prompt)

    # ---- 会話の表示 ----
    for m in st.session_state.get("kakeibo_messages", []):
        if m.get("hidden"):
            continue
        if m["role"] == "model":
            with st.chat_message("ai", avatar=SENSEI_AVATAR):
                st.markdown(m["text"])
        else:
            with st.chat_message("user"):
                st.markdown(m["text"])

    # ---- 追い質問（チャット）----
    if st.session_state.get("kakeibo_messages"):
        question = st.chat_input("高坂先生に追い質問する（例: 食費を減らすコツは？）")
        if question:
            st.session_state.kakeibo_messages.append({"role": "user", "text": question})
            with st.spinner("高坂先生が考え中…"):
                try:
                    answer = gemini_generate(st.session_state.kakeibo_messages)
                    st.session_state.kakeibo_messages.append({"role": "model", "text": answer})
                except Exception as e:
                    st.error("AIの呼び出しに失敗しました。")
                    st.code(str(e))
            st.rerun()
