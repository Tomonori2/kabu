# -*- coding: utf-8 -*-
import os
import time
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


def save_trade(trade_date: str, name: str, code: str, baibai: str, shares: int, price: int):
    sb = get_supabase()
    row = {
        "date": trade_date,
        "name": name,
        "baibai": baibai,
        "shares": shares,
        "price": price,
    }
    if code:
        row["code"] = code
    sb.table("trades").insert(row).execute()


def update_trade(trade_id, date_str: str, name: str, code: str, baibai: str, shares: int, price: int):
    sb = get_supabase()
    sb.table("trades").update({
        "date": date_str,
        "name": name,
        "code": code or None,
        "baibai": baibai,
        "shares": shares,
        "price": price,
    }).eq("id", trade_id).execute()


def delete_trade(trade_id):
    sb = get_supabase()
    sb.table("trades").delete().eq("id", trade_id).execute()


def get_stock_summary(code: str) -> str:
    """yfinanceから会社の基本データを集めて、AIに渡す文章にする"""
    lines = []
    try:
        t = yf.Ticker(f"{code}.T")
        hist = t.history(period="1y")
        if not hist.empty:
            now = hist["Close"].iloc[-1]
            lines.append(f"現在株価: {now:,.0f}円")
            for label, days in [("1か月前", 21), ("半年前", 126), ("1年前", 248)]:
                if len(hist) > days:
                    past = hist["Close"].iloc[-days - 1]
                    lines.append(f"{label}と比べた株価: {(now / past - 1) * 100:+.1f}%")
            lines.append(f"直近1年の高値: {hist['High'].max():,.0f}円 ／ 安値: {hist['Low'].min():,.0f}円")
        info = t.info or {}
        if info.get("longName"):
            lines.append(f"会社名: {info['longName']}")
        if info.get("sector"):
            lines.append(f"業種: {info['sector']}")
        if info.get("marketCap"):
            lines.append(f"時価総額: 約{info['marketCap'] / 1e8:,.0f}億円")
        if info.get("trailingPE"):
            lines.append(f"PER（株価収益率）: {info['trailingPE']:.1f}倍")
        if info.get("dividendYield"):
            lines.append(f"配当利回り（生データ）: {info['dividendYield']}")
        # 決算データ（売上高・純利益の推移）
        fin = t.financials
        if fin is not None and not fin.empty:
            for label, key in [("売上高", "Total Revenue"), ("純利益", "Net Income")]:
                if key in fin.index:
                    vals = []
                    for col in list(fin.columns)[:3][::-1]:
                        v = fin.loc[key, col]
                        if pd.notna(v):
                            vals.append(f"{col.year}年: {v / 1e8:,.0f}億円")
                    if vals:
                        lines.append(f"{label}の推移: " + " → ".join(vals))
    except Exception:
        pass
    return "\n".join(lines) if lines else "（株価データは取得できませんでした）"


def gemini_generate(messages: list) -> str:
    """会話履歴を渡してGeminiに回答してもらう。

    賢い順にモデルを試し、Google検索つき → なしの順でフォールバックする。
    キーの形式（AIza / AQ.）による接続方式の違いも自動で吸収する。
    """
    from google import genai
    from google.genai import types

    api_key = get_config("GEMINI_API_KEY").strip()
    factories = [
        lambda: genai.Client(api_key=api_key),
        lambda: genai.Client(vertexai=True, api_key=api_key),
    ]
    if api_key.startswith("AQ."):
        factories.reverse()  # AQ.形式はVertex方式を先に試す

    contents = [
        types.Content(
            role="user" if m["role"] == "user" else "model",
            parts=[types.Part.from_text(text=m["text"])],
        )
        for m in messages
    ]
    # Google検索を使える設定（最新ニュースを反映できる）
    search_config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())]
    )

    last_err = None
    for attempt in range(2):  # 混雑時は少し待って全体をもう一周
        for make_client in factories:
            try:
                client = make_client()
            except Exception as e:
                last_err = e
                continue
            for model in ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]:
                for config in (search_config, None):
                    try:
                        resp = client.models.generate_content(
                            model=model, contents=contents, config=config
                        )
                        if resp.text:
                            return resp.text
                    except Exception as e:
                        last_err = e
        if attempt == 0:
            time.sleep(4)
    raise last_err


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
st.set_page_config(page_title="株取引", page_icon="assets/icon.png", layout="centered")

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
    background: #f4fafd;
    border: 1px solid #dcedf6;
    border-radius: 16px;
    padding: 14px 18px;
    margin-bottom: 10px;
}

/* ヒーロー画像を角丸カードに */
div[data-testid="stImage"] img {
    border-radius: 18px;
    box-shadow: 0 4px 14px rgba(14,165,233,0.25);
}
</style>
""", unsafe_allow_html=True)

st.title("📈 株取引")

tab_home, tab1, tab2, tab4, tab5, tab_ai = st.tabs(
    ["🏠 ホーム", "➕ 追加", "📋 履歴", "📊 保有", "📈 分析", "🤖 AI"]
)

# ---- タブ: ホーム（ダッシュボード）----
with tab_home:
    st.image("assets/hero.jpg", use_container_width=True)
    trades = load_trades()
    if not trades:
        st.info("ようこそ！「➕ 追加」タブから最初の取引を記録してみましょう。")
    else:
        holdings, realized = calc_profit(trades)
        total = round(sum(h["profit"] for h in holdings.values()))
        mochikabu = [(n, h) for n, h in holdings.items() if h["shares"] > 0]

        # 含み損益（証券コード登録済みの保有銘柄のみ）
        codes = {t["name"]: str(t["code"]) for t in trades if t.get("code")}
        fukumi_total = 0.0
        has_price = False
        for stock_name, h in mochikabu:
            stock_code = codes.get(stock_name)
            now = fetch_price(stock_code) if stock_code else None
            if now is not None:
                fukumi_total += (now - h["cost"] / h["shares"]) * h["shares"]
                has_price = True

        c1, c2 = st.columns(2)
        c1.metric("実現損益（確定分）", f"{total:+,}円")
        c2.metric(
            "含み損益（保有分）",
            f"{fukumi_total:+,.0f}円" if has_price else "—",
        )
        if not has_price and mochikabu:
            st.caption("※ 含み損益は、取引追加で証券コードを入力した銘柄に表示されます")

        if total > 0:
            tax = round(total * TAX_RATE)
            st.caption(f"実現損益から税金（{TAX_RATE * 100:.3f}%）を引いた手取りは ＋{total - tax:,}円")

        c1, c2 = st.columns(2)
        c1.metric("保有銘柄", f"{len(mochikabu)}銘柄")
        c2.metric("取引回数", f"{len(trades)}回")

        # ---- 銘柄別の実現損益 ----
        kakutei = [(n, h) for n, h in holdings.items() if round(h["profit"]) != 0]
        if kakutei:
            st.divider()
            st.markdown("##### 💰 銘柄別の実現損益")
            for stock_name, h in kakutei:
                profit = round(h["profit"])
                st.metric(label=stock_name, value=f"{profit:+,}円")

        # ---- 最近の取引 ----
        st.divider()
        st.markdown("##### 🕒 最近の取引")
        for t in list(reversed(trades))[:3]:
            st.markdown(
                f'- {t["date"]}　**{t["name"]}**　{t["baibai"]}　'
                f'{t["shares"]}株 ／ {t["price"]:,}円'
            )

# ---- タブ1: 取引追加 ----
with tab1:
    st.subheader("取引を追加する")
    with st.form("add_trade"):
        trade_date = st.date_input("日付", value=date.today())
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
                save_trade(trade_date.isoformat(), name.strip(), code.strip(), baibai, int(shares), int(price))
                st.success(f"記録しました: {trade_date} / {name} / {baibai} / {int(shares)}株 / {int(price):,}円")
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

        # ---- 取引の修正・削除 ----
        st.divider()
        st.markdown("##### ✏️ 取引の修正・削除")
        options = {
            f'{t["date"]} ／ {t["name"]} ／ {t["baibai"]} ／ {t["shares"]}株 ／ {t["price"]:,}円':
            t for t in reversed(trades)
        }
        selected = st.selectbox("直したい取引を選んでください", list(options.keys()))
        t = options[selected]

        with st.form("edit_trade"):
            e_date = st.date_input("日付", value=date.fromisoformat(str(t["date"])))
            e_name = st.text_input("銘柄名", value=t["name"])
            e_code = st.text_input("証券コード（任意）", value=t.get("code") or "")
            e_baibai = st.radio("売買", ["買", "売"],
                                index=0 if t["baibai"] == "買" else 1, horizontal=True)
            e_shares = st.number_input("株数", min_value=1, step=1, value=int(t["shares"]))
            e_price = st.number_input("単価（円）", min_value=1, step=1, value=int(t["price"]))
            delete_check = st.checkbox("🗑 この取引を削除する（削除のときだけチェック）")
            c1, c2 = st.columns(2)
            save_btn = c1.form_submit_button("✏️ 修正を保存", use_container_width=True)
            delete_btn = c2.form_submit_button("🗑 削除する", use_container_width=True)

        if save_btn:
            if not e_name.strip():
                st.error("銘柄名を入力してください。")
            else:
                update_trade(t["id"], e_date.isoformat(), e_name.strip(),
                             e_code.strip(), e_baibai, int(e_shares), int(e_price))
                st.toast("修正しました ✏️")
                st.rerun()

        if delete_btn:
            if delete_check:
                delete_trade(t["id"])
                st.toast("削除しました 🗑")
                st.rerun()
            else:
                st.warning("削除するには「この取引を削除する」にチェックを入れてから押してください。")

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

# ---- タブ: AI分析（高坂先生）----
SENSEI_AVATAR = "assets/sensei.png" if os.path.exists("assets/sensei.png") else "👨‍🏫"

with tab_ai:
    st.image("assets/sensei_hero.jpg", use_container_width=True)
    st.subheader("👨‍🏫 高坂先生のAI株式分析")
    trades = load_trades()
    codes = {t["name"]: str(t["code"]) for t in trades if t.get("code")}
    names = list(dict.fromkeys(t["name"] for t in trades))

    if names:
        pick = st.selectbox("記録した銘柄から選ぶ", names)
    else:
        pick = None
        st.caption("取引を記録すると、ここから銘柄を選べるようになります。")

    with st.expander("記録にない会社を調べる"):
        manual_name = st.text_input("会社名（例: 任天堂）")
        manual_code = st.text_input("証券コード（例: 7974）")

    target_name = manual_name.strip() or pick
    target_code = manual_code.strip() or (codes.get(pick, "") if pick else "")

    if st.button("👨‍🏫 高坂先生に分析してもらう", use_container_width=True, type="primary"):
        if not get_config("GEMINI_API_KEY"):
            st.error(
                "Geminiの設定がまだです。share.streamlit.io でこのアプリの "
                "Settings → Secrets に `GEMINI_API_KEY = \"あなたのキー\"` を追加してください。"
            )
        elif not target_name:
            st.error("銘柄を選ぶか、会社名を入力してください。")
        else:
            # この銘柄の保有・取引状況をまとめる
            holdings, _ = calc_profit(trades)
            position = "この銘柄の取引記録はありません。"
            if target_name in holdings:
                h = holdings[target_name]
                parts = []
                if h["shares"] > 0:
                    avg = h["cost"] / h["shares"]
                    parts.append(f"現在 {h['shares']}株を保有（平均取得単価 {avg:,.0f}円）")
                if round(h["profit"]) != 0:
                    parts.append(f"これまでの実現損益 {round(h['profit']):+,}円")
                if parts:
                    position = "。".join(parts) + "。"

            stock_data = get_stock_summary(target_code) if target_code else "（証券コード未登録のため株価データなし）"
            prompt = f"""あなたは「高坂先生」。親しみやすく頼れる株式投資の先生です。投資初心者にもわかる日本語で、以下の会社を分析してください。
可能であればGoogle検索で最新のニュースや決算情報も確認して、分析に反映してください。

会社名: {target_name}（証券コード: {target_code or "不明"}）

【株価・会社データ】
{stock_data}

【この生徒の保有・取引状況】
{position}

以下の構成で、マークダウンで簡潔に書いてください（全体で600字程度）:
1. **どんな会社？** — 事業内容を2〜3行で
2. **業績と株価** — 売上・利益の推移と最近の株価の動き
3. **最新の話題** — 検索で見つかった直近のニュースがあれば
4. **あなたの持ち高との関係** — 保有がある場合、平均取得単価と現在の状況を踏まえて
5. **注目ポイント** — 良い材料とリスクを1つずつ

最後に「※これは参考情報です。投資の判断はご自身で行ってください。」と添えてください。
このあと生徒から追加の質問が来たら、高坂先生として同じ調子で会話を続けてください。"""

            st.session_state.ai_messages = [{"role": "user", "text": prompt, "hidden": True}]
            with st.spinner("高坂先生が分析中です…（高性能モードのため30秒ほどかかることがあります）"):
                try:
                    answer = gemini_generate(st.session_state.ai_messages)
                    st.session_state.ai_messages.append({"role": "model", "text": answer})
                except Exception as e:
                    st.session_state.ai_messages = []
                    st.error("AIの呼び出しに失敗しました。下のエラー内容を確認してください。")
                    st.code(str(e))

    # ---- 会話の表示 ----
    for m in st.session_state.get("ai_messages", []):
        if m.get("hidden"):
            continue
        if m["role"] == "model":
            with st.chat_message("ai", avatar=SENSEI_AVATAR):
                st.markdown(m["text"])
        else:
            with st.chat_message("user"):
                st.markdown(m["text"])

    # ---- 追い質問（チャット）----
    if st.session_state.get("ai_messages"):
        question = st.chat_input("高坂先生に追い質問する（例: リスクをもっと詳しく）")
        if question:
            st.session_state.ai_messages.append({"role": "user", "text": question})
            with st.spinner("高坂先生が考え中…"):
                try:
                    answer = gemini_generate(st.session_state.ai_messages)
                    st.session_state.ai_messages.append({"role": "model", "text": answer})
                except Exception as e:
                    st.error("AIの呼び出しに失敗しました。")
                    st.code(str(e))
            st.rerun()
