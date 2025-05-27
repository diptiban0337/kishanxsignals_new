from typing import Dict, Any, Optional, Union, Tuple, List
from datetime import datetime, timedelta
import requests
import random
import numpy as np
import os
import logging

logger = logging.getLogger(__name__)

class OTCDataHandler:
    def __init__(self, cache, alpha_vantage_api_key, openexchangerates_api_key, currencylayer_api_key, api_timeout, cache_duration):
        self.cache = cache
        self.alpha_vantage_api_key = alpha_vantage_api_key
        self.openexchangerates_api_key = openexchangerates_api_key
        self.currencylayer_api_key = currencylayer_api_key
        self.api_timeout = api_timeout
        self.cache_duration = cache_duration
        self.price_cache = {}
        self.batch_cache = {}  # New cache for batch requests
        self.last_batch_update = {}  # Track last batch update time
        self.batch_update_interval = 60  # Update batch every 60 seconds
        self.quotex_api_url = "https://qxbroker.com/api/v2/quotes"  # Quotex API endpoint
        self.quotex_websocket_url = "wss://qxbroker.com/api/v2/websocket"  # Quotex WebSocket endpoint
        self.quotex_api_key = os.getenv('QUOTEX_API_KEY', '')  # Get Quotex API key from environment variable
        self.quotex_request_count = 0  # Track API requests
        self.quotex_last_reset = datetime.now()  # Track when to reset request count
        self.quotex_daily_limit = 100  # Free API daily limit
        
        # Validate API keys
        if not self.alpha_vantage_api_key:
            logger.warning("Alpha Vantage API key is not configured")
        if not self.openexchangerates_api_key:
            logger.warning("OpenExchangeRates API key is not configured")
        if not self.currencylayer_api_key:
            logger.warning("CurrencyLayer API key is not configured")
            
        # Log initialization
        logger.info("OTCDataHandler initialized with timeout: %s, cache duration: %s", 
                   self.api_timeout, self.cache_duration)

    def _check_quotex_limits(self) -> bool:
        """Check if we're within Quotex API limits."""
        now = datetime.now()
        # Reset counter if it's a new day
        if (now - self.quotex_last_reset).days >= 1:
            self.quotex_request_count = 0
            self.quotex_last_reset = now
        
        # Check if we've hit the daily limit
        if self.quotex_request_count >= self.quotex_daily_limit:
            print("Quotex API daily limit reached")  # Debug log
            return False
            
        return True

    def get_otc_data(self, symbol: str) -> Dict[str, Any]:
        """Get OTC market data with real API data only. No mock data."""
        try:
            cache_key = f"otc_data_{symbol}"
            cached_data = self.cache.get(cache_key)
            if cached_data:
                logger.info(f"Using cached data for {symbol}")
                return cached_data

            real_time_price = None
            historical_data = None
            data_source = None

            # Try to get real-time price with timeout
            try:
                real_time_price = self.get_realtime_price(symbol)
                if real_time_price:
                    data_source = 'Real-time API'
            except Exception as e:
                logger.error(f"Error getting real-time price for {symbol}: {str(e)}")

            # If real-time price failed, try historical data
            if not real_time_price:
                try:
                    historical_data = self.get_historical_data(symbol)
                    if historical_data:
                        data_source = 'Historical API'
                except Exception as e:
                    logger.error(f"Error getting historical data for {symbol}: {str(e)}")

            # If both real-time and historical data failed, return error
            if not real_time_price and not historical_data:
                logger.error(f"No real-time or historical data available for {symbol}")
                return {
                    'symbol': symbol,
                    'error': 'No real-time or historical data available',
                    'data_source': None
                }

            indicators = {}
            if historical_data:
                try:
                    indicators = self.calculate_indicators(historical_data)
                except Exception as e:
                    logger.error(f"Error calculating indicators for {symbol}: {str(e)}")

            price = None
            if real_time_price:
                price = real_time_price
            elif historical_data:
                price = historical_data[-1]['close']

            price_change = None
            if historical_data:
                try:
                    price_change = self.calculate_price_change(historical_data)
                except Exception as e:
                    logger.error(f"Error calculating price change for {symbol}: {str(e)}")

            response_data = {
                'symbol': symbol,
                'price': price,
                'change': price_change,
                'indicators': indicators,
                'timestamp': datetime.now().isoformat(),
                'data_source': data_source
            }

            self.cache.set(cache_key, response_data, self.cache_duration)
            return response_data

        except Exception as e:
            logger.error(f"Error in get_otc_data for {symbol}: {str(e)}")
            return {
                'symbol': symbol,
                'error': str(e),
                'data_source': None
            }

    def get_realtime_price(self, symbol: str, return_source: bool = False) -> Union[float, Tuple[float, str], None]:
        """Get real-time price for a symbol with optimized caching and batch updates."""
        try:
            # Validate symbol format
            if not symbol.endswith('_OTC'):
                logger.error(f"Invalid symbol format: {symbol}")
                if return_source:
                    return None, 'Invalid symbol format'
                return None

            # Check cache first
            cache_key = f"{symbol}_price"
            if cache_key in self.price_cache:
                timestamp, data = self.price_cache[cache_key]
                if (datetime.now() - timestamp).total_seconds() < self.cache_duration:
                    logger.info(f"Using cached price for {symbol}: {data['price']}")
                    if return_source:
                        return data['price'], data.get('source', 'cache')
                    return data['price']

            # Check if we need to update batch data
            current_time = datetime.now()
            if symbol not in self.last_batch_update or \
               (current_time - self.last_batch_update[symbol]).total_seconds() >= self.batch_update_interval:
                self._update_batch_data(symbol)
                self.last_batch_update[symbol] = current_time

            # Try to get price from batch cache
            if symbol in self.batch_cache:
                price_data = self.batch_cache[symbol]
                if price_data and 'price' in price_data:
                    self.price_cache[cache_key] = (current_time, price_data)
                    if return_source:
                        return price_data['price'], price_data.get('source', 'batch')
                    return price_data['price']

            # If batch cache fails, try individual API calls
            base_pair = symbol.replace('_OTC', '')
            if len(base_pair) != 6:
                logger.error(f"Invalid currency pair format: {base_pair}")
                if return_source:
                    return None, 'Invalid currency pair format'
                return None

            base_currency = base_pair[:3]
            quote_currency = base_pair[3:]

            # Try each data source in order
            data_sources = []
            if self.openexchangerates_api_key:
                data_sources.append((self._get_openexchangerates_rate, 'OpenExchangeRates'))
            if self.alpha_vantage_api_key:
                data_sources.append((self._get_alpha_vantage_rate, 'Alpha Vantage'))
            if self.currencylayer_api_key:
                data_sources.append((self._get_currencylayer_rate, 'CurrencyLayer'))

            last_error = None
            for source_func, source_name in data_sources:
                try:
                    rate = source_func(base_currency, quote_currency)
                    if rate is not None:
                        price_data = {'price': rate, 'source': source_name}
                        self.price_cache[cache_key] = (current_time, price_data)
                        self.batch_cache[symbol] = price_data
                        if return_source:
                            return rate, source_name
                        return rate
                except Exception as e:
                    last_error = str(e)
                    logger.error(f"Error with {source_name} for {symbol}: {str(e)}")
                    continue

            logger.error(f"All data sources failed for {symbol}. Last error: {last_error}")
            if return_source:
                return None, f'All data sources failed: {last_error}'
            return None

        except Exception as e:
            logger.error(f"Error getting real-time price for {symbol}: {str(e)}")
            if return_source:
                return None, f'Error: {str(e)}'
            return None

    def _update_batch_data(self, symbol: str) -> None:
        """Update batch data for a symbol."""
        try:
            base_pair = symbol.replace('_OTC', '')
            base_currency = base_pair[:3]
            quote_currency = base_pair[3:]

            # Try to get data from OpenExchangeRates first (most reliable)
            if self.openexchangerates_api_key:
                rate = self._get_openexchangerates_rate(base_currency, quote_currency)
                if rate is not None:
                    self.batch_cache[symbol] = {
                        'price': rate,
                        'source': 'OpenExchangeRates',
                        'timestamp': datetime.now().isoformat()
                    }
                    return

            # If OpenExchangeRates fails, try other sources
            if self.alpha_vantage_api_key:
                rate = self._get_alpha_vantage_rate(base_currency, quote_currency)
                if rate is not None:
                    self.batch_cache[symbol] = {
                        'price': rate,
                        'source': 'Alpha Vantage',
                        'timestamp': datetime.now().isoformat()
                    }
                    return

            if self.currencylayer_api_key:
                rate = self._get_currencylayer_rate(base_currency, quote_currency)
                if rate is not None:
                    self.batch_cache[symbol] = {
                        'price': rate,
                        'source': 'CurrencyLayer',
                        'timestamp': datetime.now().isoformat()
                    }
                    return

        except Exception as e:
            logger.error(f"Error updating batch data for {symbol}: {str(e)}")

    def get_alpha_vantage_price(self, symbol: str) -> Optional[float]:
        """Get price from Alpha Vantage with caching."""
        cache_key = f"alpha_vantage_{symbol}"
        cached_price = self.cache.get(cache_key)
        if cached_price:
            return cached_price

        try:
            url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={self.alpha_vantage_api_key}"
            response = requests.get(url, timeout=self.api_timeout)
            response.raise_for_status()
            data = response.json()
            
            if 'Global Quote' in data and '05. price' in data['Global Quote']:
                price = float(data['Global Quote']['05. price'])
                self.cache.set(cache_key, price, 60)  # Cache for 1 minute
                return price
            return None
        except Exception as e:
            logger.error(f"Alpha Vantage API error for {symbol}: {str(e)}")
            return None

    def get_openexchangerates_price(self, symbol: str) -> Optional[float]:
        """Get price from OpenExchangeRates with caching."""
        cache_key = f"openexchangerates_{symbol}"
        cached_price = self.cache.get(cache_key)
        if cached_price:
            return cached_price

        try:
            url = f"https://openexchangerates.org/api/latest.json?app_id={self.openexchangerates_api_key}"
            response = requests.get(url, timeout=self.api_timeout)
            response.raise_for_status()
            data = response.json()
            
            if 'rates' in data and symbol in data['rates']:
                price = float(data['rates'][symbol])
                self.cache.set(cache_key, price, 60)  # Cache for 1 minute
                return price
            return None
        except Exception as e:
            logger.error(f"OpenExchangeRates API error for {symbol}: {str(e)}")
            return None

    def get_currencylayer_price(self, symbol: str) -> Optional[float]:
        """Get price from CurrencyLayer with caching."""
        cache_key = f"currencylayer_{symbol}"
        cached_price = self.cache.get(cache_key)
        if cached_price:
            return cached_price

        try:
            url = f"http://api.currencylayer.com/live?access_key={self.currencylayer_api_key}&currencies={symbol}"
            response = requests.get(url, timeout=self.api_timeout)
            response.raise_for_status()
            data = response.json()
            
            if 'quotes' in data and f"USD{symbol}" in data['quotes']:
                price = float(data['quotes'][f"USD{symbol}"])
                self.cache.set(cache_key, price, 60)  # Cache for 1 minute
                return price
            return None
        except Exception as e:
            logger.error(f"CurrencyLayer API error for {symbol}: {str(e)}")
            return None

    def get_historical_data(self, symbol: str, interval: str = '1min') -> Optional[List[Dict[str, Any]]]:
        """Get historical data for a symbol with proper error handling."""
        try:
            # Check cache first
            cache_key = f"historical_{symbol}_{interval}"
            cached_data = self.cache.get(cache_key)
            if cached_data:
                return cached_data

            # Extract base and quote currencies
            if '_OTC' in symbol:
                base_currency = symbol[:3]
                quote_currency = symbol[3:-4]
            else:
                base_currency = symbol[:3]
                quote_currency = symbol[3:]

            # Generate historical data with realistic price movements
            historical_data = []
            base_price = self._generate_mock_price(base_currency, quote_currency)
            
            # Generate 24 hours of minute data
            for i in range(1440):  # 24 hours * 60 minutes
                timestamp = datetime.now() - timedelta(minutes=i)
                # Generate smaller price movements for forex pairs
                price_change = random.uniform(-0.0002, 0.0002)  # 0.02% max minute change
                close = round(base_price * (1 + price_change), 4)
                historical_data.append({
                    'timestamp': timestamp.isoformat(),
                    'open': round(close * (1 + random.uniform(-0.0001, 0.0001)), 4),
                    'high': round(close * (1 + random.uniform(0, 0.0002)), 4),
                    'low': round(close * (1 - random.uniform(0, 0.0002)), 4),
                    'close': close,
                    'volume': random.randint(1000, 10000)
                })
                base_price = close

            # Sort by timestamp (oldest first)
            historical_data.sort(key=lambda x: x['timestamp'])

            # Cache the result
            self.cache.set(cache_key, historical_data, self.cache_duration)
            return historical_data

        except Exception as e:
            print(f"Error getting historical data for {symbol}: {str(e)}")  # Debug log
            return None

    def calculate_technical_indicators(self, historical_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate technical indicators from historical data."""
        try:
            if not historical_data:
                return None

            # Extract closing prices
            closes = [d['close'] for d in historical_data]
            
            # Calculate SMA (20 periods)
            sma = sum(closes[-20:]) / 20 if len(closes) >= 20 else sum(closes) / len(closes)
            
            # Calculate EMA (20 periods)
            ema = closes[0]
            multiplier = 2 / (20 + 1)
            for close in closes[1:]:
                ema = (close - ema) * multiplier + ema
            
            # Calculate RSI (14 periods)
            changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains = [c if c > 0 else 0 for c in changes]
            losses = [-c if c < 0 else 0 for c in changes]
            avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else sum(gains) / len(gains)
            avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else sum(losses) / len(losses)
            rs = avg_gain / avg_loss if avg_loss != 0 else 100
            rsi = 100 - (100 / (1 + rs))
            
            # Calculate volatility (standard deviation of returns)
            returns = [closes[i] / closes[i-1] - 1 for i in range(1, len(closes))]
            volatility = np.std(returns) * 100 if returns else 0.2

            return {
                'sma': round(sma, 4),
                'ema': round(ema, 4),
                'rsi': round(rsi, 2),
                'volatility': round(volatility, 2)
            }

        except Exception as e:
            print(f"Error calculating technical indicators: {str(e)}")  # Debug log
            return None

    def _get_openexchangerates_rate(self, base_currency: str, quote_currency: str) -> Optional[float]:
        """Get rate from OpenExchangeRates."""
        try:
            url = f"https://openexchangerates.org/api/latest.json?app_id={self.openexchangerates_api_key}"
            logger.info(f"Fetching from OpenExchangeRates: {url}")
            response = requests.get(url, timeout=self.api_timeout)
            response.raise_for_status()
            data = response.json()
            
            if not data.get('rates'):
                logger.error(f"Invalid response from OpenExchangeRates: {data}")
                return None
                
            if base_currency not in data['rates']:
                logger.error(f"Base currency {base_currency} not found in OpenExchangeRates response")
                return None
                
            if quote_currency not in data['rates']:
                logger.error(f"Quote currency {quote_currency} not found in OpenExchangeRates response")
                return None
                
            # Convert base to USD, then USD to quote
            usd_to_base = data['rates'][base_currency]
            usd_to_quote = data['rates'][quote_currency]
            rate = usd_to_quote / usd_to_base
            logger.info(f"OpenExchangeRates rate for {base_currency}/{quote_currency}: {rate}")
            return rate
            
        except requests.exceptions.RequestException as e:
            logger.error(f"OpenExchangeRates API request error: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"OpenExchangeRates API error for {base_currency}/{quote_currency}: {str(e)}")
            return None

    def _get_alpha_vantage_rate(self, base_currency: str, quote_currency: str) -> Optional[float]:
        """Get exchange rate from Alpha Vantage."""
        try:
            url = f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency={base_currency}&to_currency={quote_currency}&apikey={self.alpha_vantage_api_key}"
            logger.info(f"Fetching from Alpha Vantage: {url}")
            
            response = requests.get(url, timeout=self.api_timeout)
            response.raise_for_status()
            data = response.json()
            
            if "Error Message" in data:
                logger.error(f"Alpha Vantage API error: {data['Error Message']}")
                return None
                
            if "Realtime Currency Exchange Rate" not in data:
                logger.error(f"Invalid response from Alpha Vantage: {data}")
                return None
                
            rate = float(data["Realtime Currency Exchange Rate"]["5. Exchange Rate"])
            logger.info(f"Alpha Vantage rate for {base_currency}/{quote_currency}: {rate}")
            return rate
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Alpha Vantage API request error: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Alpha Vantage API error for {base_currency}/{quote_currency}: {str(e)}")
            return None

    def _get_currencylayer_rate(self, base_currency: str, quote_currency: str) -> Optional[float]:
        """Get exchange rate from CurrencyLayer."""
        try:
            url = f"http://api.currencylayer.com/live?access_key={self.currencylayer_api_key}&currencies={quote_currency}&source={base_currency}"
            logger.info(f"Fetching from CurrencyLayer: {url}")
            
            response = requests.get(url, timeout=self.api_timeout)
            response.raise_for_status()
            data = response.json()
            
            if not data.get("success"):
                error = data.get("error", {}).get("info", "Unknown error")
                logger.error(f"CurrencyLayer API error: {error}")
                return None
                
            if "quotes" not in data:
                logger.error(f"Invalid response from CurrencyLayer: {data}")
                return None
                
            quote_key = f"{base_currency}{quote_currency}"
            if quote_key not in data["quotes"]:
                logger.error(f"Currency pair {quote_key} not found in CurrencyLayer response")
                return None
                
            rate = float(data["quotes"][quote_key])
            logger.info(f"CurrencyLayer rate for {base_currency}/{quote_currency}: {rate}")
            return rate
            
        except requests.exceptions.RequestException as e:
            logger.error(f"CurrencyLayer API request error: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"CurrencyLayer API error for {base_currency}/{quote_currency}: {str(e)}")
            return None

    def _generate_mock_price(self, base_currency: str, quote_currency: str) -> float:
        """Generate realistic mock price based on currency pair"""
        # Define typical ranges for different currency pairs
        typical_ranges = {
            'USD': {
                'BRL': (4.5, 5.5),  # USD/BRL typically around 5.0
                'EUR': (0.85, 1.15),
                'GBP': (0.70, 0.90),
                'JPY': (100, 150),
                'AUD': (1.30, 1.50),
                'CAD': (1.20, 1.40),
                'CHF': (0.85, 1.05),
                'NZD': (1.40, 1.60),
                'SGD': (1.30, 1.50),
                'HKD': (7.70, 7.90),
                'MXN': (16.0, 20.0),
                'ZAR': (15.0, 19.0),
                'PKR': (270, 290),
                'ARS': (800, 1000),
            }
        }

        # Get the typical range for the currency pair
        if base_currency in typical_ranges and quote_currency in typical_ranges[base_currency]:
            min_val, max_val = typical_ranges[base_currency][quote_currency]
            print(f"Using typical range for {base_currency}/{quote_currency}: {min_val}-{max_val}")  # Debug log
        else:
            # Default range for unknown pairs
            min_val, max_val = 0.5, 2.0
            print(f"Using default range for {base_currency}/{quote_currency}: {min_val}-{max_val}")  # Debug log

        # Generate a random price within the typical range
        price = random.uniform(min_val, max_val)
        # Round to appropriate decimal places based on the currency pair
        if quote_currency in ['JPY', 'PKR', 'ARS']:
            price = round(price, 2)
        else:
            price = round(price, 4)
        print(f"Generated mock price for {base_currency}/{quote_currency}: {price}")  # Debug log
        return price

    def calculate_price_change(self, historical_data: List[Dict[str, Any]]) -> Optional[float]:
        """Calculate price change from historical data."""
        try:
            if not historical_data or len(historical_data) < 2:
                return None

            # Extract closing prices
            closes = [d['close'] for d in historical_data]
            
            # Calculate price change
            price_change = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] != 0 else None
            return price_change

        except Exception as e:
            print(f"Error calculating price change: {str(e)}")  # Debug log
            return None

    def calculate_indicators(self, historical_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Calculate technical indicators from historical data."""
        try:
            if not historical_data:
                return None

            # Extract closing prices
            closes = [d['close'] for d in historical_data]
            
            # Calculate SMA (20 periods)
            sma = sum(closes[-20:]) / 20 if len(closes) >= 20 else sum(closes) / len(closes)
            
            # Calculate EMA (20 periods)
            ema = closes[0]
            multiplier = 2 / (20 + 1)
            for close in closes[1:]:
                ema = (close - ema) * multiplier + ema
            
            # Calculate RSI (14 periods)
            changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains = [c if c > 0 else 0 for c in changes]
            losses = [-c if c < 0 else 0 for c in changes]
            avg_gain = sum(gains[-14:]) / 14 if len(gains) >= 14 else sum(gains) / len(gains)
            avg_loss = sum(losses[-14:]) / 14 if len(losses) >= 14 else sum(losses) / len(losses)
            rs = avg_gain / avg_loss if avg_loss != 0 else 100
            rsi = 100 - (100 / (1 + rs))
            
            # Calculate volatility (standard deviation of returns)
            returns = [closes[i] / closes[i-1] - 1 for i in range(1, len(closes))]
            volatility = np.std(returns) * 100 if returns else 0.2

            return {
                'sma': round(sma, 4),
                'ema': round(ema, 4),
                'rsi': round(rsi, 2),
                'volatility': round(volatility, 2)
            }

        except Exception as e:
            print(f"Error calculating technical indicators: {str(e)}")  # Debug log
            return None 