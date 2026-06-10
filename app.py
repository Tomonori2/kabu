# -*- coding: utf-8 -*-
import os
from datetime import date

import streamlit as st
from supabase import create_client, Client

TAX_RATE = 0.20315


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
            "Supabase の接続情報が設定されていません。\n\n"
            "環境変数 `SUPABASE_URL` と `SUPABASE_KEY` を設定するか、"
            "`.streamlit/secrets.toml` に記載してください（README 参照）。"
        )
        st.stop()
    return create_client(url, key)


def load_trades():
    sb = get_supabase()
    result = sb.table("trades").select("*").order("date").order("id").execute()
    return result.data


def save_trade(name: str, baibai: str, shares: int, price: int):
    sb = get_supabase()
    sb.table("trades").insert({
        "date": date.today().isoformat(),
        "name": name,
        "baibai": baibai,
        "shares": shares,
        "price": price,
    }).execute()


def calc_profit(trades: list) -> dict:
    holdings = {}
    for t in trades:
        name = t["name"]
        baibai = t["baibai"]
        shares = int(t["shares"])
        price = int(t["price"])

        if name not in holdings:
            holdings[name] = {"shares": 0, "cost": 0.0, "profit": 0.0}

        h = holdings[name]
        if baibai == "買":
            h["shares"] += shares
            h["cost"] += shares * price
        else:
            if h["shares"] >= shares:
                avg = h["cost"] / h["shares"]
                h["profit"] += (price - avg) * shares
                h["cost"] -= avg * shares
                h["shares"] -= shares

    return holdings


# ---- ページ設定 ----
st.set_page_config(page_title="株 損益アプリ", page_icon="📈", layout="centered")

# スマホアプリ風の見た目調整
st.markdown("""
<style>
/* Streamlitのメニュー・フッターを隠してアプリらしく */
#MainMenu, footer, header {visibility: hidden;}

/* 余白を詰めて画面を広く使う */
.block-container {padding-top: 1.2rem; padding-bottom: 4rem; padding-left: 1rem; padding-right: 1rem;}

/* タブをボトムナビ風に大きく・押しやすく */
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

/* ボタンを指で押しやすい大きさに */
.stForm button, .stButton button {
    height: 3.2rem;
    font-size: 17px;
    font-weight: 700;
    border-radius: 14px;
}

/* 入力欄も大きめに */
.stTextInput input, .stNumberInput input {
    font-size: 16px;
    height: 2.8rem;
    border-radius: 12px;
}

/* メトリクスをカード風に */
div[data-testid="stMetric"] {
    background: #f7faf7;
    border: 1px solid #e3ece3;
    border-radius: 16px;
    padding: 14px 18px;
    margin-bottom: 10px;
}
</style>
""", unsafe_allow_html=True)

st.title("📈 株 取引損益アプリ")

tab1, tab2, tab3, tab4 = st.tabs(["➕ 追加", "📋 履歴", "💰 損益", "📊 保有"])

# ---- タブ1: 取引追加 ----
with tab1:
    st.subheader("取引を追加する")
    with st.form("add_trade"):
        name = st.text_input("銘柄名（例: トヨタ）")
        baibai = st.radio("売買", ["買", "売"], horizontal=True)
        shares = st.number_input("株数", min_value=1, step=1, value=1)
        price = st.number_input("単価（円）", min_value=1, step=1, value=1)
        submitted = st.form_submit_button("記録する", use_container_width=True)

    if submitted:
        if not name.strip():
            st.error("銘柄名を入力してください。")
        else:
            save_trade(name.strip(), baibai, int(shares), int(price))
            st.success(f"記録しました: {name} / {baibai} / {int(shares)}株 / {int(price):,}円")
            st.cache_resource.clear()

# ---- タブ2: 取引履歴 ----
with tab2:
    st.subheader("取引履歴")
    trades = load_trades()
    if not trades:
        st.info("まだ取引がありません。")
    else:
        rows = [
            {"日付": t["date"], "銘柄": t["name"], "売買": t["baibai"],
             "株数": t["shares"], "単価（円）": f'{t["price"]:,}'}
            for t in reversed(trades)  # 新しい取引を上に表示
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)

# ---- タブ3: 損益 ----
with tab3:
    st.subheader("損益（実現損益）")
    trades = load_trades()
    if not trades:
        st.info("まだ取引がありません。")
    else:
        holdings = calc_profit(trades)
        total = 0
        for stock_name, h in holdings.items():
            profit = round(h["profit"])
            total += profit
            mark = "＋" if profit >= 0 else "－"
            delta_color = "normal" if profit >= 0 else "inverse"
            st.metric(
                label=stock_name,
                value=f"{mark}{abs(profit):,}円",
                delta_color=delta_color,
            )

        st.divider()
        total_mark = "＋" if total >= 0 else "－"
        st.markdown(f"**合計損益（税引き前）: {total_mark}{abs(round(total)):,}円**")

        if total > 0:
            tax = round(total * TAX_RATE)
            tedori = total - tax
            st.markdown(f"税金（{TAX_RATE * 100:.3f}%）: －{tax:,}円")
            st.success(f"手取り（税引き後）: ＋{tedori:,}円")
        else:
            st.info("損失のため、税金はかかりません。")

# ---- タブ4: 保有状況 ----
with tab4:
    st.subheader("保有状況")
    trades = load_trades()
    holdings = calc_profit(trades)
    mochikabu = [(n, h) for n, h in holdings.items() if h["shares"] > 0]

    if not mochikabu:
        st.info("現在、保有している株はありません。")
    else:
        for stock_name, h in mochikabu:
            avg = round(h["cost"] / h["shares"])
            st.metric(
                label=stock_name,
                value=f'{h["shares"]}株',
                delta=f"平均取得単価 {avg:,}円",
                delta_color="off",
            )
