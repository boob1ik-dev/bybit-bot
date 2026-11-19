"""
Bybit spot bot: правило 200 дней.

Раз в сутки (после закрытия дневной свечи по UTC):
  - цена закрытия вчерашнего дня ВЫШЕ средней за 200 дней  -> держим монету (покупаем на все USDT)
  - НИЖЕ                                                  -> держим USDT (продаём всю монету)

Сигнал всегда считается по настоящим ценам Bybit (mainnet), даже если торговля идёт на testnet.
Без внешних библиотек: только стандартный Python 3.9+.

Переменные окружения:
  BYBIT_API_KEY, BYBIT_API_SECRET   ключ субаккаунта (только торговля, без вывода)
  ACCOUNT        "demo" (по умолчанию) = демо-счёт Bybit с настоящими ценами,
                 "testnet" = тестовая сеть, "live" = реальный счёт
  DRY_RUN        "1" (по умолчанию) = только посчитать и написать, без ордеров; "0" = торговать
  SYMBOLS        пары через запятую, например BTCUSDT,ETHUSDT (по умолчанию BTCUSDT).
                 Деньги делятся поровну: каждой паре своя доля счёта.
  SMA_DAYS       по умолчанию 200
  TG_TOKEN, TG_CHAT_ID   необязательно: уведомления в Telegram
"""
import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN

MAINNET = "https://api.bybit.com"
TESTNET = "https://api-testnet.bybit.com"
DEMO = "https://api-demo.bybit.com"


def env(name, default=None):
    v = os.environ.get(name)
    return v.strip() if v and v.strip() else default


SYMBOLS = [x.strip().upper() for x in env("SYMBOLS", env("SYMBOL", "BTCUSDT")).split(",") if x.strip()]
SYMBOL = ",".join(SYMBOLS)
SMA_DAYS = int(env("SMA_DAYS", "200"))
ACCOUNT = env("ACCOUNT", "demo").lower()
if ACCOUNT not in ("demo", "testnet", "live"):
    raise SystemExit("ACCOUNT должен быть demo, testnet или live")
DRY_RUN = env("DRY_RUN", "1") != "0"
API_KEY = env("BYBIT_API_KEY")
API_SECRET = env("BYBIT_API_SECRET")
TG_TOKEN = env("TG_TOKEN")
TG_CHAT_ID = env("TG_CHAT_ID")
TRADE_BASE = {"demo": DEMO, "testnet": TESTNET, "live": MAINNET}[ACCOUNT]
RECV_WINDOW = "10000"


# ---------- HTTP ----------
def http(method, url, body=None, headers=None, retries=3):
    data = body.encode() if body is not None else None
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # сеть / 5xx — пробуем ещё раз
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Сеть: {method} {url} не ответил: {last}")


def public_get(path, params):
    url = MAINNET + path + "?" + urllib.parse.urlencode(params)
    j = http("GET", url)
    if j.get("retCode") != 0:
        raise RuntimeError(f"Bybit {path}: {j.get('retMsg')}")
    return j["result"]


def _sign(payload, ts):
    msg = ts + API_KEY + RECV_WINDOW + payload
    return hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()


def private(method, path, params=None):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("Не заданы BYBIT_API_KEY / BYBIT_API_SECRET")
    ts = str(int(time.time() * 1000))
    if method == "GET":
        qs = urllib.parse.urlencode(params or {})
        url = TRADE_BASE + path + ("?" + qs if qs else "")
        payload, body = qs, None
    else:
        body = json.dumps(params or {}, separators=(",", ":"))
        url = TRADE_BASE + path
        payload = body
    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": _sign(payload, ts),
        "Content-Type": "application/json",
    }
    j = http(method, url, body, headers)
    if j.get("retCode") != 0:
        raise RuntimeError(f"Bybit {path}: {j.get('retCode')} {j.get('retMsg')}")
    return j["result"]


# ---------- Telegram ----------
def notify(text):
    print(text, flush=True)
    if not (TG_TOKEN and TG_CHAT_ID):
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": TG_CHAT_ID, "text": text}).encode()
        urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=15)
    except Exception as e:
        print(f"Telegram не отправился: {e}", flush=True)


# ---------- Логика ----------
def daily_signal(symbol, now_ms=None):
    """(выше_средней, цена_закрытия, средняя, дата) по ЗАКРЫТЫМ дневным свечам."""
    now_ms = now_ms or int(time.time() * 1000)
    today_start = now_ms - now_ms % 86_400_000
    rows = public_get("/v5/market/kline",
                      {"category": "spot", "symbol": symbol, "interval": "D", "limit": SMA_DAYS + 20})["list"]
    closed = sorted((r for r in rows if int(r[0]) < today_start), key=lambda r: int(r[0]))
    if len(closed) < SMA_DAYS:
        raise RuntimeError(f"{symbol}: мало истории — {len(closed)} дней из {SMA_DAYS}")
    closes = [float(r[4]) for r in closed[-SMA_DAYS:]]
    sma = sum(closes) / SMA_DAYS
    last = closes[-1]
    day = time.strftime("%d.%m.%Y", time.gmtime(int(closed[-1][0]) / 1000))
    return last > sma, last, sma, day


def instrument(symbol):
    info = public_get("/v5/market/instruments-info", {"category": "spot", "symbol": symbol})["list"][0]
    lot = info["lotSizeFilter"]
    return {
        "base": info["baseCoin"],
        "quote": info["quoteCoin"],
        "base_step": Decimal(lot["basePrecision"]),
        "quote_step": max(Decimal(lot.get("quotePrecision", "0.01")), Decimal("0.01")),
        "min_qty": Decimal(lot.get("minOrderQty", "0")),
        "min_amt": Decimal(lot.get("minOrderAmt", "5")),
    }


def balances(coins):
    res = private("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": ",".join(coins)})
    out = {c: Decimal("0") for c in coins}
    for acct in res.get("list", []):
        for c in acct.get("coin", []):
            if c["coin"] in out:
                free = Decimal(c.get("walletBalance") or "0") - Decimal(c.get("locked") or "0")
                out[c["coin"]] = max(free, Decimal("0"))
    return out


def floor_to(x, step):
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def market_order(symbol, side, qty, unit):
    params = {"category": "spot", "symbol": symbol, "side": side, "orderType": "Market",
              "qty": format(qty.normalize(), "f"), "marketUnit": unit}
    return private("POST", "/v5/order/create", params)


def fmt_bal(bal, coins, quote):
    return ", ".join(f"{bal[c].normalize()} {c}" for c in coins if c != quote) + f", {bal[quote]:,.2f} {quote}"


def main():
    mode = {"demo": "ДЕМО-СЧЁТ", "testnet": "TESTNET", "live": "РЕАЛЬНЫЙ СЧЁТ"}[ACCOUNT] + \
           (" · пробный запуск, без ордеров" if DRY_RUN else "")
    pairs = []
    for sym in SYMBOLS:
        up, close, sma, day = daily_signal(sym)
        pairs.append({"sym": sym, "up": up, "close": close, "sma": sma, "day": day, **instrument(sym)})
    quote = pairs[0]["quote"]
    if any(p["quote"] != quote for p in pairs):
        raise RuntimeError("Все пары должны торговаться к одной валюте (например, USDT)")
    coins = [p["base"] for p in pairs] + [quote]
    bal = balances(coins)

    # Деньги делятся поровну между парами: каждой паре своя доля от всего счёта
    equity = bal[quote] + sum(bal[p["base"]] * Decimal(str(p["close"])) for p in pairs)
    slot = equity / len(pairs)

    lines = [f"Бот 200 дней · {mode}", f"Счёт ≈ {equity:,.2f} {quote}, на пару ≈ {slot:,.2f} {quote}"]
    acted = False

    # Сначала продажи (освобождают доллары), потом покупки
    for p in sorted(pairs, key=lambda p: p["up"]):
        base, price = p["base"], Decimal(str(p["close"]))
        held_val = bal[base] * price
        sig = f"выше средней ({p['sma']:,.2f}) → держим {base}" if p["up"] else \
              f"ниже средней ({p['sma']:,.2f}) → держим {quote}"
        line = f"\n{p['sym']}: закрытие {p['day']} {p['close']:,.2f}, {sig}.\n  В монете ≈ {held_val:,.2f} {quote}. "
        action = "Действий нет."
        if not p["up"] and held_val >= p["min_amt"]:
            qty = floor_to(bal[base], p["base_step"])
            if qty >= p["min_qty"] and qty > 0:
                if DRY_RUN:
                    action = f"[пробно] Продал бы {qty.normalize()} {base}."
                    bal[quote] += qty * price
                else:
                    r = market_order(p["sym"], "Sell", qty, "baseCoin")
                    action = f"Продано {qty.normalize()} {base}. Ордер {r.get('orderId')}."
                    acted = True
                    time.sleep(2)
                    bal = balances(coins)
        elif p["up"] and held_val < slot * Decimal("0.5"):
            want = slot - held_val
            amt = floor_to(min(bal[quote] * Decimal("0.998"), want), p["quote_step"])
            if amt >= p["min_amt"]:
                if DRY_RUN:
                    action = f"[пробно] Купил бы {base} на {amt} {quote}."
                    bal[quote] -= amt
                else:
                    r = market_order(p["sym"], "Buy", amt, "quoteCoin")
                    action = f"Куплен {base} на {amt} {quote}. Ордер {r.get('orderId')}."
                    acted = True
                    time.sleep(2)
                    bal = balances(coins)
        lines.append(line + action)

    if acted:
        lines.append("\nНовый баланс: " + fmt_bal(balances(coins), coins, quote))
    notify("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify(f"⚠️ Бот {SYMBOL}: ошибка — {e}")
        sys.exit(1)
