import os
import time
from threading import Thread
import ccxt
import pandas as pd
import pandas_ta as ta
from flask import Flask, jsonify

app = Flask(__name__)

# Configuración de parámetros de la estrategia
SYMBOL = 'SOL/USDT'      # Par a operar (o 'DOGE/USDT')
TIMEFRAME = '15m'        # Temporalidad del gráfico
AMOUNT_USDT = 5.0        # Capital por operación en USDT
CHECK_INTERVAL = 60      # Revisar el mercado cada 60 segundos

# Inicializar cliente de OKX con tus variables de entorno
exchange = ccxt.okx({
    'apiKey': os.environ.get("OKX_API_KEY"),
    'secret': os.environ.get("OKX_SECRET_KEY"),
    'password': os.environ.get("OKX_PASSPHRASE"),
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

def check_strategy_and_trade():
    """Calcula las EMAs y SMA 200 y ejecuta las órdenes en OKX Spot"""
    try:
        # 1. Obtener las últimas 250 velas de OKX
        bars = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=250)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        
        # 2. Calcular los indicadores
        df['ema9'] = ta.ema(df['close'], length=9)
        df['ema21'] = ta.ema(df['close'], length=21)
        df['sma200'] = ta.sma(df['close'], length=200)

        # Usamos la penúltima vela (índice -2) porque la última (índice -1) aún no ha cerrado
        prev_close = df['close'].iloc[-3]
        last_close = df['close'].iloc[-2]

        prev_ema9 = df['ema9'].iloc[-3]
        last_ema9 = df['ema9'].iloc[-2]

        prev_ema21 = df['ema21'].iloc[-3]
        last_ema21 = df['ema21'].iloc[-2]

        last_sma200 = df['sma200'].iloc[-2]

        # 3. Detectar cruces de medias
        crossover = (prev_ema9 <= prev_ema21) and (last_ema9 > last_ema21)   # Cruce alcista
        crossunder = (prev_ema9 >= prev_ema21) and (last_ema9 < last_ema21)  # Cruce bajista
        trend_filter = last_close > last_sma200                             # Filtro SMA 200

        # 4. Consultar saldo en la cuenta Spot
        balance = exchange.fetch_balance()
        base_coin = SYMBOL.split('/')[0]  # Ej: 'SOL'
        coin_balance = balance['free'].get(base_coin, 0.0)
        usdt_balance = balance['free'].get('USDT', 0.0)

        # --- SEÑAL DE COMPRA ---
        if crossover and trend_filter and coin_balance < (AMOUNT_USDT / last_close) * 0.5:
            if usdt_balance >= AMOUNT_USDT:
                print(f"[SEÑAL COMPRA] Cruce alcista detectado en {SYMBOL}. Comprando {AMOUNT_USDT} USDT...")
                order = exchange.create_market_buy_order_requires_price(SYMBOL, AMOUNT_USDT)
                print("Orden ejecutada con éxito:", order['id'])

        # --- SEÑAL DE VENTA / CIERRE ---
        elif crossunder and coin_balance > 0.001:
            print(f"[SEÑAL VENTA] Cruce bajista detectado en {SYMBOL}. Vendiendo {coin_balance} {base_coin}...")
            order = exchange.create_market_sell_order(SYMBOL, coin_balance)
            print("Posición cerrada con éxito:", order['id'])

    except Exception as e:
        print("Error al verificar la estrategia:", str(e))

def bot_loop():
    """Bucle principal que corre en segundo plano en Render"""
    while True:
        check_strategy_and_trade()
        time.sleep(CHECK_INTERVAL)

# Iniciar el bot en un hilo de ejecución independiente al arrancar el servidor
bot_thread = Thread(target=bot_loop, daemon=True)
bot_thread.start()

@app.route('/')
def health_check():
    return jsonify({"status": "running", "bot": "EMAs 9/21 + SMA 200 OKX Bot"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
