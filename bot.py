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
  SYMBOL         по умолчанию BTCUSDT
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


SYMBOL = env("SYMBOL", "BTCUSDT")
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
def daily_signal(now_ms=None):
    """Возвращает (выше_средней, цена_закрытия, средняя, дата) по ЗАКРЫТЫМ дневным свечам."""
    now_ms = now_ms or int(time.time() * 1000)
    today_start = now_ms - now_ms % 86_400_000
    rows = public_get("/v5/market/kline",
                      {"category": "spot", "symbol": SYMBOL, "interval": "D", "limit": SMA_DAYS + 20})["list"]
    closed = sorted((r for r in rows if int(r[0]) < today_start), key=lambda r: int(r[0]))
    if len(closed) < SMA_DAYS:
        raise RuntimeError(f"Мало истории: {len(closed)} дней из {SMA_DAYS}")
    closes = [float(r[4]) for r in closed[-SMA_DAYS:]]
    sma = sum(closes) / SMA_DAYS
    last = closes[-1]
    day = time.strftime("%d.%m.%Y", time.gmtime(int(closed[-1][0]) / 1000))
    return last > sma, last, sma, day


def instrument():
    info = public_get("/v5/market/instruments-info", {"category": "spot", "symbol": SYMBOL})["list"][0]
    lot = info["lotSizeFilter"]
    return {
        "base": info["baseCoin"],
        "quote": info["quoteCoin"],
        "base_step": Decimal(lot["basePrecision"]),
        "quote_step": max(Decimal(lot.get("quotePrecision", "0.01")), Decimal("0.01")),
        "min_qty": Decimal(lot.get("minOrderQty", "0")),
        "min_amt": Decimal(lot.get("minOrderAmt", "5")),
    }


def balances(base, quote):
    res = private("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED", "coin": f"{base},{quote}"})
    out = {base: Decimal("0"), quote: Decimal("0")}
    for acct in res.get("list", []):
        for c in acct.get("coin", []):
            if c["coin"] in out:
                free = Decimal(c.get("walletBalance") or "0") - Decimal(c.get("locked") or "0")
                out[c["coin"]] = max(free, Decimal("0"))
    return out


def floor_to(x, step):
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def market_order(side, qty, unit):
    params = {"category": "spot", "symbol": SYMBOL, "side": side, "orderType": "Market",
              "qty": format(qty.normalize(), "f"), "marketUnit": unit}
    return private("POST", "/v5/order/create", params)


def main():
    mode = {"demo": "ДЕМО-СЧЁТ", "testnet": "TESTNET", "live": "РЕАЛЬНЫЙ СЧЁТ"}[ACCOUNT] + (" · пробный запуск, без ордеров" if DRY_RUN else "")
    up, close, sma, day = daily_signal()
    inst = instrument()
    base, quote = inst["base"], inst["quote"]
    head = (f"{SYMBOL} {mode}\nЗакрытие {day}: {close:,.2f}\nСредняя за {SMA_DAYS} дней: {sma:,.2f}\n"
            f"Сигнал: {'ВЫШЕ средней → держим ' + base if up else 'НИЖЕ средней → держим ' + quote}")

    bal = balances(base, quote)
    price = Decimal(str(close))
    base_val = bal[base] * price
    head += f"\nБаланс: {bal[base].normalize()} {base} (~{base_val:,.2f} {quote}) и {bal[quote]:,.2f} {quote}"

    action = "Действий нет."
    if up and bal[quote] >= inst["min_amt"]:
        amt = floor_to(bal[quote] * Decimal("0.998"), inst["quote_step"])  # запас на комиссию
        if amt >= inst["min_amt"]:
            if DRY_RUN:
                action = f"[пробно] Купил бы {base} на {amt} {quote}."
            else:
                r = market_order("Buy", amt, "quoteCoin")
                action = f"Куплен {base} на {amt} {quote}. Ордер {r.get('orderId')}."
    elif not up and base_val >= inst["min_amt"]:
        qty = floor_to(bal[base], inst["base_step"])
        if qty >= inst["min_qty"] and qty > 0:
            if DRY_RUN:
                action = f"[пробно] Продал бы {qty.normalize()} {base}."
            else:
                r = market_order("Sell", qty, "baseCoin")
                action = f"Продано {qty.normalize()} {base}. Ордер {r.get('orderId')}."

    if not DRY_RUN and not action.startswith("Действий"):
        time.sleep(3)
        b2 = balances(base, quote)
        action += f"\nНовый баланс: {b2[base].normalize()} {base} и {b2[quote]:,.2f} {quote}"

    notify(head + "\n" + action)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        notify(f"⚠️ Бот {SYMBOL}: ошибка — {e}")
        sys.exit(1)
