# -*- coding: utf-8 -*-
import hmac
import json
import os
import re
import time
import unicodedata
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


def save_trade(trade_date: str, name: str, code: str, baibai: str, shares: int, price: int, memo: str = ""):
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
    if memo:
        row["memo"] = memo
    sb.table("trades").insert(row).execute()


def update_trade(trade_id, date_str: str, name: str, code: str, baibai: str, shares: int, price: int, memo: str = ""):
    sb = get_supabase()
    sb.table("trades").update({
        "date": date_str,
        "name": name,
        "code": code or None,
        "baibai": baibai,
        "shares": shares,
        "price": price,
        "memo": memo or None,
    }).eq("id", trade_id).execute()


def load_dividends():
    """配当の記録を読む。テーブルが未作成なら None を返す"""
    try:
        return get_supabase().table("dividends").select("*").order("date").order("id").execute().data
    except Exception:
        return None


def save_dividend(d: str, name: str, amount: int):
    get_supabase().table("dividends").insert({"date": d, "name": name, "amount": amount}).execute()


def delete_dividend(div_id):
    get_supabase().table("dividends").delete().eq("id", div_id).execute()


def delete_trade(trade_id):
    sb = get_supabase()
    sb.table("trades").delete().eq("id", trade_id).execute()


def load_setting(key: str):
    """設定値を読む。settingsテーブルが未作成なら None を返す"""
    try:
        res = get_supabase().table("settings").select("value").eq("key", key).execute()
        return res.data[0]["value"] if res.data else ""
    except Exception:
        return None


def save_setting(key: str, value: str):
    get_supabase().table("settings").upsert({"key": key, "value": value}).execute()


def load_watchlist():
    """ウォッチリストを読む。テーブルが未作成なら None を返す"""
    try:
        return get_supabase().table("watchlist").select("*").order("id").execute().data
    except Exception:
        return None


def add_watch(code: str, name: str):
    get_supabase().table("watchlist").insert({"code": code, "name": name}).execute()


def remove_watch(watch_id):
    get_supabase().table("watchlist").delete().eq("id", watch_id).execute()


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


def gemini_read_trades(image_bytes: bytes, mime_type: str) -> list:
    """取引画面のスクショをGeminiで読み取り、取引のリストにして返す"""
    from google import genai
    from google.genai import types

    api_key = get_config("GEMINI_API_KEY").strip()
    factories = [
        lambda: genai.Client(api_key=api_key),
        lambda: genai.Client(vertexai=True, api_key=api_key),
    ]
    if api_key.startswith("AQ."):
        factories.reverse()

    prompt = """この画像は株取引のスクリーンショットです。読み取れる取引を抽出して、JSONだけを出力してください。
形式: [{"date": "YYYY-MM-DD"（不明ならnull）, "name": "銘柄名", "code": "4桁の証券コード"（不明ならnull）, "baibai": "買"または"売", "shares": 株数の整数, "price": 1株あたりの約定単価の整数}]
注意: 必ず配列で出力する。JSON以外の文字は一切出力しない。"""
    contents = [types.Content(role="user", parts=[
        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
        types.Part.from_text(text=prompt),
    ])]

    text = None
    last_err = None
    for make_client in factories:
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
                "name": str(d.get("name") or "").strip(),
                "code": str(d.get("code") or "").strip(),
                "baibai": "売" if "売" in str(d.get("baibai", "買")) else "買",
                "shares": int(float(d.get("shares") or 0)),
                "price": int(round(float(d.get("price") or 0))),
            }
        except (TypeError, ValueError):
            continue
        if item["name"] and item["shares"] > 0 and item["price"] > 0:
            result.append(item)
    return result


@st.cache_data(ttl=3600)
def fetch_history(code: str):
    """直近6か月の終値を取得する（チャート表示用）"""
    try:
        hist = yf.Ticker(f"{code}.T").history(period="6mo")
        return hist["Close"] if not hist.empty else None
    except Exception:
        return None


def run_sensei(prompt: str):
    """高坂先生との会話を新しく始めて、最初の回答をもらう"""
    st.session_state.ai_messages = [{"role": "user", "text": prompt, "hidden": True}]
    with st.spinner("高坂先生が考え中です…（30秒ほどかかることがあります）"):
        try:
            answer = gemini_generate(st.session_state.ai_messages)
            st.session_state.ai_messages.append({"role": "model", "text": answer})
        except Exception as e:
            st.session_state.ai_messages = []
            st.error("AIの呼び出しに失敗しました。下のエラー内容を確認してください。")
            st.code(str(e))


def normalize_code(s) -> str:
    """証券コードの表記を整える（全角→半角、空白除去、大文字化）"""
    return unicodedata.normalize("NFKC", str(s or "")).strip().upper()


def gemini_quick(prompt: str) -> str:
    """軽い質問用のGemini呼び出し（速いモデルのみ・検索つき→なしの順）"""
    from google import genai
    from google.genai import types

    api_key = get_config("GEMINI_API_KEY").strip()
    if not api_key:
        return ""
    factories = [
        lambda: genai.Client(api_key=api_key),
        lambda: genai.Client(vertexai=True, api_key=api_key),
    ]
    if api_key.startswith("AQ."):
        factories.reverse()
    search_config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())]
    )
    for make_client in factories:
        try:
            client = make_client()
        except Exception:
            continue
        for model in ["gemini-2.5-flash", "gemini-2.0-flash"]:
            for config in (search_config, None):
                try:
                    resp = client.models.generate_content(model=model, contents=prompt, config=config)
                    if resp.text:
                        return resp.text
                except Exception:
                    continue
    return ""


@st.cache_data(ttl=86400)
def fetch_company_name(code: str) -> str:
    """証券コードから会社名を調べる（見つからなければ空文字）

    1) Yahoo!ファイナンスのデータ → 2) Gemini検索 の順で探す
    """
    try:
        info = yf.Ticker(f"{code}.T").info or {}
        name = info.get("shortName") or info.get("longName") or ""
        if name:
            return name
    except Exception:
        pass
    try:
        text = gemini_quick(
            f"日本の証券コード「{code}」で上場している企業の会社名を、日本語で会社名だけ出力してください。"
            "「株式会社」は省略してよい。確信が持てない場合や存在しない場合は「不明」とだけ出力。"
        )
        name = text.strip().splitlines()[0].strip()
        if name and "不明" not in name and len(name) <= 40:
            return name
    except Exception:
        pass
    return ""


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
    """取引から保有状況と実現損益を計算する。

    証券コードが同じ取引は、銘柄名の表記が違っても同じ会社として合算する。
    （コードがない取引は銘柄名でまとめる）
    """
    # 銘柄名→証券コードの対応表（同じ名前ならコードを共有できるように）
    name_to_code = {}
    for t in trades:
        c = str(t.get("code") or "").strip()
        if c:
            name_to_code[t["name"]] = c

    holdings = {}
    realized = []  # 売却1回ごとの実現損益（分析用）
    for t in trades:
        name = t["name"]
        code = str(t.get("code") or "").strip() or name_to_code.get(name, "")
        key = code or name
        baibai = t["baibai"]
        shares = int(t["shares"])
        price = int(t["price"])

        if key not in holdings:
            holdings[key] = {"name": name, "code": code, "shares": 0, "cost": 0.0, "profit": 0.0}

        h = holdings[key]
        h["name"] = name  # 一番新しい取引の名前を表示に使う
        if code and not h["code"]:
            h["code"] = code
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
                realized.append({"date": str(t["date"]), "name": h["name"], "profit": profit})

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

# ---- パスワード保護 ----
# Secrets に APP_PASSWORD を設定すると有効になる（未設定なら保護なしで動く）
APP_PASSWORD = get_config("APP_PASSWORD")
if APP_PASSWORD and not st.session_state.get("authed"):
    st.title("📈 株取引")
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
        fukumi_total = 0.0
        has_price = False
        for _, h in mochikabu:
            now = fetch_price(h["code"]) if h["code"] else None
            if now is not None:
                fukumi_total += (now - h["cost"] / h["shares"]) * h["shares"]
                has_price = True

        # 今月の損益（月が変わると0円からスタート）
        this_month = date.today().strftime("%Y-%m")
        month_profit = round(sum(r["profit"] for r in realized if str(r["date"])[:7] == this_month))

        c1, c2 = st.columns(2)
        c1.metric(f"今月の損益（{date.today().month}月）", f"{month_profit:+,}円")
        c2.metric("通算の実現損益", f"{total:+,}円")

        # ---- 今月の目標 ----
        goal_raw = load_setting("monthly_goal")
        if goal_raw is not None:
            goal = int(goal_raw) if str(goal_raw).isdigit() else 0
            if goal > 0:
                pct = max(0.0, min(month_profit / goal, 1.0))
                st.progress(
                    pct,
                    text=f"🎯 今月の目標 {goal:,}円 ／ 達成率 {month_profit / goal * 100:.0f}%",
                )
                if month_profit >= goal:
                    if st.session_state.get("celebrated") != this_month:
                        st.balloons()
                        st.session_state.celebrated = this_month
                    st.success(f"🎉 今月の目標を達成しました！（＋{month_profit:,}円 ／ 目標 {goal:,}円）")
            with st.expander("🎯 毎月の目標を設定する"):
                new_goal = st.number_input(
                    "毎月の利益目標（円）", min_value=0, step=1000, value=goal,
                    help="0にすると目標ゲージは非表示になります",
                )
                if st.button("目標を保存", use_container_width=True):
                    try:
                        save_setting("monthly_goal", str(int(new_goal)))
                    except Exception as e:
                        st.error(
                            "保存に失敗しました。settings テーブルが書き込み禁止（RLS有効）に"
                            "なっている可能性があります。SQL Editorで "
                            "`alter table settings disable row level security;` を実行してください。"
                        )
                        st.code(str(e))
                    else:
                        st.rerun()

        c1, c2 = st.columns(2)
        c1.metric(
            "含み損益（保有分）",
            f"{fukumi_total:+,.0f}円" if has_price else "—",
        )
        c2.metric("保有銘柄", f"{len(mochikabu)}銘柄")
        if not has_price and mochikabu:
            st.caption("※ 含み損益は、取引追加で証券コードを入力した銘柄に表示されます")

        if total > 0:
            tax = round(total * TAX_RATE)
            st.caption(f"通算の実現損益から税金（{TAX_RATE * 100:.3f}%）を引いた手取りは ＋{total - tax:,}円")

        divs = load_dividends()
        if divs:
            this_year = date.today().strftime("%Y")
            dy = sum(int(d["amount"]) for d in divs if str(d["date"])[:4] == this_year)
            if dy:
                st.caption(f"💸 今年もらった配当: {dy:,}円")

        # ---- 銘柄別の実現損益 ----
        kakutei = [(n, h) for n, h in holdings.items() if round(h["profit"]) != 0]
        if kakutei:
            st.divider()
            st.markdown("##### 💰 銘柄別の実現損益")
            for _, h in kakutei:
                profit = round(h["profit"])
                st.metric(label=h["name"], value=f"{profit:+,}円")

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
    code = st.text_input(
        "証券コード（任意・例: 7203）",
        help="入力すると会社名が自動で出ます。保有状況タブに現在株価と含み損益も表示されます",
    )
    code = normalize_code(code)  # 全角入力なども自動で半角に直す
    suggested_name = ""
    if code:
        suggested_name = fetch_company_name(code)
        if suggested_name:
            st.success(f"この銘柄: {suggested_name}（コード: {code}）")
        else:
            # 失敗を覚え込まないようにキャッシュを消しておく
            fetch_company_name.clear()
            if fetch_price(code) is not None:
                st.info(f"コード {code} は有効のようです（株価は取得できました）が、"
                        "会社名だけ取得できませんでした。銘柄名は手で入力してください。")
            else:
                st.warning(f"コード {code} の会社が見つかりませんでした。"
                           "コードを確認するか、少し時間をおいて試してください。")

    with st.form("add_trade"):
        trade_date = st.date_input("日付", value=date.today())
        name = st.text_input(
            "銘柄名（例: トヨタ）",
            value=suggested_name,
            help="自動で入った名前は、呼びやすい名前（例: トヨタ）に書き換えてもOKです",
        )
        baibai = st.radio("売買", ["買", "売"], horizontal=True)
        shares = st.number_input("株数", min_value=1, step=1, value=1)
        price = st.number_input("単価（円）", min_value=1, step=1, value=1)
        memo = st.text_input("メモ（任意・例: 決算が良かったので買い）",
                             help="売買の理由を残しておくと、分析や高坂先生の振り返りに活きます")
        submitted = st.form_submit_button("記録する", use_container_width=True)

    if submitted:
        if not name.strip():
            st.error("銘柄名を入力してください。")
        else:
            try:
                save_trade(trade_date.isoformat(), name.strip(), code.strip(), baibai,
                           int(shares), int(price), memo.strip())
                st.success(f"記録しました: {trade_date} / {name} / {baibai} / {int(shares)}株 / {int(price):,}円")
                st.cache_resource.clear()
            except Exception:
                st.error(
                    "保存に失敗しました。証券コードを入力した場合は、"
                    "Supabaseの trades テーブルに `code` 列（text）の追加が必要です。"
                    "PROGRESS.md の手順を確認してください。"
                )

    # ---- スクショから自動入力 ----
    st.divider()
    st.markdown("##### 📷 スクショから自動入力")
    st.caption("証券アプリの約定画面などのスクショを選ぶと、高坂先生が読み取って入力してくれます")
    up = st.file_uploader(
        "画像を選ぶ", type=["png", "jpg", "jpeg", "webp"], label_visibility="collapsed"
    )
    if up is not None and st.button("📷 読み取る", use_container_width=True):
        if not get_config("GEMINI_API_KEY"):
            st.error("Geminiの設定がまだです（Secrets に GEMINI_API_KEY を追加してください）。")
        else:
            with st.spinner("高坂先生が読み取り中です…"):
                try:
                    found = gemini_read_trades(up.getvalue(), up.type or "image/png")
                    if found:
                        st.session_state.scan_trades = found
                    else:
                        st.session_state.scan_trades = None
                        st.warning("取引を読み取れませんでした。金額や銘柄名が写ったスクショで試してください。")
                except Exception as e:
                    st.error("読み取りに失敗しました。")
                    st.code(str(e))

    if st.session_state.get("scan_trades"):
        st.markdown("**読み取り結果（内容を確認してから記録してください）:**")
        st.dataframe(
            [{"日付": s["date"], "銘柄": s["name"], "コード": s["code"] or "—",
              "売買": s["baibai"], "株数": s["shares"], "単価（円）": f'{s["price"]:,}'}
             for s in st.session_state.scan_trades],
            use_container_width=True, hide_index=True,
        )
        scan_memo = st.text_input(
            "メモ（任意・読み取った取引ぜんぶに付きます）",
            key="scan_memo",
            placeholder="例: 決算が良かったので買い",
        )
        c1, c2 = st.columns(2)
        if c1.button("✅ この内容で記録する", use_container_width=True, type="primary"):
            for s in st.session_state.scan_trades:
                try:
                    d = date.fromisoformat(s["date"]).isoformat()
                except ValueError:
                    d = date.today().isoformat()
                save_trade(d, s["name"], s["code"], s["baibai"], s["shares"], s["price"],
                           scan_memo.strip())
            saved_count = len(st.session_state.scan_trades)
            st.session_state.scan_trades = None
            st.toast(f"{saved_count}件 記録しました 📷")
            st.rerun()
        if c2.button("❌ やり直す", use_container_width=True):
            st.session_state.scan_trades = None
            st.rerun()

    # ---- 配当金の記録 ----
    st.divider()
    with st.expander("💸 配当金を記録する"):
        divs = load_dividends()
        if divs is None:
            st.caption("この機能を使うには、Supabaseで dividends テーブルの作成が必要です（PROGRESS.md 参照）")
        else:
            with st.form("add_dividend"):
                d_date = st.date_input("受け取った日", value=date.today())
                d_name = st.text_input("銘柄名（例: トヨタ）")
                d_amount = st.number_input("受け取った金額（円）", min_value=1, step=100, value=1000)
                d_sub = st.form_submit_button("💸 配当を記録する", use_container_width=True)
            if d_sub:
                if not d_name.strip():
                    st.error("銘柄名を入力してください。")
                else:
                    save_dividend(d_date.isoformat(), d_name.strip(), int(d_amount))
                    st.toast("配当を記録しました 💸")
                    st.rerun()
            if divs:
                st.markdown("**最近の配当:**")
                for d in list(reversed(divs))[:5]:
                    c1, c2 = st.columns([5, 1])
                    c1.markdown(f'{d["date"]}　{d["name"]}　**{int(d["amount"]):,}円**')
                    if c2.button("🗑", key=f'rmd_{d["id"]}'):
                        delete_dividend(d["id"])
                        st.rerun()

# ---- タブ2: 取引履歴 ----
with tab2:
    st.subheader("取引履歴")
    trades = load_trades()
    if not trades:
        st.info("まだ取引がありません。")
    else:
        rows = [
            {"日付": t["date"], "銘柄": t["name"], "売買": t["baibai"],
             "株数": t["shares"], "単価（円）": f'{t["price"]:,}',
             "メモ": t.get("memo") or ""}
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
            e_memo = st.text_input("メモ（任意）", value=t.get("memo") or "")
            delete_check = st.checkbox("🗑 この取引を削除する（削除のときだけチェック）")
            c1, c2 = st.columns(2)
            save_btn = c1.form_submit_button("✏️ 修正を保存", use_container_width=True)
            delete_btn = c2.form_submit_button("🗑 削除する", use_container_width=True)

        if save_btn:
            if not e_name.strip():
                st.error("銘柄名を入力してください。")
            else:
                saved_ok = False
                try:
                    update_trade(t["id"], e_date.isoformat(), e_name.strip(),
                                 normalize_code(e_code), e_baibai, int(e_shares), int(e_price),
                                 e_memo.strip())
                    saved_ok = True
                except Exception:
                    st.error("修正の保存に失敗しました。メモ機能を使うには trades テーブルに "
                             "memo 列（text）の追加が必要です（PROGRESS.md 参照）。")
                if saved_ok:
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
        total_fukumi = 0.0
        has_price = False
        for _, h in mochikabu:
            stock_name = h["name"]
            avg = h["cost"] / h["shares"]
            stock_code = h["code"]
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
                with st.expander(f"📊 {stock_name} のチャート（6か月）"):
                    closes = fetch_history(stock_code)
                    if closes is not None:
                        st.line_chart(closes, use_container_width=True)
                    else:
                        st.caption("チャートを取得できませんでした")
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

    # ---- ウォッチリスト ----
    st.divider()
    st.markdown("##### 👀 ウォッチリスト（気になる銘柄）")
    watch = load_watchlist()
    if watch is None:
        st.caption("この機能を使うには、Supabaseで watchlist テーブルの作成が必要です（PROGRESS.md 参照）")
    else:
        c1, c2 = st.columns([3, 1])
        w_code = c1.text_input("証券コードで追加（例: 9984）", key="watch_code", label_visibility="collapsed", placeholder="証券コードで追加（例: 9984）")
        if c2.button("追加", use_container_width=True):
            wc = normalize_code(w_code)
            if wc:
                w_name = fetch_company_name(wc) or wc
                add_watch(wc, w_name)
                st.rerun()
        for w in watch:
            now = fetch_price(str(w["code"]))
            c1, c2 = st.columns([5, 1])
            c1.metric(
                label=f'{w["name"]}（{w["code"]}）',
                value=f"{now:,.0f}円" if now is not None else "—",
            )
            if c2.button("🗑", key=f'rmw_{w["id"]}'):
                remove_watch(w["id"])
                st.rerun()

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
            month_rows = [
                {"月": m, "損益": f"{round(v):+,}円"}
                for m, v in monthly.sort_index(ascending=False).items()
            ]
            st.dataframe(month_rows, use_container_width=True, hide_index=True)

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

        # ---- 年別集計 ----
        st.divider()
        st.markdown("##### 🧾 年別集計（確定申告の参考）")
        divs = load_dividends() or []
        years = sorted(
            {str(r["date"])[:4] for r in realized} | {str(d["date"])[:4] for d in divs},
            reverse=True,
        )
        if not years:
            st.caption("売却または配当の記録ができると、ここに年ごとのまとめが表示されます。")
        else:
            year_rows = []
            for y in years:
                p = round(sum(r["profit"] for r in realized if str(r["date"])[:4] == y))
                dv = sum(int(d["amount"]) for d in divs if str(d["date"])[:4] == y)
                goukei = p + dv
                tax = round(goukei * TAX_RATE) if goukei > 0 else 0
                year_rows.append({
                    "年": y,
                    "実現損益": f"{p:+,}円",
                    "配当": f"{dv:,}円",
                    "合計": f"{goukei:+,}円",
                    "税金の目安": f"{tax:,}円",
                    "手取りの目安": f"{goukei - tax:+,}円",
                })
            st.dataframe(year_rows, use_container_width=True, hide_index=True)
            st.caption("※ 税金は20.315%で計算した目安です。実際の税額は口座の種類（特定/一般/NISA）や源泉徴収の有無で変わります。")

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
    target_code = normalize_code(manual_code) or (codes.get(pick, "") if pick else "")

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
            target_h = None
            for h in holdings.values():
                if h["name"] == target_name or (target_code and h["code"] == target_code):
                    target_h = h
                    break
            position = "この銘柄の取引記録はありません。"
            if target_h is not None:
                h = target_h
                parts = []
                if h["shares"] > 0:
                    avg = h["cost"] / h["shares"]
                    parts.append(f"現在 {h['shares']}株を保有（平均取得単価 {avg:,.0f}円）")
                if round(h["profit"]) != 0:
                    parts.append(f"これまでの実現損益 {round(h['profit']):+,}円")
                if parts:
                    position = "。".join(parts) + "。"

            memo_lines = [
                f"- {t['date']} {t['baibai']}{t['shares']}株@{int(t['price']):,}円: {t['memo']}"
                for t in trades
                if t.get("memo") and (t["name"] == target_name or (target_code and str(t.get("code") or "") == target_code))
            ][-5:]

            stock_data = get_stock_summary(target_code) if target_code else "（証券コード未登録のため株価データなし）"
            prompt = f"""あなたは「高坂先生」。親しみやすく頼れる株式投資の先生です。投資初心者にもわかる日本語で、以下の会社を分析してください。
可能であればGoogle検索で最新のニュースや決算情報も確認して、分析に反映してください。

会社名: {target_name}（証券コード: {target_code or "不明"}）

【株価・会社データ】
{stock_data}

【この生徒の保有・取引状況】
{position}

【この生徒の売買メモ（この銘柄）】
{chr(10).join(memo_lines) if memo_lines else "（メモなし）"}

以下の構成で、マークダウンで簡潔に書いてください（全体で600字程度）:
1. **どんな会社？** — 事業内容を2〜3行で
2. **業績と株価** — 売上・利益の推移と最近の株価の動き
3. **最新の話題** — 検索で見つかった直近のニュースがあれば
4. **あなたの持ち高との関係** — 保有がある場合、平均取得単価と現在の状況を踏まえて
5. **注目ポイント** — 良い材料とリスクを1つずつ

最後に「※これは参考情報です。投資の判断はご自身で行ってください。」と添えてください。
このあと生徒から追加の質問が来たら、高坂先生として同じ調子で会話を続けてください。"""

            run_sensei(prompt)

    # ---- 今日の市況 ----
    if st.button("📰 今日の市況を聞く", use_container_width=True):
        if not get_config("GEMINI_API_KEY"):
            st.error("Geminiの設定がまだです（Secrets に GEMINI_API_KEY を追加してください）。")
        else:
            holdings, _ = calc_profit(trades)
            meigara_lines = []
            for h in holdings.values():
                if h["shares"] > 0:
                    now = fetch_price(h["code"]) if h["code"] else None
                    avg = h["cost"] / h["shares"]
                    line = f"- 保有: {h['name']}（{h['code'] or 'コード不明'}）{h['shares']}株、平均取得単価 {avg:,.0f}円"
                    if now is not None:
                        line += f"、現在値 {now:,.0f}円"
                    meigara_lines.append(line)
            for w in (load_watchlist() or []):
                noww = fetch_price(str(w["code"]))
                line = f"- ウォッチ中: {w['name']}（{w['code']}）"
                if noww is not None:
                    line += f"現在値 {noww:,.0f}円"
                meigara_lines.append(line)

            if not meigara_lines:
                st.error("保有株かウォッチリストの銘柄がまだありません。")
            else:
                prompt = f"""あなたは「高坂先生」。親しみやすく頼れる株式投資の先生です。今日は {date.today().isoformat()} です。
Google検索で今日の日本株市場の状況と、以下の銘柄それぞれの最新ニュース・値動きを調べて、生徒向けの「今日の市況ブリーフィング」を日本語で書いてください。

【生徒の銘柄】
{chr(10).join(meigara_lines)}

構成（マークダウンで、全体700字程度）:
1. **今日の市場ぜんたい** — 日経平均などの大きな流れを2〜3行で
2. **あなたの銘柄の今日** — 銘柄ごとに1〜2行ずつ、値動きと関連ニュース
3. **今日のひとこと** — 高坂先生からのアドバイスを1つ

最後に「※これは参考情報です。投資の判断はご自身で行ってください。」と添えてください。
このあと生徒から追加の質問が来たら、高坂先生として同じ調子で会話を続けてください。"""
                run_sensei(prompt)

    # ---- ポートフォリオ診断 ----
    if st.button("💼 保有株ぜんぶを診断してもらう", use_container_width=True):
        if not get_config("GEMINI_API_KEY"):
            st.error("Geminiの設定がまだです（Secrets に GEMINI_API_KEY を追加してください）。")
        elif not trades:
            st.error("まだ取引がありません。")
        else:
            holdings, realized = calc_profit(trades)
            port_lines = []
            for h in holdings.values():
                if h["shares"] > 0:
                    avg = h["cost"] / h["shares"]
                    c = h["code"]
                    line = f"- {h['name']}（{c or 'コード不明'}）: {h['shares']}株、平均取得単価 {avg:,.0f}円"
                    now = fetch_price(c) if c else None
                    if now is not None:
                        line += f"、現在値 {now:,.0f}円（含み損益 {(now - avg) * h['shares']:+,.0f}円）"
                    port_lines.append(line)
            total_profit = round(sum(h["profit"] for h in holdings.values()))
            win = sum(1 for r in realized if r["profit"] > 0)
            seiseki = f"通算実現損益 {total_profit:+,}円、売却 {len(realized)}回（うち勝ち {win}回）"
            memo_lines = [
                f"- {t['date']} {t['name']} {t['baibai']}{t['shares']}株: {t['memo']}"
                for t in trades if t.get("memo")
            ][-10:]

            prompt = f"""あなたは「高坂先生」。親しみやすく頼れる株式投資の先生です。投資初心者にもわかる日本語で、この生徒のポートフォリオ（保有株全体）を診断してください。
可能であればGoogle検索で各社の最新状況も確認して反映してください。

【保有株】
{chr(10).join(port_lines) if port_lines else "現在保有なし"}

【これまでの成績】
{seiseki}

【売買メモ（最近のもの）】
{chr(10).join(memo_lines) if memo_lines else "（メモなし）"}
※メモがある場合は、売買の判断のクセ（良い習慣・危ない習慣）にも触れてください。

以下の構成で、マークダウンで簡潔に（全体で600字程度）:
1. **全体のバランス** — 業種の偏り・集中度など
2. **良いところ** — ほめられる点
3. **気をつけたいところ** — リスクや改善点
4. **高坂先生からのひとこと** — 次の一手のヒント

最後に「※これは参考情報です。投資の判断はご自身で行ってください。」と添えてください。
このあと生徒から追加の質問が来たら、高坂先生として同じ調子で会話を続けてください。"""
            run_sensei(prompt)

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
