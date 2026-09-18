import time
import os
import threading
from flask import Flask
import ccxt
import ccxt.base.errors as ccxt_errors
import pandas as pd
import pandas_ta as ta

# --- CONFIGURACIÓN DE FLASK (HEALTH CHECK EN RENDER) ---
app = Flask(__name__)

@app.route('/')
def health_check():
    return "Bot de trading OKX activo y en ejecución.", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

# Iniciar servidor web en hilo secundario para UptimeRobot / Render
flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

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

# OPTIMIZACIÓN 1: Cargar mercados una sola vez al inicializar
try:
    print("Cargando mercados de OKX...", flush=True)
    exchange.load_markets()
except Exception as e:
    print(f"Error al cargar mercados iniciales: {e}", flush=True)

def fetch_data():
    """Obtiene velas de 4H y calcula indicadores técnicos."""
    ohlcv = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=300)
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

    # Indicadores Técnicos
    df['ema9'] = ta.ema(df['close'], length=9)
    df['ema21'] = ta.ema(df['close'], length=21)
    df['sma200'] = ta.sma(df['close'], length=200)
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    df['rsi'] = ta.rsi(df['close'], length=14)
    
    # ADX
    adx_df = ta.adx(df['high'], df['low'], df['close'], length=14)
    df['adx'] = adx_df['ADX_14'] if adx_df is not None and 'ADX_14' in adx_df else 0

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

        # ----------------------------------------------------
        # 1. VELAS CONFIRMADAS / CERRADAS
        # ----------------------------------------------------
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

        # ----------------------------------------------------
        # 2. PRECIO Y SALDOS (OPTIMIZACIÓN RATE LIMITS)
        # ----------------------------------------------------
        # Se toma el precio de cierre de la vela en formación para ahorrar llamadas
        live_price = float(df.iloc[-1]['close'])

        balance = exchange.fetch_balance()
        usdt_balance = float(balance['free'].get('USDT', 0.0))
        btc_balance = float(balance['free'].get('BTC', 0.0))

        # ----------------------------------------------------
        # 3. COMPRA SPOT (ENTRADA)
        # ----------------------------------------------------
        if ema_crossover and trend_filter and strength_filter and rsi_filter:
            print("[SEÑAL COMPRA] Condición alcista confirmada.", flush=True)

            stop_distance_usdt = last_atr * ATR_STOP_MULTIPLIER
            stop_distance_pct = stop_distance_usdt / live_price

            if stop_distance_pct > 0:
                target_usdt = RISK_PER_TRADE_USDT / stop_distance_pct
            else:
                target_usdt = usdt_balance

            target_usdt = min(target_usdt, usdt_balance)

            # Validar límites de mercado reales de OKX
            min_amount, min_cost = get_market_limits()
            raw_btc_amount = target_usdt / live_price
            formatted_amount = float(exchange.amount_to_precision(SYMBOL, raw_btc_amount))

            # OPTIMIZACIÓN 2: Validación estricta con el monto ya formateado
            if target_usdt >= min_cost and formatted_amount >= min_amount:
                print(f"Ejecutando COMPRA: {formatted_amount} BTC (~${target_usdt:.2f} USDT)", flush=True)
                order = exchange.create_market_buy_order(SYMBOL, formatted_amount)
                print("Orden de compra realizada con éxito. ID:", order['id'], flush=True)

                # OPTIMIZACIÓN 3: Orden de Stop Loss automática en OKX
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
                        'orderPrice': '-1'  # Exec a precio de mercado al activarse
                    }
                )
                print("Stop Loss configurado exitosamente. ID:", sl_order['id'], flush=True)
            else:
                print(f"Monto (${target_usdt:.2f} USDT) por debajo del mínimo de OKX (Min USDT: {min_cost}, Min BTC: {min_amount}).", flush=True)

        # ----------------------------------------------------
        # 4. VENTA SPOT (SALIDA POR CRUCE)
        # ----------------------------------------------------
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

    # OPTIMIZACIÓN 5: Manejo de errores específicos
    except ccxt_errors.InsufficientFunds as e:
        print(f"[ERROR OKX] Saldo insuficiente para operar: {e}", flush=True)
    except ccxt_errors.NetworkError as e:
        print(f"[ERROR RED] Error de conexión con OKX: {e}", flush=True)
    except ccxt_errors.ExchangeError as e:
        print(f"[ERROR API OKX] Rechazo por parte de OKX: {e}", flush=True)
    except Exception as e:
        print(f"[ERROR INESPERADO] {e}", flush=True)

# --- BUCLE PRINCIPAL ---
if __name__ == "__main__":
    print("Bot OKX 4H iniciado y monitoreando...", flush=True)
    while True:
        run_strategy()
        time.sleep(300)  # Verificación cada 5 minutos
