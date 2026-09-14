import os
import time
from threading import Thread
import ccxt
import pandas as pd
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

# Variable global para rastrear el precio de entrada
entry_price = None

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

        current_price = float(df['close'].iloc[-1])
        last_close = float(df['close'].iloc[-2])

        prev_ema9 = float(df['ema9'].iloc[-3])
        last_ema9 = float(df['ema9'].iloc[-2])

        prev_ema21 = float(df['ema21'].iloc[-3])
        last_ema21 = float(df['ema21'].iloc[-2])

        last_sma200 = float(df['sma200'].iloc[-2])

        # 3. Detectar cruces
        crossover = (prev_ema9 <= prev_ema21) and (last_ema9 > last_ema21)
        crossunder = (prev_ema9 >= prev_ema21) and (last_ema9 < last_ema21)
        trend_filter = last_close > last_sma200

        # Log informativo en cada ciclo para confirmar funcionamiento en Render
        print(f"[CHECK] BTC: ${current_price:,.2f} | EMA9: ${last_ema9:,.2f} | EMA21: ${last_ema21:,.2f} | SMA200: ${last_sma200:,.2f} | Tendencia OK: {trend_filter}", flush=True)

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

        # --- SEÑAL DE COMPRA ---
        if crossover and trend_filter and not has_position:
            if usdt_balance >= AMOUNT_USDT:
                print(f"[SEÑAL COMPRA] Cruce alcista en {SYMBOL}. Comprando...", flush=True)
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
    return jsonify({"status": "running", "bot": "OKX Spot Bot con SL/TP"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
