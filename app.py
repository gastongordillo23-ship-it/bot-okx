import os
import time
from threading import Thread
import ccxt
import pandas as pd
import numpy as np
from flask import Flask, jsonify

app = Flask(__name__)

# ==========================================
# CONFIGURACIÓN GENERAL Y GESTIÓN DE RIESGO
# ==========================================
SYMBOL = 'BTC/USDT'
TIMEFRAME = '1h'
CHECK_INTERVAL = 60            # Frecuencia de chequeo en segundos

# Parámetros de Riesgo Adaptativo
RISK_PER_TRADE_USDT = 2.0      # Pérdida máxima en USDT que permitís por trade si toca el SL
MIN_ORDER_USDT = 10.0          # Límite mínimo de orden exigido por OKX (Spot)
MAX_ORDER_USDT = 50.0          # Techo máximo de capital a invertir por entrada

# Parámetros ATR
ATR_LENGTH = 14
SL_ATR_MULT = 1.8              # Stop Loss Inicial = 1.8 * ATR
TS_ATR_MULT = 2.2              # Trailing Stop = 2.2 * ATR

ADX_LENGTH = 11
ADX_THRESHOLD = 23

RSI_LENGTH = 14
RSI_MIN_BUY = 50
RSI_MAX_BUY = 78

# Variables de estado
entry_price = None
highest_price = None
dynamic_sl_pct = 0.025
dynamic_ts_pct = 0.030

def calculate_indicators(df, adx_len=11, rsi_len=14, atr_len=14):
    """Calcula RSI, ADX y ATR Dinámico sobre el DataFrame."""
    # 1. RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=rsi_len).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=rsi_len).mean()
    loss_safe = np.where(loss == 0, 1e-9, loss)
    rs = gain / loss_safe
    df['rsi'] = 100 - (100 / (1 + rs))

    # 2. True Range & ATR
    df['tr0'] = abs(df['high'] - df['low'])
    df['tr1'] = abs(df['high'] - df['close'].shift(1))
    df['tr2'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = df[['tr0', 'tr1', 'tr2']].max(axis=1)
    df['atr'] = df['tr'].ewm(alpha=1/atr_len, adjust=False).mean()

    # 3. ADX
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
    """Recupera el precio de la última compra si el servidor se reinició."""
    try:
        trades = exchange.fetch_my_trades(SYMBOL, limit=5)
        for trade in reversed(trades):
            if trade['side'] == 'buy':
                return float(trade['price'])
    except Exception as e:
        print(f"[WARN] No se pudo recuperar precio de entrada: {e}", flush=True)
    return None

def calculate_position_size(current_price, last_atr):
    """Calcula el tamaño de posición en USDT basado en el riesgo por ATR."""
    sl_distance_usdt = last_atr * SL_ATR_MULT
    sl_pct = sl_distance_usdt / current_price

    # Monto necesario en USDT para arriesgar exactamente RISK_PER_TRADE_USDT
    target_usdt = RISK_PER_TRADE_USDT / sl_pct

    # Acotar entre los límites permitidos de OKX y de seguridad
    target_usdt = max(MIN_ORDER_USDT, min(MAX_ORDER_USDT, target_usdt))
    return target_usdt, sl_pct

def check_strategy_and_trade():
    global entry_price, highest_price, dynamic_sl_pct, dynamic_ts_pct
    
    try:
        exchange = get_exchange()
        exchange.load_markets()

        bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=250)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        
        df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
        df['sma200'] = df['close'].rolling(window=200).mean()
        df = calculate_indicators(df, adx_len=ADX_LENGTH, rsi_len=RSI_LENGTH, atr_len=ATR_LENGTH)

        current_price = float(df['close'].iloc[-1])
        last_close = float(df['close'].iloc[-2])
        last_atr = float(df['atr'].iloc[-2])

        # Recalibración dinámica del SL y TS en función de la volatilidad actual
        dynamic_sl_pct = (last_atr * SL_ATR_MULT) / current_price
        dynamic_ts_pct = (last_atr * TS_ATR_MULT) / current_price

        # Límites de seguridad para evitar rangos anómalos
        dynamic_sl_pct = max(0.012, min(0.050, dynamic_sl_pct))
        dynamic_ts_pct = max(0.018, min(0.060, dynamic_ts_pct))

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

        # Recuperación de estado en reinicios de servidor
        if has_position and entry_price is None:
            entry_price = fetch_last_buy_price(exchange) or current_price
            highest_price = current_price
            print(f"[RECOVERY] Posición abierta detectada. Entrada: ${entry_price:,.2f}", flush=True)

        print(f"[CHECK] BTC: ${current_price:,.2f} | ATR: ${last_atr:.2f} | "
              f"SL Dynamic: {dynamic_sl_pct*100:.2f}% | TS Dynamic: {dynamic_ts_pct*100:.2f}%", flush=True)

        # ==========================================
        # GESTIÓN DE POSICIÓN ABIERTA (SL / TRAILING STOP)
        # ==========================================
        if has_position and entry_price is not None:
            if highest_price is None or current_price > highest_price:
                highest_price = current_price

            change_from_entry = (current_price - entry_price) / entry_price
            drop_from_peak = (highest_price - current_price) / highest_price

            is_stop_loss = change_from_entry <= -dynamic_sl_pct
            is_trailing_stop = (highest_price > entry_price) and (drop_from_peak >= dynamic_ts_pct)

            if is_stop_loss or is_trailing_stop:
                reason = (f"SL DINÁMICO ({dynamic_sl_pct*100:.2f}%)" if is_stop_loss 
                          else f"TRAILING ADAPTATIVO (Caída {drop_from_peak*100:.2f}% desde pico de ${highest_price:,.2f})")
                
                sell_amount = exchange.amount_to_precision(SYMBOL, coin_balance)
                print(f"[{reason}] Vendiendo {sell_amount} {base_coin} a ${current_price:,.2f}...", flush=True)
                
                order = exchange.create_market_sell_order(SYMBOL, sell_amount)
                print(f"Orden de venta ejecutada: {order['id']}", flush=True)
                
                entry_price = None
                highest_price = None
                return

        # ==========================================
        # ENTRADA EN COMPRA CON POSITION SIZING DINÁMICO
        # ==========================================
        if crossover and trend_filter and adx_filter and rsi_filter and not has_position:
            target_usdt, sl_pct = calculate_position_size(current_price, last_atr)

            if usdt_balance >= target_usdt:
                raw_btc_amount = target_usdt / current_price
                target_amount = exchange.amount_to_precision(SYMBOL, raw_btc_amount)

                print(f"[SEÑAL COMPRA] Calculado: ${target_usdt:.2f} USDT ({target_amount} BTC) | "
                      f"Riesgo SL ({sl_pct*100:.2f}%): ~${RISK_PER_TRADE_USDT:.2f} USDT", flush=True)

                order = exchange.create_market_buy_order(SYMBOL, target_amount)
                entry_price = float(order.get('price', current_price)) or current_price
                highest_price = entry_price
                print(f"Compra ejecutada con éxito ID: {order['id']} a ${entry_price:,.2f}", flush=True)
            else:
                print(f"[ALERTA COMPRA] Balance insuficiente. Requerido: ${target_usdt:.2f} USDT | Disponible: ${usdt_balance:.2f} USDT", flush=True)

        # ==========================================
        # SALIDA TÉCNICA POR CRUCE BAJISTA
        # ==========================================
        elif crossunder and has_position:
            sell_amount = exchange.amount_to_precision(SYMBOL, coin_balance)
            print(f"[SEÑAL VENTA] Cruce bajista EMA 9/21. Vendiendo {sell_amount} {base_coin}...", flush=True)
            order = exchange.create_market_sell_order(SYMBOL, sell_amount)
            print(f"Orden de venta ejecutada: {order['id']}", flush=True)
            entry_price = None
            highest_price = None

    except Exception as e:
        print(f"Error en chequeo: {str(e)}", flush=True)

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
    return jsonify({
        "status": "running", 
        "bot": "OKX Spot Bot Adaptive (ATR Position Sizing & Trailing Stop)",
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "sl_dinamico_actual": f"{dynamic_sl_pct*100:.2f}%",
        "ts_dinamico_actual": f"{dynamic_ts_pct*100:.2f}%"
    }), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
