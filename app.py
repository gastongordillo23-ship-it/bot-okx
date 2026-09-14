import os
import time
from threading import Thread
import ccxt
import pandas as pd
import numpy as np
from flask import Flask, jsonify

app = Flask(__name__)

# Configuración de parámetros de la estrategia
SYMBOL = 'BTC/USDT'      # Par a operar
TIMEFRAME = '15m'        # Temporalidad del gráfico
AMOUNT_USDT = 17         # Capital por operación en USDT
CHECK_INTERVAL = 60      # Revisar el mercado cada 60 segundos

# Gestión de Riesgo (Porcentajes)
STOP_LOSS_PCT = 0.02     # 2% de pérdida máxima
TAKE_PROFIT_PCT = 0.04   # 4% de ganancia objetivo

# Parámetros de Filtros
ADX_THRESHOLD = 22       # Mínima fuerza de tendencia para operar (Evita rangos)
RSI_MAX_BUY = 65         # No comprar si el RSI supera este nivel (Evita sobrecompra)

entry_price = None

def calculate_adx_and_rsi(df, length=14):
    """Calcula ADX y RSI de forma nativa con pandas/numpy"""
    # 1. RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=length).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=length).mean()
    rs = gain / loss
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

    tr_s = df['tr'].ewm(alpha=1/length, adjust=False).mean()
    plus_di = 100 * (df['plus_dm'].ewm(alpha=1/length, adjust=False).mean() / tr_s)
    minus_di = 100 * (df['minus_dm'].ewm(alpha=1/length, adjust=False).mean() / tr_s)

    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
    df['adx'] = dx.ewm(alpha=1/length, adjust=False).mean()
    
    return df

def get_exchange():
    """Inicializa la conexión con OKX usando variables de entorno"""
    return ccxt.okx({
        'apiKey': os.environ.get("OKX_API_KEY"),
        'secret': os.environ.get("OKX_SECRET_KEY"),
        'password': os.environ.get("OKX_PASSPHRASE"),
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'}
    })

def check_strategy_and_trade():
    """Calcula indicadores, gestiona SL/TP y ejecuta órdenes en OKX Spot"""
    global entry_price
    
    try:
        exchange = get_exchange()
        exchange.load_markets()

        # 1. Obtener las últimas 250 velas de OKX
        bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=250)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        
        # 2. Calcular indicadores
        df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
        df['sma200'] = df['close'].rolling(window=200).mean()
        
        # Agregar ADX y RSI
        df = calculate_adx_and_rsi(df, length=14)

        current_price = float(df['close'].iloc[-1])
        last_close = float(df['close'].iloc[-2])

        prev_ema9 = float(df['ema9'].iloc[-3])
        last_ema9 = float(df['ema9'].iloc[-2])

        prev_ema21 = float(df['ema21'].iloc[-3])
        last_ema21 = float(df['ema21'].iloc[-2])

        last_sma200 = float(df['sma200'].iloc[-2])
        last_adx = float(df['adx'].iloc[-2])
        last_rsi = float(df['rsi'].iloc[-2])

        # 3. Condiciones de Estrategia
        crossover = (prev_ema9 <= prev_ema21) and (last_ema9 > last_ema21)
        crossunder = (prev_ema9 >= prev_ema21) and (last_ema9 < last_ema21)
        
        # Filtros de Calidad
        trend_filter = last_close > last_sma200
        adx_filter = last_adx >= ADX_THRESHOLD
        rsi_filter = last_rsi <= RSI_MAX_BUY

        # Log de Monitoreo
        print(f"[CHECK] BTC: ${current_price:,.2f} | ADX: {last_adx:.1f} | RSI: {last_rsi:.1f} | "
              f"Tendencia: {trend_filter} | ADX OK: {adx_filter} | RSI OK: {rsi_filter}", flush=True)

        # 4. Consultar saldo Spot
        balance = exchange.fetch_balance()
        base_coin = SYMBOL.split('/')[0]
        coin_balance = float(balance['free'].get(base_coin, 0.0))
        usdt_balance = float(balance['free'].get('USDT', 0.0))

        min_amount = float(exchange.market(SYMBOL)['limits']['amount']['min'])
        has_position = coin_balance >= min_amount

        # --- GESTIÓN DE POSICIÓN ABIERTA (SL / TP) ---
        if has_position and entry_price is not None:
            price_change = (current_price - entry_price) / entry_price

            if price_change <= -STOP_LOSS_PCT:
                sell_amount = exchange.amount_to_precision(SYMBOL, coin_balance)
                print(f"[STOP LOSS] Caída del {price_change*100:.2f}%. Vendiendo {sell_amount} {base_coin}...", flush=True)
                order = exchange.create_market_sell_order(SYMBOL, sell_amount)
                print(f"Orden ejecutada: {order['id']}", flush=True)
                entry_price = None
                return

            elif price_change >= TAKE_PROFIT_PCT:
                sell_amount = exchange.amount_to_precision(SYMBOL, coin_balance)
                print(f"[TAKE PROFIT] Subida del {price_change*100:.2f}%. Vendiendo {sell_amount} {base_coin}...", flush=True)
                order = exchange.create_market_sell_order(SYMBOL, sell_amount)
                print(f"Orden ejecutada: {order['id']}", flush=True)
                entry_price = None
                return

        # --- SEÑAL DE COMPRA FILTRADA ---
        # Requiere: Cruce alcista + Precio > SMA200 + Tendencia Fuerte (ADX) + Sin Sobrecompra (RSI)
        if crossover and trend_filter and adx_filter and rsi_filter and not has_position:
            if usdt_balance >= AMOUNT_USDT:
                print(f"[SEÑAL COMPRA] Cruce alcista validado por ADX ({last_adx:.1f}) y RSI ({last_rsi:.1f}). Comprando...", flush=True)
                raw_amount = AMOUNT_USDT / current_price
                target_amount = exchange.amount_to_precision(SYMBOL, raw_amount)

                order = exchange.create_market_buy_order(SYMBOL, target_amount)
                print(f"Orden de compra ejecutada: {order['id']}", flush=True)
                entry_price = current_price

        # --- SEÑAL DE VENTA POR CRUCE BAJISTA ---
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

# Arrancar el hilo secundario
Thread(target=bot_loop, daemon=True).start()

@app.route('/')
def health_check():
    return jsonify({"status": "running", "bot": "OKX Spot Bot con Filtros ADX/RSI"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
