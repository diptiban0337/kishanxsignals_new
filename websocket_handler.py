from flask_socketio import SocketIO, emit
from flask import session, request
from datetime import datetime, timedelta
import json
import logging
from typing import Dict, Optional, Set
import yfinance as yf
import pandas as pd
import numpy as np
import time
import random
import asyncio
from forex_data import get_cached_realtime_forex
import pandas_ta as ta

logger = logging.getLogger(__name__)

REAL_FOREX_SYMBOLS = {'AUDUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'USDCAD', 'USDCHF', 'NZDUSD', 'EURGBP', 'EURJPY', 'GBPJPY'}
REAL_INDIAN_SYMBOLS = {'NIFTY50', 'BANKNIFTY', 'NSEBANK', 'NSEIT', 'NSEINFRA', 'NSEPHARMA', 'NSEFMCG', 'NSEMETAL', 'NSEENERGY', 'NSEAUTO', 'NIFTYMIDCAP', 'NIFTYSMALLCAP', 'NIFTYNEXT50', 'NIFTY100', 'NIFTY500', 'NIFTYREALTY', 'NIFTYPVTBANK', 'NIFTYPSUBANK', 'NIFTYFIN', 'NIFTYMEDIA', 'RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', 'HINDUNILVR', 'SBIN', 'BHARTIARTL', 'KOTAKBANK', 'BAJFINANCE'}

class WebSocketHandler:
    def __init__(self, socketio):
        self.socketio = socketio
        self.connected_users = {}
        self.subscribed_symbols = {}
        self.price_cache = {}
        self.last_update = {}
        self.last_request_time = {}
        self.min_request_interval = 2  # Reduced to 2 seconds for better responsiveness
        self.rate_limit_backoff = {}
        self.max_backoff = 30
        self.forex_symbols = {
            'AUDUSD': 'AUDUSD=X',
            'EURUSD': 'EURUSD=X',
            'GBPUSD': 'GBPUSD=X',
            'USDJPY': 'USDJPY=X',
            'USDCAD': 'USDCAD=X',
            'USDCHF': 'USDCHF=X',
            'NZDUSD': 'NZDUSD=X',
            'EURGBP': 'EURGBP=X',
            'EURJPY': 'EURJPY=X',
            'GBPJPY': 'GBPJPY=X'
        }
        # Indian index symbols mapping
        self.indian_symbols = {
            'NIFTY50': '^NSEI',
            'BANKNIFTY': '^NSEBANK',
            'NSEBANK': '^NSEBANK',
            'NSEIT': '^CNXIT',
            'NSEINFRA': '^CNXINFRA',
            'NSEPHARMA': '^CNXPHARMA',
            'NSEFMCG': '^CNXFMCG',
            'NSEMETAL': '^CNXMETAL',
            'NSEENERGY': '^CNXENERGY',
            'NSEAUTO': '^CNXAUTO',
            'NIFTYMIDCAP': '^NSEI_MIDCAP',
            'NIFTYSMALLCAP': '^NSEI_SMALLCAP',
            'NIFTYNEXT50': '^NSEI_NEXT50',
            'NIFTY100': '^NSEI_100',
            'NIFTY500': '^NSEI_500',
            'NIFTYREALTY': '^NSEI_REALTY',
            'NIFTYPVTBANK': '^NSEI_PVTBANK',
            'NIFTYPSUBANK': '^NSEI_PSUBANK',
            'NIFTYFIN': '^NSEI_FIN',
            'NIFTYMEDIA': '^NSEI_MEDIA'
        }
        # Initialize OTC data handler
        from data_handlers import OTCDataHandler
        from config import (
            ALPHA_VANTAGE_API_KEY,
            OPENEXCHANGERATES_API_KEY,
            CURRENCYLAYER_API_KEY,
            API_TIMEOUT,
            CACHE_DURATION
        )
        self.otc_handler = OTCDataHandler(
            cache=None,  # We don't need caching in the websocket handler
            alpha_vantage_api_key=ALPHA_VANTAGE_API_KEY,
            openexchangerates_api_key=OPENEXCHANGERATES_API_KEY,
            currencylayer_api_key=CURRENCYLAYER_API_KEY,
            api_timeout=API_TIMEOUT,
            cache_duration=CACHE_DURATION
        )
        self.setup_handlers()
        
    def setup_handlers(self):
        """Setup WebSocket event handlers"""
        @self.socketio.on('connect')
        def handle_connect():
            if 'user_id' in session:
                self.handle_connect(session['user_id'])
            
        @self.socketio.on('disconnect')
        def handle_disconnect():
            if 'user_id' in session:
                self.handle_disconnect(session['user_id'])
            
        @self.socketio.on('subscribe')
        def handle_subscribe(data):
            try:
                symbol = data.get('symbol')
                if symbol and 'user_id' in session:
                    self.subscribe_symbol(session['user_id'], symbol)
            except Exception as e:
                logger.error(f'Error handling subscription: {str(e)}')
                
        @self.socketio.on('unsubscribe')
        def handle_unsubscribe(data):
            try:
                symbol = data.get('symbol')
                if symbol and 'user_id' in session:
                    self.unsubscribe_symbol(session['user_id'], symbol)
            except Exception as e:
                logger.error(f'Error handling unsubscription: {str(e)}')
                
    def handle_connect(self, user_id):
        """Handle new WebSocket connection with improved error handling."""
        try:
            self.connected_users[user_id] = {
                'sid': request.sid,
                'subscriptions': set(),
                'last_update': {},
                'error_count': 0
            }
            logger.info(f"User {user_id} connected via WebSocket")
            return True
        except Exception as e:
            logger.error(f"Error handling WebSocket connection: {str(e)}")
            return False

    def handle_disconnect(self, user_id):
        """Handle WebSocket disconnection with cleanup."""
        try:
            if user_id in self.connected_users:
                # Clean up subscriptions
                for symbol in self.connected_users[user_id]['subscriptions']:
                    self.unsubscribe_symbol(user_id, symbol)
                del self.connected_users[user_id]
                logger.info(f"User {user_id} disconnected from WebSocket")
        except Exception as e:
            logger.error(f"Error handling WebSocket disconnection: {str(e)}")

    async def subscribe_symbol(self, user_id, symbol):
        """Subscribe user to symbol updates"""
        try:
            if user_id not in self.connected_users:
                logger.warning(f"User {user_id} not connected")
                return False

            # Extract symbol string if it's a SQLite Row object
            if hasattr(symbol, 'symbol'):
                symbol = symbol.symbol
            elif isinstance(symbol, dict) and 'symbol' in symbol:
                symbol = symbol['symbol']

            # Add to user's subscriptions
            self.connected_users[user_id]['subscriptions'].add(symbol)

            # Send initial data
            data = await self.get_latest_price_data(symbol)
            if data:
                self.socketio.emit('price_update', data, room=request.sid)
                logger.info(f"User {user_id} subscribed to {symbol} with initial data")
                
                # Start periodic updates
                await self.start_periodic_updates(user_id, symbol)
                return True
            return False
        except Exception as e:
            logger.error(f"Error subscribing to symbol {symbol}: {str(e)}")
            return False

    async def start_periodic_updates(self, user_id, symbol):
        """Start periodic updates for a symbol"""
        try:
            async def update():
                if user_id in self.connected_users and symbol in self.connected_users[user_id]['subscriptions']:
                    data = await self.get_latest_price_data(symbol)
                    if data:
                        self.socketio.emit('price_update', data, room=self.connected_users[user_id]['sid'])
                        logger.debug(f"Sent periodic update for {symbol} to user {user_id}")
            
            # Schedule updates every 10 seconds instead of 5
            self.socketio.start_background_task(update)
        except Exception as e:
            logger.error(f"Error starting periodic updates for {symbol}: {str(e)}")

    def unsubscribe_symbol(self, user_id, symbol):
        """Unsubscribe user from symbol updates"""
        try:
            if user_id in self.connected_users and symbol in self.connected_users[user_id]['subscriptions']:
                self.connected_users[user_id]['subscriptions'].remove(symbol)
                logger.info(f"User {user_id} unsubscribed from {symbol}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error unsubscribing from symbol {symbol}: {str(e)}")
            return False

    async def get_latest_price_data(self, symbol):
        """Get latest price data for a symbol"""
        try:
            # Check if it's an Indian index
            if symbol in ['NIFTY50', 'BANKNIFTY']:
                try:
                    yahoo_symbol = self.indian_symbols.get(symbol, symbol)
                    ticker = yf.Ticker(yahoo_symbol)
                    data = ticker.history(period='1d', interval='1m')
                    if not data.empty:
                        current_price = data['Close'].iloc[-1]
                        high_24h = data['High'].max()
                        low_24h = data['Low'].min()
                        
                        # Calculate indicators
                        rsi = ta.rsi(data['Close'], length=14).iloc[-1]
                        macd = ta.macd(data['Close'])['MACD_12_26_9'].iloc[-1]
                        volatility = data['Close'].pct_change().std() * 100
                        
                        return {
                            'symbol': symbol,
                            'price': current_price,
                            'high_24h': high_24h,
                            'low_24h': low_24h,
                            'bid': current_price * 0.999,  # Simulated bid
                            'ask': current_price * 1.001,  # Simulated ask
                            'spread_pips': (current_price * 0.002) * 100,  # Simulated spread
                            'volatility': volatility,
                            'rsi': rsi,
                            'macd': macd,
                            'source': 'Yahoo Finance',
                            'status': 'success_with_indicators'
                        }
                    else:
                        logger.error(f"No data available for {symbol} from Yahoo Finance")
                        return {
                            'symbol': symbol,
                            'error': 'No data available',
                            'source': 'Yahoo Finance',
                            'status': 'error'
                        }
                except Exception as e:
                    logger.error(f"Error fetching data for {symbol} from Yahoo Finance: {str(e)}")
                    return {
                        'symbol': symbol,
                        'error': f'Failed to get real-time price data: {str(e)}',
                        'source': 'Yahoo Finance',
                        'status': 'error'
                    }
            
            # Handle OTC pairs
            if '_OTC' in symbol:
                try:
                    price_data = self.otc_handler.get_realtime_price(symbol, return_source=True)
                    if price_data:
                        if isinstance(price_data, tuple):
                            price, source = price_data
                            return {
                                'symbol': symbol,
                                'price': price,
                                'high_24h': None,
                                'low_24h': None,
                                'bid': None,
                                'ask': None,
                                'spread_pips': None,
                                'volatility': None,
                                'rsi': None,
                                'macd': None,
                                'source': source,
                                'status': 'success'
                            }
                        elif isinstance(price_data, dict):
                            return {
                                'symbol': symbol,
                                'price': price_data.get('price'),
                                'high_24h': price_data.get('high_24h'),
                                'low_24h': price_data.get('low_24h'),
                                'bid': price_data.get('bid'),
                                'ask': price_data.get('ask'),
                                'spread_pips': price_data.get('spread_pips'),
                                'volatility': price_data.get('volatility'),
                                'rsi': price_data.get('rsi'),
                                'macd': price_data.get('macd'),
                                'source': price_data.get('source'),
                                'status': 'success_with_indicators'
                            }
                        else:
                            logger.error(f"Unexpected type for price_data: {type(price_data)}")
                            return {
                                'symbol': symbol,
                                'error': 'Unexpected data format from OTC handler',
                                'source': 'None',
                                'status': 'error'
                            }
                except Exception as e:
                    logger.error(f"Error getting OTC price for {symbol}: {str(e)}")
                    return {
                        'symbol': symbol,
                        'error': f'Failed to get real-time price data: {str(e)}',
                        'source': 'None',
                        'status': 'error'
                    }
            
            # Handle Forex pairs
            try:
                rate = get_cached_realtime_forex(symbol)
                if rate:
                    # Generate simulated historical data for indicators
                    historical_data = pd.DataFrame({
                        'Open': [rate * (1 + np.random.normal(0, 0.0001)) for _ in range(100)],
                        'High': [rate * (1 + np.random.normal(0, 0.0002)) for _ in range(100)],
                        'Low': [rate * (1 + np.random.normal(0, 0.0002)) for _ in range(100)],
                        'Close': [rate * (1 + np.random.normal(0, 0.0001)) for _ in range(100)],
                        'Volume': [1000 + np.random.normal(0, 100) for _ in range(100)]
                    })
                    
                    # Calculate indicators
                    rsi = ta.rsi(historical_data['Close'], length=14).iloc[-1]
                    macd = ta.macd(historical_data['Close'])['MACD_12_26_9'].iloc[-1]
                    volatility = historical_data['Close'].pct_change().std() * 100
                    
                    return {
                        'symbol': symbol,
                        'price': rate,
                        'high_24h': rate * 1.001,  # Simulated high
                        'low_24h': rate * 0.999,   # Simulated low
                        'bid': rate * 0.999,       # Simulated bid
                        'ask': rate * 1.001,       # Simulated ask
                        'spread_pips': (rate * 0.002) * 100,  # Simulated spread
                        'volatility': volatility,
                        'rsi': rsi,
                        'macd': macd,
                        'source': 'ExchangeRate-API',
                        'status': 'success_with_indicators'
                    }
            except Exception as e:
                logger.error(f"Error getting Forex rate for {symbol}: {str(e)}")
                return {
                    'symbol': symbol,
                    'error': f'Failed to get real-time price data: {str(e)}',
                    'source': 'None',
                    'status': 'error'
                }
            
        except Exception as e:
            logger.error(f"Unexpected error in get_latest_price_data for {symbol}: {str(e)}")
            return {
                'symbol': symbol,
                'error': f'Failed to get real-time price data: {str(e)}',
                'source': 'None',
                'status': 'error'
            }

    def calculate_price_change(self, data):
        """Calculate price change percentage"""
        try:
            if len(data) < 2:
                return 0.0
            return ((data['Close'].iloc[-1] - data['Close'].iloc[0]) / data['Close'].iloc[0]) * 100
        except Exception as e:
            logger.error(f"Error calculating price change: {str(e)}")
            return 0.0

    def calculate_indicators(self, data):
        """Calculate technical indicators"""
        try:
            # RSI
            delta = data['Close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))

            # MACD
            exp1 = data['Close'].ewm(span=12, adjust=False).mean()
            exp2 = data['Close'].ewm(span=26, adjust=False).mean()
            macd = exp1 - exp2
            signal = macd.ewm(span=9, adjust=False).mean()

            return {
                'rsi': rsi.iloc[-1],
                'macd': macd.iloc[-1],
                'macd_signal': signal.iloc[-1]
            }
        except Exception as e:
            logger.error(f"Error calculating indicators: {str(e)}")
            return {
                'rsi': 50.0,
                'macd': 0.0,
                'macd_signal': 0.0
            }

    async def broadcast_updates(self):
        """Broadcast price updates to all connected clients with improved error handling."""
        while True:
            try:
                if not self.active_connections:
                    await asyncio.sleep(1)
                    continue

                for symbol in self.subscribed_symbols:
                    try:
                        # Add delay between requests to avoid rate limits
                        await asyncio.sleep(0.5)  # 500ms delay between symbols
                        
                        price_data = await self.get_latest_price_data(symbol)
                        if price_data:
                            # Prepare message with additional metadata
                            message = {
                                'type': 'price_update',
                                'data': price_data,
                                'timestamp': datetime.now().isoformat(),
                                'status': price_data.get('status', 'success')
                            }
                            
                            # Broadcast to all clients subscribed to this symbol
                            for connection in self.active_connections:
                                if symbol in self.connection_subscriptions.get(connection, set()):
                                    try:
                                        await connection.send_json(message)
                                    except Exception as e:
                                        logger.error(f"Error sending update to client for {symbol}: {str(e)}")
                                        # Remove failed connection
                                        await self.handle_disconnect(connection)
                        else:
                            logger.warning(f"No price data available for {symbol}")
                    except Exception as e:
                        logger.error(f"Error processing updates for {symbol}: {str(e)}")
                        continue

                await asyncio.sleep(1)  # Wait before next update cycle
            except Exception as e:
                logger.error(f"Error in broadcast loop: {str(e)}")
                await asyncio.sleep(1)  # Wait before retrying

    def update_price(self, symbol: str, price_data: Dict):
        """Update price data and broadcast to subscribed clients"""
        try:
            # Update cache
            self.price_cache[symbol] = {
                'symbol': symbol,
                'price': price_data.get('price'),
                'change': price_data.get('change'),
                'volume': price_data.get('volume'),
                'timestamp': datetime.now().isoformat()
            }
            
            # Broadcast to all subscribed clients
            self.socketio.emit('price_update', self.price_cache[symbol])
            
        except Exception as e:
            logger.error(f'Error updating price: {str(e)}')
            
    def broadcast_trade(self, trade_data: Dict):
        """Broadcast trade information to all clients"""
        try:
            self.socketio.emit('trade_update', trade_data)
        except Exception as e:
            logger.error(f'Error broadcasting trade: {str(e)}')
            
    def broadcast_signal(self, signal_data: Dict):
        """Broadcast trading signal to all clients"""
        try:
            self.socketio.emit('signal_update', signal_data)
        except Exception as e:
            logger.error(f'Error broadcasting signal: {str(e)}')
            
    def broadcast_alert(self, alert_data: Dict):
        """Broadcast alert to all clients"""
        try:
            self.socketio.emit('alert', alert_data)
        except Exception as e:
            logger.error(f'Error broadcasting alert: {str(e)}')
            
    def get_cached_price(self, symbol: str) -> Optional[Dict]:
        """Get cached price data for a symbol"""
        return self.price_cache.get(symbol)
        
    def clear_cache(self, symbol: Optional[str] = None):
        """Clear price cache for a symbol or all symbols"""
        try:
            if symbol:
                self.price_cache.pop(symbol, None)
            else:
                self.price_cache.clear()
        except Exception as e:
            logger.error(f'Error clearing cache: {str(e)}')

    async def register(self, client: asyncio.Queue):
        """Register a new WebSocket client."""
        self.clients.add(client)
        logger.info(f"New client registered. Total clients: {len(self.clients)}")
        
        # Send initial data to the new client
        if self.last_prices:
            await client.put(json.dumps(self.last_prices))

    async def unregister(self, client: asyncio.Queue):
        """Unregister a WebSocket client."""
        self.clients.remove(client)
        logger.info(f"Client unregistered. Total clients: {len(self.clients)}")

    async def broadcast(self, message: str):
        """Broadcast a message to all connected clients."""
        if not self.clients:
            return

        dead_clients = set()
        for client in self.clients:
            try:
                await client.put(message)
            except Exception as e:
                logger.error(f"Error broadcasting to client: {e}")
                dead_clients.add(client)

        # Remove dead clients
        for client in dead_clients:
            await self.unregister(client)

    async def update_prices(self):
        """Update prices for all OTC pairs and broadcast changes."""
        try:
            # Get all OTC pairs from the data handler
            otc_pairs = self.otc_handler.get_all_otc_pairs()
            
            for pair in otc_pairs:
                try:
                    # Get real-time price with source information
                    price, source = self.otc_handler.get_realtime_price(pair, return_source=True)
                    
                    if price is not None:
                        current_time = datetime.now().isoformat()
                        
                        # Calculate price change
                        last_price = self.last_prices.get(pair, {}).get('price')
                        price_change = None
                        if last_price is not None:
                            price_change = ((price - last_price) / last_price) * 100
                        
                        # Update last prices
                        self.last_prices[pair] = {
                            'price': price,
                            'change': f"{price_change:.2f}%" if price_change is not None else None,
                            'source': source,
                            'last_update': current_time
                        }
                except Exception as e:
                    logger.error(f"Error updating price for {pair}: {e}")
                    continue
            
            # Broadcast updates if there are any changes
            if self.last_prices:
                await self.broadcast(json.dumps(self.last_prices))
                
        except Exception as e:
            logger.error(f"Error in update_prices: {e}")

    async def start(self):
        """Start the WebSocket handler."""
        if self.is_running:
            return

        self.is_running = True
        logger.info("Starting WebSocket handler")

        while self.is_running:
            try:
                await self.update_prices()
                await asyncio.sleep(self.update_interval)
            except Exception as e:
                logger.error(f"Error in WebSocket handler loop: {e}")
                await asyncio.sleep(5)  # Wait before retrying

    async def stop(self):
        """Stop the WebSocket handler."""
        self.is_running = False
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
        logger.info("WebSocket handler stopped")

    def start_background(self):
        """Start the WebSocket handler in the background."""
        if self._update_task is None or self._update_task.done():
            self._update_task = asyncio.create_task(self.start())
            logger.info("WebSocket handler started in background") 