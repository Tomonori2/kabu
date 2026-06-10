# -*- coding: utf-8 -*-
import os
from datetime import date

import pandas as pd
import streamlit as st
import yfinance as yf
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


def save_trade(name: str, code: str, baibai: str, shares: int, price: int):
    sb = get_supabase()
    row = {
        "date": date.today().isoformat(),
        "name": name,
        "baibai": baibai,
        "shares": shares,
        "price": price,
    }
    if code:
        row["code"] = code
    sb.table("trades").insert(row).execute()


@st.cache_data(ttl=300)
def fetch_price(code: str):
    """東証の現在株価を取得する（約20分遅れの参考値）。取得できなければ None"""
    try:
        ticker = yf.Ticker(f"{code}.T")
        price = ticker.fast_info["last_price"]
        return float(price) if price else None
    except Exception:
        return None


def calc_profit(trades: list):
    holdings = {}
    realized = []  # 売却1回ごとの実現損益（分析用）
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
                profit = (price - avg) * shares
                h["profit"] += profit
                h["cost"] -= avg * shares
                h["shares"] -= shares
                realized.append({"date": str(t["date"]), "name": name, "profit": profit})

    return holdings, realized


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

tab1, tab2, tab3, tab4, tab5 = st.tabs(["➕ 追加", "📋 履歴", "💰 損益", "📊 保有", "📈 分析"])

# ---- タブ1: 取引追加 ----
with tab1:
    st.subheader("取引を追加する")
    with st.form("add_trade"):
        name = st.text_input("銘柄名（例: トヨタ）")
        code = st.text_input(
            "証券コード（任意・例: 7203）",
            help="入力すると保有状況タブに現在株価と含み損益が表示されます",
        )
        baibai = st.radio("売買", ["買", "売"], horizontal=True)
        shares = st.number_input("株数", min_value=1, step=1, value=1)
        price = st.number_input("単価（円）", min_value=1, step=1, value=1)
        submitted = st.form_submit_button("記録する", use_container_width=True)

    if submitted:
        if not name.strip():
            st.error("銘柄名を入力してください。")
        else:
            try:
                save_trade(name.strip(), code.strip(), baibai, int(shares), int(price))
                st.success(f"記録しました: {name} / {baibai} / {int(shares)}株 / {int(price):,}円")
                st.cache_resource.clear()
            except Exception:
                st.error(
                    "保存に失敗しました。証券コードを入力した場合は、"
                    "Supabaseの trades テーブルに `code` 列（text）の追加が必要です。"
                    "PROGRESS.md の手順を確認してください。"
                )

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
        holdings, _ = calc_profit(trades)
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
    holdings, _ = calc_profit(trades)
    mochikabu = [(n, h) for n, h in holdings.items() if h["shares"] > 0]

    if not mochikabu:
        st.info("現在、保有している株はありません。")
    else:
        # 銘柄名 → 証券コードの対応（取引時に入力された最新のコードを使う）
        codes = {t["name"]: str(t["code"]) for t in trades if t.get("code")}

        total_fukumi = 0.0
        has_price = False
        for stock_name, h in mochikabu:
            avg = h["cost"] / h["shares"]
            stock_code = codes.get(stock_name)
            now = fetch_price(stock_code) if stock_code else None

            if now is not None:
                fukumi = (now - avg) * h["shares"]
                total_fukumi += fukumi
                has_price = True
                st.metric(
                    label=f"{stock_name}（{stock_code}）",
                    value=f'{h["shares"]}株',
                    delta=f"{fukumi:+,.0f}円（含み損益）",
                )
                st.caption(f"現在値 {now:,.0f}円 ／ 平均取得単価 {avg:,.0f}円")
            else:
                st.metric(
                    label=stock_name,
                    value=f'{h["shares"]}株',
                    delta=f"平均取得単価 {avg:,.0f}円",
                    delta_color="off",
                )
                if stock_code:
                    st.caption("株価を取得できませんでした（証券コードが正しいか確認してください）")
                else:
                    st.caption("証券コード未登録のため現在株価は表示されません（次の取引でコードを入力すると表示されます）")

        if has_price:
            st.divider()
            mark = "＋" if total_fukumi >= 0 else "－"
            st.markdown(f"**含み損益 合計: {mark}{abs(total_fukumi):,.0f}円**")
            st.caption("※ 株価は20分ほど遅れの参考値です")

# ---- タブ5: 分析 ----
with tab5:
    st.subheader("傾向分析")
    trades = load_trades()
    if not trades:
        st.info("まだ取引がありません。")
    else:
        holdings, realized = calc_profit(trades)

        if not realized:
            st.info("売却した取引がまだないため、損益の分析は表示できません。株を売却すると、ここに勝率や傾向が表示されます。")
        else:
            df = pd.DataFrame(realized)
            df["月"] = df["date"].str[:7]

            # ---- 成績サマリー ----
            total_count = len(df)
            win_df = df[df["profit"] > 0]
            lose_df = df[df["profit"] <= 0]
            win_rate = len(win_df) / total_count * 100

            c1, c2, c3 = st.columns(3)
            c1.metric("売却回数", f"{total_count}回")
            c2.metric("勝率", f"{win_rate:.0f}%")
            c3.metric("合計損益", f'{df["profit"].sum():,.0f}円')

            c1, c2 = st.columns(2)
            avg_win = win_df["profit"].mean() if len(win_df) else 0
            avg_lose = lose_df["profit"].mean() if len(lose_df) else 0
            c1.metric("勝ちの平均", f"＋{avg_win:,.0f}円")
            c2.metric("負けの平均", f"{avg_lose:,.0f}円")

            # ---- ひとことコメント ----
            by_name = df.groupby("name")["profit"].sum()
            best_name = by_name.idxmax()
            worst_name = by_name.idxmin()
            comments = []
            if by_name[best_name] > 0:
                comments.append(f"🏆 得意な銘柄は **{best_name}**（＋{by_name[best_name]:,.0f}円）")
            if by_name[worst_name] < 0:
                comments.append(f"⚠️ 苦手な銘柄は **{worst_name}**（{by_name[worst_name]:,.0f}円）")
            if len(lose_df) and avg_win and abs(avg_lose) > avg_win:
                comments.append("📉 負けたときの損失が、勝ったときの利益より大きい傾向があります（損切りは早めが鉄則）")
            for c in comments:
                st.markdown(c)

            st.divider()

            # ---- 累計損益の推移 ----
            st.markdown("##### 📈 累計損益の推移")
            cum = df.groupby("date")["profit"].sum().sort_index().cumsum()
            cum.index.name = "日付"
            st.line_chart(cum, use_container_width=True)

            # ---- 月別の実現損益 ----
            st.markdown("##### 🗓 月別の損益")
            monthly = df.groupby("月")["profit"].sum()
            st.bar_chart(monthly, use_container_width=True)

            # ---- 銘柄別の実現損益 ----
            st.markdown("##### 🏷 銘柄別の損益")
            st.bar_chart(by_name.sort_values(ascending=False), use_container_width=True)

        # ---- 月別の取引回数（買い・売り別）----
        st.markdown("##### 🔁 月別の取引回数")
        df_all = pd.DataFrame(trades)
        df_all["月"] = df_all["date"].astype(str).str[:7]
        counts = df_all.groupby(["月", "baibai"]).size().unstack(fill_value=0)
        counts = counts.rename(columns={"買": "買い", "売": "売り"})
        st.bar_chart(counts, use_container_width=True)
