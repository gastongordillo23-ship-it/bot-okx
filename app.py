import time
import os
import threading
from flask import Flask
import ccxt
import pandas as pd
import numpy as np

# --- CONFIGURACIÓN DE FLASK (Gunicorn se encarga de servirlo) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot de trading OKX activo y en ejecución.", 200

# --- CONFIGURACIÓN DEL BOT & OKX ---
API_KEY = os.environ.get("OKX_API_KEY")
SECRET = os.environ.get("OKX_SECRET_KEY")
PASSWORD = os.environ.get("OKX_PASSPHRASE")

SYMBOL = "BTC/USDT"
TIMEFRAME = "4h"
RISK_PER_TRADE_USDT = 2.0  # Riesgo máximo por operación en USDT
ATR_STOP_MULTIPLIER = 1.5   # Multiplicador ATR para Stop Loss

exchange = ccxt.okx({
    'apiKey': API_KEY,
    'secret': SECRET,
    'password': PASSWORD,
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'}
})

# Cargar mercados una sola vez al inicializar
try:
    print("Cargando mercados de OKX...", flush=True)
    exchange.load_markets()
except Exception as e:
    print(f"Error al cargar mercados iniciales: {e}", flush=True)

# --- CÁLCULO NATIVO DE INDICADORES (CON PANDAS / NUMPY) ---
def calculate_indicators(df):
    """Calcula indicadores técnicos usando únicamente pandas y numpy nativos."""
    # EMA y SMA
    df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['sma200'] = df['close'].rolling(window=200).mean()

    # RSI (14)
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))

    # ATR (14)
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(window=14).mean()

    # ADX (14)
    up_move = df['high'].diff()
    down_move = -df['low'].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    atr_smooth = tr.rolling(window=14).mean()
    plus_di = 100 * (pd.Series(plus_dm).rolling(window=14).mean() / atr_smooth)
    minus_di = 100 * (pd.Series(minus_dm).rolling(window=14).mean() / atr_smooth)

    dx = 100 * (abs(plus_di - minus_di) / (plus_di + minus_di))
    df['adx'] = dx.rolling(window=14).mean()

    return df

def fetch_data():
    """Obtiene velas de 4H y procesa los indicadores."""
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=300)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

    df = calculate_indicators(df)
    return df

def get_market_limits():
    """Devuelve los límites mínimos desde los mercados previamente cargados."""
    market = exchange.markets.get(SYMBOL, {})
    limits = market.get('limits', {})
    
    min_amount = float(limits.get('amount', {}).get('min', 0.00001))
    min_cost = float(limits.get('cost', {}).get('min', 0.1))
    
    return min_amount, min_cost

def run_strategy():
    print("Iniciando ciclo de estrategia (4H)...", flush=True)
    try:
        df = fetch_data()
        if len(df) < 200:
            print("Datos insuficientes para SMA200. Esperando más velas.", flush=True)
            return

        # 1. VELAS CONFIRMADAS / CERRADAS
        prev_closed = df.iloc[-3]  # Vela cerrada hace 2 períodos
        last_closed = df.iloc[-2]  # Última vela cerrada (confirmada)

        # Filtros de tendencia y momento
        trend_filter = last_closed['close'] > last_closed['sma200']
        strength_filter = last_closed['adx'] >= 20
        rsi_filter = (52 <= last_closed['rsi'] <= 75)
        last_atr = float(last_closed['atr'])

        # Cruces de medias móviles
        ema_crossover = (prev_closed['ema9'] <= prev_closed['ema21']) and (last_closed['ema9'] > last_closed['ema21'])
        ema_crossunder = (prev_closed['ema9'] >= prev_closed['ema21']) and (last_closed['ema9'] < last_closed['ema21'])

        # 2. PRECIO Y SALDOS
        live_price = float(df.iloc[-1]['close'])

        balance = exchange.fetch_balance()
        usdt_balance = float(balance['free'].get('USDT', 0.0))
        btc_balance = float(balance['free'].get('BTC', 0.0))

        # 3. COMPRA SPOT (ENTRADA)
        if ema_crossover and trend_filter and strength_filter and rsi_filter:
            print("[SEÑAL COMPRA] Condición alcista confirmada.", flush=True)

            stop_distance_usdt = last_atr * ATR_STOP_MULTIPLIER
            stop_distance_pct = stop_distance_usdt / live_price

            if stop_distance_pct > 0:
                target_usdt = RISK_PER_TRADE_USDT / stop_distance_pct
            else:
                target_usdt = usdt_balance

            target_usdt = min(target_usdt, usdt_balance)

            min_amount, min_cost = get_market_limits()
            raw_btc_amount = target_usdt / live_price
            formatted_amount = float(exchange.amount_to_precision(SYMBOL, raw_btc_amount))

            if target_usdt >= min_cost and formatted_amount >= min_amount:
                print(f"Ejecutando COMPRA: {formatted_amount} BTC (~${target_usdt:.2f} USDT)", flush=True)
                order = exchange.create_market_buy_order(SYMBOL, formatted_amount)
                print("Orden de compra realizada con éxito. ID:", order['id'], flush=True)

                # Stop Loss automático en OKX
                stop_loss_price = live_price - stop_distance_usdt
                formatted_sl_price = exchange.price_to_precision(SYMBOL, stop_loss_price)

                print(f"Colocando Stop Loss en OKX a ${formatted_sl_price} USDT...", flush=True)
                sl_order = exchange.create_order(
                    symbol=SYMBOL,
                    type='trigger',
                    side='sell',
                    amount=formatted_amount,
                    params={
                        'triggerPrice': formatted_sl_price,
                        'orderPrice': '-1'
                    }
                )
                print("Stop Loss configurado exitosamente. ID:", sl_order['id'], flush=True)
            else:
                print(f"Monto (${target_usdt:.2f} USDT) por debajo del mínimo de OKX.", flush=True)

        # 4. VENTA SPOT (SALIDA)
        elif ema_crossunder and btc_balance > 0:
            print("[SEÑAL VENTA] Cruce bajista confirmado.", flush=True)
            
            min_amount, _ = get_market_limits()
            formatted_btc = float(exchange.amount_to_precision(SYMBOL, btc_balance))

            if formatted_btc >= min_amount:
                print(f"Ejecutando VENTA: {formatted_btc} BTC", flush=True)
                order = exchange.create_market_sell_order(SYMBOL, formatted_btc)
                print("Orden realizada con éxito. ID:", order['id'], flush=True)
            else:
                print(f"Saldo de BTC ({btc_balance}) menor al mínimo operable ({min_amount}).", flush=True)

        else:
            print(f"[CHECK 4H] BTC: ${live_price:,.2f} | Sin señal de entrada/salida.", flush=True)

    except ccxt.InsufficientFunds as e:
        print(f"[ERROR OKX] Saldo insuficiente para operar: {e}", flush=True)
    except ccxt.NetworkError as e:
        print(f"[ERROR RED] Error de conexión con OKX: {e}", flush=True)
    except ccxt.ExchangeError as e:
        print(f"[ERROR API OKX] Rechazo por parte de OKX: {e}", flush=True)
    except Exception as e:
        print(f"[ERROR INESPERADO] {e}", flush=True)

def start_bot_loop():
    """Inicia el bucle de trading en segundo plano."""
    print("Bot OKX 4H iniciado y monitoreando...", flush=True)
    while True:
        run_strategy()
        time.sleep(300)

# Iniciar la lógica del bot en un hilo en segundo plano cuando Gunicorn cargue el archivo
bot_thread = threading.Thread(target=start_bot_loop, daemon=True)
bot_thread.start()
