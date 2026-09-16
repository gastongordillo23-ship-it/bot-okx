import os
import time
from threading import Thread
import ccxt
import pandas as pd
import numpy as np
from flask import Flask, jsonify

app = Flask(__name__)

# Configuración de parámetros
SYMBOL = 'BTC/USDT'
TIMEFRAME = '15m'
AMOUNT_USDT = 17
CHECK_INTERVAL = 60

STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.04

ADX_LENGTH = 11
ADX_THRESHOLD = 23

RSI_LENGTH = 14
RSI_MIN_BUY = 50
RSI_MAX_BUY = 68

entry_price = None

def calculate_adx_and_rsi(df, adx_len=11, rsi_len=14):
    """Calcula ADX y RSI de forma nativa controlando divisiones por cero"""
    # 1. RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=rsi_len).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_len).mean()
    
    # Evitar división por cero en RS
    loss_safe = np.where(loss == 0, 1e-9, loss)
    rs = gain / loss_safe
    df['rsi'] = 100 - (100 / (1 + rs))

    # 2. ADX
    df['tr0'] = abs(df['high'] - df['low'])
    df['tr1'] = abs(df['high'] - df['close'].shift(1))
    df['tr2'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)

    df['up_move'] = df['high'] - df['high'].shift(1)
    df['down_move'] = df['low'].shift(1) - df['low']

    df['plus_dm'] = np.where((df['up_move'] > df['down_move']) & (df['up_move'] > 0), df['up_move'], 0)
    df['minus_dm'] = np.where((df['down_move'] > df['up_move']) & (df['down_move'] > 0), df['down_move'], 0)

    tr_s = df['tr'].ewm(alpha=1/adx_len, adjust=False).mean()
    tr_s_safe = np.where(tr_s == 0, 1e-9, tr_s)

    plus_di = 100 * (df['plus_dm'].ewm(alpha=1/adx_len, adjust=False).mean() / tr_s_safe)
    minus_di = 100 * (df['minus_dm'].ewm(alpha=1/adx_len, adjust=False).mean() / tr_s_safe)

    di_sum = plus_di + minus_di
    di_sum_safe = np.where(di_sum == 0, 1e-9, di_sum)

    dx = 100 * (abs(plus_di - minus_di) / di_sum_safe)
    df['adx'] = dx.ewm(alpha=1/adx_len, adjust=False).mean()
    
    return df

def get_exchange():
    return ccxt.okx({
        'apiKey': os.environ.get("OKX_API_KEY"),
        'secret': os.environ.get("OKX_SECRET_KEY"),
        'password': os.environ.get("OKX_PASSPHRASE"),
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

def fetch_last_buy_price(exchange):
    """Recupera el precio de la última compra si el servidor se reinició"""
    try:
        trades = exchange.fetch_my_trades(SYMBOL, limit=5)
        for trade in reversed(trades):
            if trade['side'] == 'buy':
                return float(trade['price'])
    except Exception as e:
        print(f"[WARN] No se pudo recuperar precio de entrada: {e}", flush=True)
    return None

def check_strategy_and_trade():
    global entry_price
    
    try:
        exchange = get_exchange()
        exchange.load_markets()

        bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=250)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        
        df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
        df['sma200'] = df['close'].rolling(window=200).mean()
        df = calculate_adx_and_rsi(df, adx_len=ADX_LENGTH, rsi_len=RSI_LENGTH)

        current_price = float(df['close'].iloc[-1])
        last_close = float(df['close'].iloc[-2])

        prev_ema9 = float(df['ema9'].iloc[-3])
        last_ema9 = float(df['ema9'].iloc[-2])

        prev_ema21 = float(df['ema21'].iloc[-3])
        last_ema21 = float(df['ema21'].iloc[-2])

        last_sma200 = float(df['sma200'].iloc[-2])
        last_adx = float(df['adx'].iloc[-2])
        last_rsi = float(df['rsi'].iloc[-2])

        crossover = (prev_ema9 <= prev_ema21) and (last_ema9 > last_ema21)
        crossunder = (prev_ema9 >= prev_ema21) and (last_ema9 < last_ema21)
        
        trend_filter = last_close > last_sma200
        adx_filter = last_adx >= ADX_THRESHOLD
        rsi_filter = (RSI_MIN_BUY <= last_rsi <= RSI_MAX_BUY)

        balance = exchange.fetch_balance()
        base_coin = SYMBOL.split('/')[0]
        coin_balance = float(balance['free'].get(base_coin, 0.0))
        usdt_balance = float(balance['free'].get('USDT', 0.0))

        min_amount = float(exchange.market(SYMBOL)['limits']['amount']['min'])
        has_position = coin_balance >= min_amount

        # Recuperar entry_price si hubo un restart
        if has_position and entry_price is None:
            entry_price = fetch_last_buy_price(exchange)
            print(f"[RECOVERY] Posición detectada. Precio de compra recuperado: ${entry_price}", flush=True)

        print(f"[CHECK] BTC: ${current_price:,.2f} | ADX: {last_adx:.1f} | RSI: {last_rsi:.1f} | "
              f"Macro: {trend_filter} | ADX OK: {adx_filter} | RSI OK: {rsi_filter}", flush=True)

        # GESTIÓN DE POSICIÓN ABIERTA (SL / TP)
        if has_position and entry_price is not None:
            price_change = (current_price - entry_price) / entry_price

            if price_change <= -STOP_LOSS_PCT or price_change >= TAKE_PROFIT_PCT:
                action = "STOP LOSS" if price_change <= -STOP_LOSS_PCT else "TAKE PROFIT"
                sell_amount = exchange.amount_to_precision(SYMBOL, coin_balance)
                print(f"[{action}] Cambio: {price_change*100:.2f}%. Vendiendo {sell_amount} {base_coin}...", flush=True)
                order = exchange.create_market_sell_order(SYMBOL, sell_amount)
                print(f"Orden ejecutada: {order['id']}", flush=True)
                entry_price = None
                return

        # SEÑAL DE COMPRA FILTRADA
        if crossover and trend_filter and adx_filter and rsi_filter and not has_position:
            if usdt_balance >= AMOUNT_USDT:
                print(f"[SEÑAL COMPRA] Validada. Comprando...", flush=True)
                raw_amount = AMOUNT_USDT / current_price
                target_amount = exchange.amount_to_precision(SYMBOL, raw_amount)

                order = exchange.create_market_buy_order(SYMBOL, target_amount)
                # Tomar el precio real ejecutado si está disponible en la respuesta del exchange
                entry_price = float(order.get('price', current_price)) or current_price
                print(f"Orden de compra ejecutada: {order['id']} a ${entry_price}", flush=True)

        # SEÑAL DE VENTA POR CRUCE BAJISTA
        elif crossunder and has_position:
            sell_amount = exchange.amount_to_precision(SYMBOL, coin_balance)
            print(f"[SEÑAL VENTA] Cruce bajista en {SYMBOL}. Vendiendo...", flush=True)
            order = exchange.create_market_sell_order(SYMBOL, sell_amount)
            print(f"Orden de venta ejecutada: {order['id']}", flush=True)
            entry_price = None

    except Exception as e:
        print(f"Error al verificar la estrategia: {str(e)}", flush=True)

def bot_loop():
    time.sleep(5)
    while True:
        try:
            check_strategy_and_trade()
        except Exception as e:
            print(f"Error en bot_loop: {e}", flush=True)
        time.sleep(CHECK_INTERVAL)

Thread(target=bot_loop, daemon=True).start()

@app.route('/')
def health_check():
    return jsonify({"status": "running", "bot": "OKX Spot Bot con Filtros ADX/RSI (15m)"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
