import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, send_file, g, flash, jsonify, abort, send_from_directory
from datetime import datetime, timedelta
import random
import csv
import io
import requests
from werkzeug.security import generate_password_hash, check_password_hash
import math
from statistics import NormalDist
import time
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib import colors
import pandas as pd
import numpy as np
import yfinance as yf
from flask_socketio import SocketIO, emit
from data_handlers import OTCDataHandler
from trading_system import TradingSystem
from risk_manager import RiskManager
from auto_trader import AutoTrader
from websocket_handler import WebSocketHandler
from config import (
    ALPHA_VANTAGE_API_KEY,
    OPENEXCHANGERATES_API_KEY,
    CURRENCYLAYER_API_KEY,
    API_TIMEOUT,
    CACHE_DURATION,
    PREMIUM_API_ENABLED,
    PREMIUM_API_CALLS_PER_MINUTE,
    OTC_PAIRS,
    OTC_PAIR_CATEGORIES
)
import logging
from typing import Dict
import json
import threading
import asyncio
from forex_data import get_cached_realtime_forex
from statistics import NormalDist
import functools
from functools import lru_cache
from flask_caching import Cache

# Get absolute paths for PythonAnywhere
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_FOLDER = os.path.join(BASE_DIR, 'static')
TEMPLATES_FOLDER = os.path.join(BASE_DIR, 'templates')
DATABASE = os.path.join(BASE_DIR, 'kishanx.db')

# Configure Flask app for PythonAnywhere
app = Flask(__name__,
    static_url_path='/static',
    static_folder=STATIC_FOLDER,
    template_folder=TEMPLATES_FOLDER
)
app.secret_key = "kishan_secret"

# Configure caching
cache = Cache(app, config={
    'CACHE_TYPE': 'filesystem',
    'CACHE_DIR': os.path.join(BASE_DIR, 'flask_cache'),
    'CACHE_DEFAULT_TIMEOUT': CACHE_DURATION
})

# Configure static file serving
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000  # 1 year in seconds
app.config['STATIC_FOLDER'] = STATIC_FOLDER

# Add cache headers to static files
@app.after_request
def add_header(response):
    if 'Cache-Control' not in response.headers:
        if request.path.startswith('/static/'):
            response.headers['Cache-Control'] = 'public, max-age=31536000'
        else:
            response.headers['Cache-Control'] = 'no-store'
    return response

# Add this after Flask app configuration
print(f"Using template folder: {app.template_folder}")
print(f"Template folder exists: {os.path.exists(app.template_folder)}")
print(f"Available templates: {os.listdir(app.template_folder)}")

# Configure SocketIO for PythonAnywhere
socketio = SocketIO(app, 
    async_mode='eventlet',
    cors_allowed_origins="*",
    ping_timeout=60,
    ping_interval=25,
    logger=True,
    engineio_logger=True
)

# Configure logging for PythonAnywhere
logging.basicConfig(
    level=logging.DEBUG,  # Changed to DEBUG for more detailed logs
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(BASE_DIR, 'trading_app.log')),
        logging.StreamHandler()  # This ensures logs appear in console
    ]
)
logger = logging.getLogger(__name__)

# Add this right after logging configuration
print(f"Log file location: {os.path.join(BASE_DIR, 'trading_app.log')}")
print(f"Database location: {DATABASE}")

# Error handlers for PythonAnywhere
@app.errorhandler(404)
def not_found_error(error):
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('500.html'), 500

# Static file handler for PythonAnywhere
@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(app.static_folder, filename)

# Debug logging for PythonAnywhere
@app.before_request
def log_request_info():
    app.logger.debug('Headers: %s', request.headers)
    app.logger.debug('Body: %s', request.get_data())

# Initialize components
trading_system = TradingSystem()
risk_manager = RiskManager()
auto_trader = AutoTrader(trading_system, risk_manager)
websocket_handler = WebSocketHandler(socketio)

# Initialize OTCDataHandler with required parameters
otc_handler = OTCDataHandler(
    cache=cache,  # Flask-Caching instance
    alpha_vantage_api_key=ALPHA_VANTAGE_API_KEY,
    openexchangerates_api_key=OPENEXCHANGERATES_API_KEY,
    currencylayer_api_key=CURRENCYLAYER_API_KEY,
    api_timeout=API_TIMEOUT,
    cache_duration=CACHE_DURATION
)

# Import OTC pairs from config
otc_pairs = OTC_PAIRS

# --- Database helpers ---
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    try:
        logger.info("=== Initializing Database ===")
        with app.app_context():
            db = get_db()
            logger.info("Creating database tables...")
            db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                last_login TEXT,
                balance REAL DEFAULT 10000.0,
                is_premium BOOLEAN DEFAULT 0
            );

            -- Add balance column if it doesn't exist
            PRAGMA table_info(users);
            BEGIN TRANSACTION;
            INSERT OR IGNORE INTO users (id, username, password, registered_at, last_login, balance, is_premium)
            SELECT id, username, password, registered_at, last_login, 10000.0, is_premium
            FROM users;
            COMMIT;

            CREATE TABLE IF NOT EXISTS user_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                UNIQUE(user_id, symbol)
            );

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                confidence REAL NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity REAL NOT NULL,
                status TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                profit_loss REAL,
                stop_loss REAL,
                take_profit REAL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                quantity REAL NOT NULL,
                average_price REAL NOT NULL,
                last_updated TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id),
                UNIQUE(user_id, symbol)
            );

            CREATE TABLE IF NOT EXISTS market_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                price REAL NOT NULL,
                volume REAL,
                timestamp TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS risk_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                max_position_size REAL DEFAULT 0.02,
                max_daily_loss REAL DEFAULT 0.05,
                max_drawdown REAL DEFAULT 0.15,
                stop_loss_pct REAL DEFAULT 0.02,
                take_profit_pct REAL DEFAULT 0.04,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS portfolio_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                portfolio_value REAL NOT NULL,
                timestamp TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id)
            );
            ''')
            db.commit()
            logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {str(e)}", exc_info=True)
        raise

# Initialize database on startup
with app.app_context():
    init_db()

# --- User helpers ---
def get_user_by_username(username):
    try:
        logger.info(f"Looking up user by username: {username}")
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        if user:
            logger.info(f"Found user: {username}")
            return user
        logger.warning(f"No user found with username: {username}")
        return None
    except Exception as e:
        logger.error(f"Error looking up user by username: {str(e)}", exc_info=True)
        return None

def get_user_by_id(user_id):
    try:
        logger.info(f"Attempting to get user by ID: {user_id}")
        db = get_db()
        user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
        if user:
            logger.info(f"Successfully retrieved user: {dict(user)}")
            return dict(user)
        logger.warning(f"No user found with ID: {user_id}")
        return None
    except Exception as e:
        logger.error(f"Database error in get_user_by_id: {str(e)}", exc_info=True)
        return None

def create_user(username, password):
    try:
        logger.info(f"Creating user in database: {username}")
        db = get_db()
        hashed = generate_password_hash(password)
        db.execute('INSERT INTO users (username, password, registered_at, balance) VALUES (?, ?, ?, ?)',
                   (username, hashed, datetime.now().isoformat(), 10000.0))
        db.commit()
        logger.info(f"Successfully created user in database: {username}")
    except Exception as e:
        logger.error(f"Error creating user in database: {str(e)}", exc_info=True)
        raise

def update_last_login(user_id):
    db = get_db()
    db.execute('UPDATE users SET last_login = ? WHERE id = ?', (datetime.now().isoformat(), user_id))
    db.commit()

def verify_user(username, password):
    try:
        logger.info(f"Verifying user: {username}")
        user = get_user_by_username(username)
        if user and check_password_hash(user['password'], password):
            logger.info(f"User verification successful: {username}")
            return user
        logger.warning(f"User verification failed: {username}")
        return None
    except Exception as e:
        logger.error(f"Error verifying user: {str(e)}", exc_info=True)
        return None
    
def login_required(f):
    """Decorate routes to require login."""
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page.", "warning")
            return redirect(url_for("login", next=request.url))
        return f(*args, **kwargs)
    return decorated_function


# --- Signal helpers ---
def save_signal(user_id, time, pair, direction):
    db = get_db()
    db.execute('INSERT INTO signals (user_id, symbol, direction, confidence, created_at) VALUES (?, ?, ?, ?, ?)',
               (user_id, pair, direction, 0.0, datetime.now().isoformat()))
    db.commit()

def get_signals_for_user(user_id, limit=20):
    try:
        logger.info(f"Fetching signals for user {user_id} with limit {limit}")
        db = get_db()
        signals = db.execute('SELECT * FROM signals WHERE user_id = ? ORDER BY created_at DESC LIMIT ?', 
                           (user_id, limit)).fetchall()
        logger.info(f"Retrieved {len(signals) if signals else 0} signals")
        return [dict(signal) for signal in signals] if signals else []
    except Exception as e:
        logger.error(f"Error fetching signals for user {user_id}: {str(e)}", exc_info=True)
        return []

def get_signal_stats(user_id):
    try:
        logger.info(f"Fetching signal stats for user {user_id}")
        db = get_db()
        total = db.execute('SELECT COUNT(*) FROM signals WHERE user_id = ?', (user_id,)).fetchone()[0] or 0
        by_pair = [dict(row) for row in db.execute('SELECT symbol as pair, COUNT(*) as count FROM signals WHERE user_id = ? GROUP BY symbol', (user_id,)).fetchall()]
        by_direction = [dict(row) for row in db.execute('SELECT direction, COUNT(*) as count FROM signals WHERE user_id = ? GROUP BY direction', (user_id,)).fetchall()]
        logger.info(f"Signal stats - Total: {total}, Pairs: {len(by_pair)}, Directions: {len(by_direction)}")
        return total, by_pair, by_direction
    except Exception as e:
        logger.error(f"Error fetching signal stats for user {user_id}: {str(e)}", exc_info=True)
        return 0, [], []

# --- App logic ---
pairs = ["EURAUD", "USDCHF", "USDBRL", "AUDUSD", "GBPCAD", "EURCAD", "NZDUSD", "USDPKR", "EURUSD", "USDCAD", "AUDCHF", "GBPUSD", "EURGBP"]
brokers = ["Quotex", "Pocket Option", "Binolla", "IQ Option", "Bullex", "Exnova"]

# Initialize price cache
price_cache = {}

# Symbol mapping for Indian markets
symbol_map = {
    # Major Indices
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "NSEBANK": "^NSEBANK",
    "NSEIT": "^CNXIT",
    "NSEINFRA": "^CNXINFRA",
    "NSEPHARMA": "^CNXPHARMA",
    "NSEFMCG": "^CNXFMCG",
    "NSEMETAL": "^CNXMETAL",
    "NSEENERGY": "^CNXENERGY",
    "NSEAUTO": "^CNXAUTO",
    # Additional Indices
    "NIFTYMIDCAP": "^NSEI_MIDCAP",
    "NIFTYSMALLCAP": "^NSEI_SMALLCAP",
    "NIFTYNEXT50": "^NSEI_NEXT50",
    "NIFTY100": "^NSEI_100",
    "NIFTY500": "^NSEI_500",
    # Sector Indices
    "NIFTYREALTY": "^NSEI_REALTY",
    "NIFTYPVTBANK": "^NSEI_PVTBANK",
    "NIFTYPSUBANK": "^NSEI_PSUBANK",
    "NIFTYFIN": "^NSEI_FIN",
    "NIFTYMEDIA": "^NSEI_MEDIA",
    # Popular Stocks
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "HDFCBANK": "HDFCBANK.NS",
    "INFY": "INFY.NS",
    "ICICIBANK": "ICICIBANK.NS",
    "HINDUNILVR": "HINDUNILVR.NS",
    "SBIN": "SBIN.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "KOTAKBANK": "KOTAKBANK.NS",
    "BAJFINANCE": "BAJFINANCE.NS"
}

broker_payouts = {
    "Quotex": 0.85,
    "Pocket Option": 0.80,
    "Binolla": 0.78,
    "IQ Option": 0.82,
    "Bullex": 0.75,
    "Exnova": 0.77
}

def get_cached_realtime_forex(pair, api_key=ALPHA_VANTAGE_API_KEY, cache_duration=CACHE_DURATION, return_source=False):
    """
    Get cached real-time forex rate with improved error handling for premium API.
    
    Args:
        pair (str): Currency pair (e.g., 'EURUSD')
        api_key (str): Alpha Vantage API key
        cache_duration (int): Cache duration in seconds
    
    Returns:
        float: Current exchange rate or None if unavailable
    """
    cache_key = f"forex_{pair}"
    now = time.time()
    
    # Check cache
    if cache_key in price_cache:
        price, timestamp = price_cache[cache_key]
        if now - timestamp < cache_duration:
            logger.info(f"Using cached price for {pair}: {price} (cached {int(now - timestamp)} seconds ago)")
            if return_source:
                return price, 'cached'
            return price
        else:
            logger.info(f"Cache expired for {pair}, fetching new data...")
    
    try:
        price = get_realtime_forex(pair, api_key)
        if price is not None:
            price_cache[cache_key] = (price, now)
            logger.info(f"Updated cache for {pair} with new price: {price}")
            if return_source:
                return price, 'cached'
            return price
    except Exception as e:
        logger.error(f"Error getting forex rate for {pair}: {str(e)}")
        return None

def get_realtime_forex(pair, api_key=ALPHA_VANTAGE_API_KEY):
    """
    Get real-time forex rate with support for premium API features.
    
    Args:
        pair (str): Currency pair (e.g., 'EURUSD')
        api_key (str): Alpha Vantage API key
    
    Returns:
        float: Current exchange rate or None if unavailable
    """
    if not api_key or api_key == "YOUR_PREMIUM_API_KEY":
        logger.warning("Using fallback data - No valid API key provided")
        return None
        
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    
    # Try multiple data sources in order of preference
    data_sources = [
        lambda: _get_alpha_vantage_rate(pair, api_key),
        lambda: _get_exchange_rate_api_rate(pair),
        lambda: _get_fixer_io_rate(pair)
    ]
    
    for source in data_sources:
        try:
            rate = source()
            if rate is not None:
                return rate
        except Exception as e:
            logger.error(f"Error with data source: {str(e)}")
            continue
    
    return None

def _get_alpha_vantage_rate(pair, api_key):
    """Get rate from Alpha Vantage with premium API support."""
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    
    url = f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE&from_currency={from_symbol}&to_currency={to_symbol}&apikey={api_key}"
    
    try:
        logger.info(f"Fetching rate for {pair} from Alpha Vantage")
        response = requests.get(url, timeout=API_TIMEOUT)
        data = response.json()
        
        if "Error Message" in data:
            logger.error(f"Alpha Vantage API Error: {data['Error Message']}")
            return None
            
        if "Note" in data:
            if "premium" in data["Note"].lower():
                if not PREMIUM_API_ENABLED:
                    logger.warning("Premium API features required. Set PREMIUM_API_ENABLED=True in config.py")
                return None
            elif "API call frequency" in data["Note"]:
                logger.warning(f"API rate limit reached: {data['Note']}")
                return None
            
        if "Realtime Currency Exchange Rate" in data:
            rate = float(data["Realtime Currency Exchange Rate"]["5. Exchange Rate"])
            logger.info(f"Successfully fetched rate for {pair}: {rate}")
            return rate
            
    except requests.exceptions.Timeout:
        logger.error(f"Timeout while fetching rate for {pair}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Request error while fetching rate for {pair}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error while fetching rate for {pair}: {str(e)}")
        return None
    
    return None

def _get_exchange_rate_api_rate(pair):
    """Get rate from ExchangeRate-API with error handling."""
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    url = f"https://open.er-api.com/v6/latest/{from_symbol}"
    
    try:
        logger.info(f"Fetching rate for {pair} from ExchangeRate-API")
        response = requests.get(url, timeout=API_TIMEOUT)
        data = response.json()
        
        if data.get("result") == "error":
            logger.error(f"ExchangeRate-API Error: {data.get('error-type', 'Unknown error')}")
            return None
            
        if data.get("rates") and to_symbol in data["rates"]:
            rate = float(data["rates"][to_symbol])
            logger.info(f"Successfully fetched rate for {pair}: {rate}")
            return rate
            
    except Exception as e:
        logger.error(f"Error fetching from ExchangeRate-API for {pair}: {str(e)}")
        return None
    
    return None

def _get_fixer_io_rate(pair):
    """Get rate from Fixer.io with error handling."""
    from_symbol = pair[:3]
    to_symbol = pair[3:]
    url = f"http://data.fixer.io/api/latest?access_key=YOUR_FIXER_API_KEY&base={from_symbol}&symbols={to_symbol}"
    
    try:
        logger.info(f"Fetching rate for {pair} from Fixer.io")
        response = requests.get(url, timeout=API_TIMEOUT)
        data = response.json()
        
        if not data.get("success", False):
            logger.error(f"Fixer.io API Error: {data.get('error', {}).get('info', 'Unknown error')}")
            return None
            
        if data.get("rates") and to_symbol in data["rates"]:
            rate = float(data["rates"][to_symbol])
            logger.info(f"Successfully fetched rate for {pair}: {rate}")
            return rate
            
    except Exception as e:
        logger.error(f"Error fetching from Fixer.io for {pair}: {str(e)}")
        return None
    
    return None

def black_scholes_call_put(S, K, T, r, sigma, option_type="call"):
    """
    Calculate option price using Black-Scholes model with NormalDist
    """
    d1 = (math.log(S/K) + (r + sigma**2/2)*T) / (sigma*math.sqrt(T))
    d2 = d1 - sigma*math.sqrt(T)
    
    # Use NormalDist for CDF calculations
    normal = NormalDist()
    if option_type.lower() == "call":
        price = S*normal.cdf(d1) - K*math.exp(-r*T)*normal.cdf(d2)
    else:  # put
        price = K*math.exp(-r*T)*normal.cdf(-d2) - S*normal.cdf(-d1)
    
    return price

DEMO_UNLOCK_PASSWORD = 'Indiandemo2021'
DEMO_TIMEOUT_MINUTES = 1440

@app.before_request
def demo_lockout():
    allowed_routes = {'login', 'register', 'static', 'lock', 'unlock'}
    if request.endpoint in allowed_routes or request.endpoint is None:
        return
    if 'demo_start_time' not in session:
        session['demo_start_time'] = datetime.now().isoformat()
    start_time = datetime.fromisoformat(session['demo_start_time'])
    if (datetime.now() - start_time).total_seconds() > DEMO_TIMEOUT_MINUTES * 60:
        session['locked'] = True
        if request.endpoint not in {'lock', 'unlock'}:
            return redirect(url_for('lock'))
    else:
        session['locked'] = False

@app.route('/lock', methods=['GET'])
def lock():
    return render_template('lock.html')

@app.route('/unlock', methods=['POST'])
def unlock():
    password = request.form.get('password')
    if password == DEMO_UNLOCK_PASSWORD:
        session['demo_start_time'] = datetime.now().isoformat()
        session['locked'] = False
        return redirect(url_for('dashboard'))
    else:
        flash('Incorrect password. Please try again.', 'error')
        return render_template('lock.html')

@app.route('/get_demo_time')
def get_demo_time():
    demo_timeout = DEMO_TIMEOUT_MINUTES
    start_time = session.get('demo_start_time')
    if not start_time:
        # fallback: reset timer
        session['demo_start_time'] = datetime.now().isoformat()
        start_time = session['demo_start_time']
    start_time = datetime.fromisoformat(start_time)
    elapsed = (datetime.now() - start_time).total_seconds()
    remaining = max(0, int(demo_timeout * 60 - elapsed))
    minutes = remaining // 60
    seconds = remaining % 60
    time_left = f"{minutes:02d}:{seconds:02d}"
    return jsonify({'time_left': time_left})

@app.route("/register", methods=["GET", "POST"])
def register():
    logger.info("=== Registration Attempt ===")
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        logger.info(f"Registration attempt for username: {username}")
        
        try:
            if get_user_by_username(username):
                logger.warning(f"Registration failed - Username already exists: {username}")
                flash("Username already exists.", "error")
                return render_template("register.html")
            
            logger.info(f"Creating new user: {username}")
            create_user(username, password)
            logger.info(f"Successfully created user: {username}")
            flash("Registration successful. Please log in.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            logger.error(f"Error during registration: {str(e)}", exc_info=True)
            flash("An error occurred during registration. Please try again.", "error")
            return render_template("register.html")
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    logger.info("=== Login Attempt ===")
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        logger.info(f"Login attempt for username: {username}")
        
        try:
            user = verify_user(username, password)
            if user:
                logger.info(f"Successful login for user: {username}")
                session["user_id"] = user["id"]
                update_last_login(user["id"])
                next_page = request.args.get('next', url_for('dashboard'))
                logger.info(f"Redirecting to: {next_page}")
                return redirect(next_page)
            else:
                logger.warning(f"Failed login attempt for username: {username}")
                return render_template("login.html", error="Invalid credentials")
        except Exception as e:
            logger.error(f"Error during login: {str(e)}", exc_info=True)
            return render_template("login.html", error="An error occurred during login")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user_id" not in session:
        return redirect(url_for("login"))
    user = get_user_by_id(session["user_id"])
    if request.method == "POST":
        new_password = request.form["new_password"]
        if new_password:
            db = get_db()
            db.execute('UPDATE users SET password = ? WHERE id = ?', (generate_password_hash(new_password), user["id"]))
            db.commit()
            flash("Password updated successfully.", "success")
    return render_template("profile.html", user=user)

@app.route("/dashboard")
@login_required
def dashboard():
    user_id = session.get('user_id')
    
    # Get cached user data
    user = get_cached_user_data(user_id)
    if not user:
        flash('User not found', 'error')
        return redirect(url_for('login'))
    
    # Get cached signals
    signals = get_cached_signals(user_id)
    
    # Get analytics data in a single query
    db = get_db()
    analytics = db.execute('''
        SELECT 
            symbol as pair,
            direction,
            COUNT(*) as count
        FROM signals 
        WHERE user_id = ? 
        GROUP BY symbol, direction
    ''', (user_id,)).fetchall()
    
    # Process analytics data
    pair_data = {}
    direction_data = {}
    for row in analytics:
        pair = row['pair']
        direction = row['direction']
        count = row['count']
        
        if pair not in pair_data:
            pair_data[pair] = 0
        pair_data[pair] += count
        
        if direction not in direction_data:
            direction_data[direction] = 0
        direction_data[direction] += count
    
    pair_labels = list(pair_data.keys())
    pair_counts = list(pair_data.values())
    direction_labels = list(direction_data.keys())
    direction_counts = list(direction_data.values())
    
    return render_template('dashboard.html',
        user=user,
        signals=signals,
        pair_labels=pair_labels,
        pair_counts=pair_counts,
        direction_labels=direction_labels,
        direction_counts=direction_counts
    )

@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    """Render the Forex Market page as the index page."""
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in to access this page.', 'warning')
        return redirect(url_for('login'))

    current_rate = None
    selected_pair = pairs[0]
    selected_broker = brokers[0]
    payout = broker_payouts[selected_broker]
    call_price = None
    put_price = None
    volatility = 0.2
    expiry = 1/365
    risk_free_rate = 0.01
    data_source = None
    signals = session.get("signals", [])

    if request.method == "POST":
        pair = request.form["pair"]
        broker = request.form["broker"]
        signal_type = request.form["signal_type"].upper()
        start_hour = request.form["start_hour"]
        start_minute = request.form["start_minute"]
        end_hour = request.form["end_hour"]
        end_minute = request.form["end_minute"]
        start_str = f"{start_hour}:{start_minute}"
        end_str = f"{end_hour}:{end_minute}"
        selected_pair = pair
        selected_broker = broker
        payout = broker_payouts.get(broker, 0.75)
        
        try:
            current_rate = get_cached_realtime_forex(pair)
            data_source = "Alpha Vantage API"
        except Exception as e:
            logger.error(f"Error fetching forex data: {str(e)}")
            current_rate = None
            data_source = "Error fetching data"

        if current_rate:
            S = current_rate
            K = S
            T = expiry
            r = risk_free_rate
            sigma = volatility
            call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
            put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")

        try:
            start = datetime.strptime(start_str, "%H:%M")
            end = datetime.strptime(end_str, "%H:%M")
            if start >= end:
                return render_template("forex.html", 
                    error="Start time must be before end time.",
                    current_rate=current_rate,
                    selected_pair=selected_pair,
                    selected_broker=selected_broker,
                    payout=payout,
                    call_price=call_price,
                    put_price=put_price,
                    volatility=volatility,
                    expiry=expiry,
                    risk_free_rate=risk_free_rate,
                    data_source=data_source,
                    signals=signals,
                    pairs=pairs,
                    brokers=brokers)

            signals = []
            current = start
            while current < end:
                direction = random.choice(["CALL", "PUT"]) if signal_type == "BOTH" else signal_type
                signals.append({
                    "time": current.strftime("%H:%M"),
                    "pair": pair,
                    "direction": direction
                })
                save_signal(session["user_id"], current.strftime("%H:%M"), pair, direction)
                current += timedelta(minutes=random.randint(1, 15))

            session["signals"] = signals
            return render_template("forex.html", 
                signals=signals,
                current_rate=current_rate,
                selected_pair=selected_pair,
                selected_broker=selected_broker,
                payout=payout,
                call_price=call_price,
                put_price=put_price,
                volatility=volatility,
                expiry=expiry,
                risk_free_rate=risk_free_rate,
                data_source=data_source,
                pairs=pairs,
                brokers=brokers)
        except ValueError:
            return render_template("forex.html", 
                error="Invalid time format.",
                current_rate=current_rate,
                selected_pair=selected_pair,
                selected_broker=selected_broker,
                payout=payout,
                call_price=call_price,
                put_price=put_price,
                volatility=volatility,
                expiry=expiry,
                risk_free_rate=risk_free_rate,
                data_source=data_source
            )

    # For GET requests, show the rate for the default pair and broker
    try:
        current_rate = get_cached_realtime_forex(selected_pair)
        data_source = "Cached Forex API"
    except Exception as e:
        logger.error(f"Error fetching real-time forex data for {selected_pair}: {str(e)}")
        current_rate = None
        data_source = "Error fetching data"
    
    if current_rate:
        S = current_rate
        K = S
        T = expiry
        r = risk_free_rate
        sigma = volatility
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")

    signals = session.get("signals", [])
    return render_template("forex.html",
        pairs=pairs,
        brokers=brokers,
        current_rate=current_rate,
        selected_pair=selected_pair,
        selected_broker=selected_broker,
        payout=payout,
        call_price=call_price,
        put_price=put_price,
        volatility=volatility,
        expiry=expiry,
        risk_free_rate=risk_free_rate,
        signals=signals,
        data_source=data_source,
        user=get_user_by_id(user_id)
    )

@app.route("/indian_market", methods=["GET"])
@login_required
def indian_market():
    """Render the Indian Market page."""
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in to access this page.', 'warning')
        return redirect(url_for('login'))

    # Define selected broker and payout for the template
    selected_broker = brokers[0] if brokers else 'N/A'
    payout = broker_payouts.get(selected_broker, 0)

    # You would typically fetch data for the Indian market here
    # For now, we just render the template
    return render_template('indian.html', user=get_user_by_id(user_id), selected_broker=selected_broker, payout=payout)

@app.route("/otc_market", methods=["GET"])
@login_required
def otc_market():
    """Render the OTC Market page."""
    user_id = session.get('user_id')
    if not user_id:
        flash('Please log in to access this page.', 'warning')
        return redirect(url_for('login'))

    # Define selected broker and payout for the template
    selected_broker = brokers[0] if brokers else 'N/A'
    payout = broker_payouts.get(selected_broker, 0)

    # Fetch real-time prices for OTC pairs
    otc_prices = {}
    try:
        for pair in otc_pairs:
            price_data = otc_handler.get_realtime_price(pair)
            if price_data is not None:
                if isinstance(price_data, tuple):
                    otc_prices[pair] = {
                        'rate': price_data[0],
                        'source': price_data[1] if len(price_data) > 1 else 'Unknown'
                    }
                else:
                    otc_prices[pair] = {
                        'rate': price_data,
                        'source': 'Unknown'
                    }
    except Exception as e:
        logger.error(f"Error fetching OTC real-time prices: {str(e)}")
        flash('Error fetching OTC prices. Please try again.', 'error')

    return render_template('otc.html',
                         user=get_user_by_id(user_id),
                         selected_broker=selected_broker,
                         payout=payout,
                         otc_pairs=otc_pairs,
                         otc_categories=OTC_PAIR_CATEGORIES,
                         brokers=brokers,
                         broker_payouts=broker_payouts,
                         otc_prices=otc_prices
                         )

@app.route("/download_otc")
def download_otc():
    if "otc_signals" not in session:
        return redirect(url_for("otc_market"))
    signals = session["otc_signals"]
    from io import BytesIO
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, height - 40, "KishanX OTC Signals Report")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 60, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    table_data = [["Time", "Pair", "Direction", "Call Price", "Put Price"]]
    for s in signals:
        S = get_cached_realtime_forex(s["pair"].replace('_OTC',''))
        K = S
        T = 1/365
        r = 0.01
        sigma = 0.2
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call") if S else "N/A"
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put") if S else "N/A"
        table_data.append([s["time"], s["pair"], s["direction"], f"{call_price:.6f}" if S else "N/A", f"{put_price:.6f}" if S else "N/A"])
    x = 40
    y = height - 100
    row_height = 18
    col_widths = [60, 70, 70, 100, 100]
    c.setFont("Helvetica-Bold", 11)
    for col, header in enumerate(table_data[0]):
        c.drawString(x + sum(col_widths[:col]), y, header)
    c.setFont("Helvetica", 10)
    y -= row_height
    for row in table_data[1:]:
        for col, cell in enumerate(row):
            c.drawString(x + sum(col_widths[:col]), y, str(cell))
        y -= row_height
        if y < 60:
            c.showPage()
            y = height - 60
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="kishan_otc_signals.pdf")

@app.route("/download_indian")
def download_indian():
    if "indian_signals" not in session:
        return redirect(url_for("indian_market"))
    signals = session["indian_signals"]
    from io import BytesIO
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, height - 40, "KishanX Indian Signals Report")
    c.setFont("Helvetica", 10)
    c.drawString(40, height - 60, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    table_data = [["Time", "Pair", "Direction", "Call Price", "Put Price"]]
    for s in signals:
        try:
            S = get_cached_realtime_forex(s["pair"])
        except Exception:
            S = round(random.uniform(10000, 50000), 2)
        K = S
        T = 1/365
        r = 0.01
        sigma = 0.2
        call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call") if S else "N/A"
        put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put") if S else "N/A"
        table_data.append([s["time"], s["pair"], s["direction"], f"{call_price:.6f}" if S else "N/A", f"{put_price:.6f}" if S else "N/A"])
    x = 40
    y = height - 100
    row_height = 18
    col_widths = [60, 70, 70, 100, 100]
    c.setFont("Helvetica-Bold", 11)
    for col, header in enumerate(table_data[0]):
        c.drawString(x + sum(col_widths[:col]), y, header)
    c.setFont("Helvetica", 10)
    y -= row_height
    for row in table_data[1:]:
        for col, cell in enumerate(row):
            c.drawString(x + sum(col_widths[:col]), y, str(cell))
        y -= row_height
        if y < 60:
            c.showPage()
            y = height - 60
    c.save()
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name="kishan_indian_signals.pdf")

def calculate_technical_indicators(data):
    try:
        # Convert data to pandas DataFrame
        df = pd.DataFrame(data)
        
        # Calculate indicators using pandas
        df['SMA_20'] = df['close'].rolling(window=20).mean()
        df['EMA_20'] = df['close'].ewm(span=20, adjust=False).mean()
        
        # MACD
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # Bollinger Bands
        df['BB_middle'] = df['close'].rolling(window=20).mean()
        df['BB_std'] = df['close'].rolling(window=20).std()
        df['BB_upper'] = df['BB_middle'] + (df['BB_std'] * 2)
        df['BB_lower'] = df['BB_middle'] - (df['BB_std'] * 2)
        
        # CCI (Commodity Channel Index)
        tp = (df['high'] + df['low'] + df['close']) / 3
        df['CCI'] = (tp - tp.rolling(window=20).mean()) / (0.015 * tp.rolling(window=20).std())
        
        # Volume Analysis
        df['Volume_SMA'] = df['volume'].rolling(window=20).mean()
        df['Volume_Ratio'] = df['volume'] / df['Volume_SMA']
        
        return {
            'sma': df['SMA_20'].tolist(),
            'ema': df['EMA_20'].tolist(),
            'macd': df['MACD'].tolist(),
            'macd_signal': df['Signal'].tolist(),
            'rsi': df['RSI'].tolist(),
            'bollinger_upper': df['BB_upper'].tolist(),
            'bollinger_lower': df['BB_lower'].tolist(),
            'cci': df['CCI'].tolist(),
            'volume_ratio': df['Volume_Ratio'].tolist()
        }
    except Exception as e:
        print(f"Error calculating indicators: {e}")
        return None

def get_historical_data(symbol, interval='1d', period='1y'):
    try:
        if symbol.endswith(('_INDIAN', '_OTC')):
            symbol = symbol.replace('_INDIAN', '').replace('_OTC', '')

        if symbol == 'NIFTY50':
            ticker_symbol = '^NSEI'
            # Use 5d period and 5m interval for better intraday data for Indian indices
            period = '5d'
            interval = '5m'
        elif symbol == 'SENSEX':
            ticker_symbol = '^BSESN'
            # Use 5d period and 5m interval for better intraday data for Indian indices
            period = '5d'
            interval = '5m'
        else:
            # Handle other symbols if needed, maybe default to 1d, 1y or infer from symbol
            ticker_symbol = symbol # Assuming international symbols can be used directly

        logger.info(f"Fetching historical data for {symbol} ({ticker_symbol}) with period={period}, interval={interval}")
        yahoo_symbol = symbol_map.get(symbol)
        if not yahoo_symbol:
            print(f"Invalid symbol: {symbol}")
            return {
                'historical': None,
                'realtime': None,
                'error': f"Invalid symbol: {symbol}"
            }
        print(f"Fetching data for {symbol} using Yahoo symbol {yahoo_symbol}")
        # Choose period/interval based on type
        if symbol in ["NIFTY50", "BANKNIFTY", "NSEBANK", "NSEIT", "NSEINFRA", "NSEPHARMA", "NSEFMCG", "NSEMETAL", "NSEENERGY", "NSEAUTO", "NIFTYMIDCAP", "NIFTYSMALLCAP", "NIFTYNEXT50", "NIFTY100", "NIFTY500", "NIFTYREALTY", "NIFTYPVTBANK", "NIFTYPSUBANK", "NIFTYFIN", "NIFTYMEDIA"]:
            # Indices: try 5d/5m, fallback to 1mo/1d
            period = period or '5d'
            interval = interval or '5m'
        else:
            # Stocks: use 1mo/1d
            period = period or '1mo'
            interval = interval or '1d'
        ticker = yf.Ticker(yahoo_symbol)
        df = ticker.history(period=period, interval=interval)
        if df.empty and (period != '1mo' or interval != '1d'):
            # fallback for indices
            df = ticker.history(period='1mo', interval='1d')
        if df.empty:
            print(f"No data received from Yahoo Finance for {symbol}")
            return {
                'historical': None,
                'realtime': None,
                'error': f"No data available for {symbol}"
            }
        # Calculate technical indicators
        df['SMA20'] = df['Close'].rolling(window=20).mean()
        df['EMA20'] = df['Close'].ewm(span=20, adjust=False).mean()
        exp1 = df['Close'].ewm(span=12, adjust=False).mean()
        exp2 = df['Close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = exp1 - exp2
        df['Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['RSI'] = 100 - (100 / (1 + rs))
        df['BB_middle'] = df['Close'].rolling(window=20).mean()
        df['BB_std'] = df['Close'].rolling(window=20).std()
        df['BB_upper'] = df['BB_middle'] + (df['BB_std'] * 2)
        df['BB_lower'] = df['BB_middle'] - (df['BB_std'] * 2)
        df = df.replace({np.nan: None})
        dates = df.index.strftime('%Y-%m-%d %H:%M' if interval and 'm' in interval else '%Y-%m-%d').tolist()
        historical_data = {
            'dates': dates,
            'prices': {
                'open': [round(x, 2) if x is not None else None for x in df['Open'].tolist()],
                'high': [round(x, 2) if x is not None else None for x in df['High'].tolist()],
                'low': [round(x, 2) if x is not None else None for x in df['Low'].tolist()],
                'close': [round(x, 2) if x is not None else None for x in df['Close'].tolist()],
                'volume': [int(x) if x is not None else None for x in df['Volume'].tolist()]
            },
            'indicators': {
                'sma': [round(x, 2) if x is not None else None for x in df['SMA20'].tolist()],
                'ema': [round(x, 2) if x is not None else None for x in df['EMA20'].tolist()],
                'macd': [round(x, 2) if x is not None else None for x in df['MACD'].tolist()],
                'macd_signal': [round(x, 2) if x is not None else None for x in df['Signal'].tolist()],
                'rsi': [round(x, 2) if x is not None else None for x in df['RSI'].tolist()],
                'bollinger_upper': [round(x, 2) if x is not None else None for x in df['BB_upper'].tolist()],
                'bollinger_middle': [round(x, 2) if x is not None else None for x in df['BB_middle'].tolist()],
                'bollinger_lower': [round(x, 2) if x is not None else None for x in df['BB_lower'].tolist()]
            }
        }
        # Optionally, get real-time data for current values
        # realtime_data = get_indian_market_data(symbol)
        return {
            'historical': historical_data
        }
    except Exception as e:
        print(f"Error in get_historical_data for {symbol}: {str(e)}")
        return {
            'historical': None,
            'realtime': None,
            'error': str(e)
        }

@app.route("/market_data/<symbol>")
def market_data(symbol):
    """API endpoint to get market data for a symbol"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401
    
    try:
        timeframe = request.args.get('timeframe', '1mo')
        print(f"Fetching data for {symbol} with timeframe {timeframe}")  # Debug log
        
        data = get_historical_data(symbol, period=timeframe)
        print(f"Received data: {data}")  # Debug log
        
        if not data:
            return jsonify({'error': 'No data available'}), 404
            
        if data.get('error'):
            return jsonify({'error': data['error']}), 500
            
        if not data.get('historical') or not data.get('realtime'):
            return jsonify({'error': 'Incomplete data received'}), 500
            
        return jsonify(data)
        
    except Exception as e:
        print(f"Error in market_data endpoint: {str(e)}")  # Debug log
        return jsonify({'error': str(e)}), 500

def get_trading_signals(symbol: str) -> Dict:
    """Get trading signals for a symbol"""
    try:
        # Get market analysis from trading system
        analysis = trading_system.analyze_market(symbol)
        if not analysis:
            return {
                'type': 'NEUTRAL',
                'confidence': 0,
                'timestamp': datetime.now().isoformat()
            }
        
        return {
            'type': analysis['signal'],
            'confidence': round(analysis['confidence'] * 100, 2),
            'timestamp': analysis['timestamp']
        }
    except Exception as e:
        logger.error(f"Error getting trading signals for {symbol}: {str(e)}")
        return {
            'type': 'NEUTRAL',
            'confidence': 0,
            'timestamp': datetime.now().isoformat()
        }

@app.route('/market')
def market_dashboard():
    symbols = load_symbols()
    return render_template(
        'market_dashboard.html',
        subscribed_symbols=[{'symbol': s} for s in symbols],
        signals={}
    )

# Add WebSocket event handlers
@socketio.on('connect')
def handle_connect():
    """Handle WebSocket connection."""
    try:
        logger.info('Client connected')
        # Start WebSocket handler if not already running
        if not websocket_handler.is_running:
            websocket_handler.start_background()
    except Exception as e:
        logger.error(f"Error handling WebSocket connection: {e}")

@socketio.on('disconnect')
def handle_disconnect():
    """Handle WebSocket disconnection."""
    try:
        logger.info('Client disconnected')
    except Exception as e:
        logger.error(f"Error handling WebSocket disconnection: {e}")

@socketio.on('subscribe_symbol')
def handle_subscribe(data):
    if 'user_id' not in session:
        return False
    return websocket_handler.subscribe_symbol(session['user_id'], data['symbol'])

@socketio.on('unsubscribe_symbol')
def handle_unsubscribe(data):
    if 'user_id' not in session:
        return False
    return websocket_handler.unsubscribe_symbol(session['user_id'], data['symbol'])

@app.route("/api/trade", methods=["POST"])
def api_trade():
    """API endpoint for executing trades"""
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Not authenticated'}), 401
    
    data = request.get_json()
    symbol = data.get('symbol')
    trade_type = data.get('trade_type')
    quantity = data.get('quantity')
    
    if not all([symbol, trade_type, quantity]):
        return jsonify({'success': False, 'message': 'Missing required parameters'}), 400
    
    success, message = execute_trade(session['user_id'], symbol, trade_type, quantity)
    return jsonify({'success': success, 'message': message})

@app.route("/legal")
def legal():
    """Legal information page"""
    if "user_id" not in session:
        return redirect(url_for("login"))
        
    return render_template("legal.html", 
                         user=get_user_by_id(session["user_id"]))

@app.route("/subscription")
def subscription():
    """Subscription plans page"""
    # Define subscription plans
    plans = [
        {
            "name": "Basic",
            "price": "₹999",
            "period": "month",
            "features": [
                "Basic Market Analysis",
                "Daily Trading Signals",
                "Email Notifications",
                "Basic Technical Indicators"
            ],
            "id": "basic"
        },
        {
            "name": "Pro",
            "price": "₹2,499",
            "period": "month",
            "features": [
                "Advanced Market Analysis",
                "Real-time Trading Signals",
                "Priority Email Support",
                "Advanced Technical Indicators",
                "Custom Alerts",
                "Market News Updates"
            ],
            "popular": True,
            "id": "pro"
        },
        {
            "name": "Premium",
            "price": "₹4,999",
            "period": "month",
            "features": [
                "All Pro Features",
                "1-on-1 Trading Support",
                "Custom Strategy Development",
                "Portfolio Analysis",
                "Risk Management Tools",
                "VIP Market Insights"
            ],
            "id": "premium"
        }
    ]
    
    # Get user if authenticated, otherwise pass None
    user = get_user_by_id(session["user_id"]) if "user_id" in session else None
    
    return render_template("subscription.html", 
                         user=user,
                         plans=plans)

@app.route("/subscribe/<plan_id>", methods=["POST"])
def subscribe(plan_id):
    """Handle subscription requests"""
    if "user_id" not in session:
        return jsonify({"error": "Please login to subscribe"}), 401
        
    user = get_user_by_id(session["user_id"])
    if not user:
        return jsonify({"error": "User not found"}), 404
        
    # Validate plan_id
    valid_plans = ["basic", "pro", "premium"]
    if plan_id not in valid_plans:
        return jsonify({"error": "Invalid subscription plan"}), 400
        
    try:
        # Here you would typically:
        # 1. Process payment
        # 2. Update user's subscription status in database
        # 3. Send confirmation email
        
        # For now, we'll just update the session
        session['subscription'] = {
            'plan': plan_id,
            'started_at': datetime.now().isoformat()
        }
        
        return jsonify({
            "success": True,
            "message": f"Successfully subscribed to {plan_id} plan",
            "redirect": url_for("dashboard")
        })
        
    except Exception as e:
        print(f"Error processing subscription: {str(e)}")
        return jsonify({"error": "Failed to process subscription. Please try again."}), 500

# Load symbols from file
def load_symbols():
    with open('symbols.json') as f:
        return json.load(f)

ALL_SYMBOLS = load_symbols()

# Background price updater
async def background_price_updater(handler):
    while True:
        symbols = load_symbols()  # Reload in case file changes
        for symbol in symbols:
            try:
                data = await handler.get_latest_price_data(symbol)
                handler.price_cache[f'{symbol}_price'] = (datetime.now(), data)
            except Exception as e:
                logger.error(f'Error updating {symbol}: {e}')
        await asyncio.sleep(10)

# Start the background updater
def start_background_updater(handler):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(background_price_updater(handler))
    loop.run_forever()

# Start the background updater thread after websocket_handler is created
threading.Thread(target=start_background_updater, args=(websocket_handler,), daemon=True).start()

@app.route('/favicon.ico')
def favicon():
    return send_file('static/favicon.ico', mimetype='image/vnd.microsoft.icon')

# --- Add these two routes for health check and test page ---
@app.route('/')
def home():
    return "Diptiban, Home page working!"

@app.route('/test')
def test():
    return render_template('test.html')
# -----------------------------------------------------------

@app.route('/api/price/<symbol>')
def api_price(symbol):
    """Get real-time price for a symbol (supports both Forex and OTC)"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not authenticated'}), 401

    try:
        # Check if the symbol is a known Forex pair
        if symbol.upper() in pairs:
            logger.info(f"Fetching Forex data for {symbol}")
            try:
                # Use the existing forex fetching function
                price_data = get_cached_realtime_forex(symbol.upper(), return_source=True)
                if price_data is None:
                    logger.error(f"Failed to fetch real-time forex data for {symbol}")
                    return jsonify({
                        'symbol': symbol,
                        'rate': "N/A",
                        'source': 'Error',
                        'status': 'error',
                        'message': f'Failed to fetch real-time data for {symbol}',
                        'call_price': "N/A",
                        'put_price': "N/A",
                        'volatility': "N/A",
                        'expiry': "N/A",
                        'risk_free_rate': "N/A",
                        'timestamp': datetime.now().isoformat()
                    }), 200 # Return 200 OK with error status in JSON

                rate, source = price_data
                now = datetime.now().isoformat()

                # Calculate Black-Scholes prices for Forex
                S = rate
                # Use typical parameters for options (these might need adjustment based on specific requirements)
                K = S # Assuming strike price is current price for simplicity
                T = 1/365 # Assuming 1 day expiry for simplicity
                r = 0.01 # Assuming 1% risk-free rate
                sigma = 0.2 # Assuming 20% volatility (adjust as needed)

                call_price = black_scholes_call_put(S, K, T, r, sigma, option_type="call")
                put_price = black_scholes_call_put(S, K, T, r, sigma, option_type="put")
                
                # Placeholder for 24h high/low as get_cached_realtime_forex might not provide it
                high_24h = "N/A"
                low_24h = "N/A"

                return jsonify({
                    'symbol': symbol,
                    'rate': float(rate),
                    'high_24h': high_24h,
                    'low_24h': low_24h,
                    'source': source,
                    'status': 'success',
                    'call_price': round(call_price, 6),
                    'put_price': round(put_price, 6),
                    'volatility': sigma,
                    'expiry': T,
                    'risk_free_rate': r,
                    'timestamp': now
                })

            except Exception as e:
                logger.error(f"Error fetching Forex price data for {symbol}: {str(e)}")
                return jsonify({
                    'symbol': symbol,
                    'rate': "N/A",
                    'high_24h': "N/A",
                    'low_24h': "N/A",
                    'source': 'Error',
                    'status': 'error',
                    'message': str(e),
                    'call_price': "N/A",
                    'put_price': "N/A",
                    'volatility': "N/A",
                    'expiry': "N/A",
                    'risk_free_rate': "N/A",
                    'timestamp': datetime.now().isoformat()
                }), 500

        # Original logic for Indian markets using yfinance
        logger.info(f"Fetching Indian market data for {symbol}")
        timeframe = request.args.get('timeframe', '1mo')
        print(f"Fetching data for {symbol} with timeframe {timeframe}")  # Debug log

        # Use all Indian pairs, not just NIFTY50 and BANKNIFTY
        indian_symbols_list = list(symbol_map.keys()) # Get all keys from symbol_map

        if symbol in indian_symbols_list: # Check against all known Indian symbols
            import yfinance as yf
            yahoo_symbol = symbol_map.get(symbol, symbol)

            # Determine period/interval based on symbol type (Index vs Stock)
            # This logic should ideally be refined to differentiate better if needed
            # For simplicity, lets keep the existing logic for now
            if symbol in ['NIFTY50', 'BANKNIFTY', 'NSEBANK']: # Check against a few key indices
                 period = '5d'
                 interval = '5m'
            else:
                 period = '1mo' # Default for stocks
                 interval = '1d' # Default for stocks

            logger.info(f"Fetching Yahoo Finance data for {symbol} ({yahoo_symbol}) with period={period}, interval={interval}")
            ticker = yf.Ticker(yahoo_symbol)
            data = ticker.history(period=period, interval=interval)
            now = datetime.now().isoformat()

            if not data.empty:
                current_price = data['Close'].iloc[-1]
                high_220d = data['High'].max() # Using max over the fetched period as 24h might be tricky
                low_220d = data['Low'].min()   # Using min over the fetched period as 24h might be tricky

                # For Indian markets, Black-Scholes parameters might be different or not directly applicable
                # We can return N/A for these or calculate based on some assumptions if required.
                # For now, let's return N/A or some placeholder if BS is not applicable for Indian markets.
                # If needed, we can add specific BS calculations for Indian options if the data is available.
                call_price = "N/A"
                put_price = "N/A"
                volatility = "N/A"
                expiry = "N/A"
                risk_free_rate = "N/A"


                return jsonify({
                    'symbol': symbol,
                    'rate': float(current_price),
                    # Returning max/min over the fetched period, not strict 24h
                    'high_24h': float(high_220d) if high_220d is not None else "N/A",
                    'low_24h': float(low_220d) if low_220d is not None else "N/A",
                    'source': 'Yahoo Finance',
                    'status': 'success',
                    'call_price': call_price,
                    'put_price': put_price,
                    'volatility': volatility,
                    'expiry': expiry,
                    'risk_free_rate': risk_free_rate,
                    'timestamp': now
                })
            else:
                logger.error(f"No Yahoo Finance data found for {symbol} ({yahoo_symbol}) with period={period}, interval={interval}")
                return jsonify({
                    'symbol': symbol,
                    'rate': "N/A",
                    'high_24h': "N/A",
                    'low_24h': "N/A",
                    'source': 'Yahoo Finance',
                    'status': 'error',
                    'message': f'No data found for {symbol} via Yahoo Finance for the requested period/interval.',
                    'call_price': "N/A",
                    'put_price': "N/A",
                    'volatility': "N/A",
                    'expiry': "N/A",
                    'risk_free_rate': "N/A",
                    'timestamp': now
                }), 200  # Return 200 OK with error status in JSON
        else:
            # If symbol is neither a known Forex pair nor in symbol_map
            logger.warning(f"Unknown symbol requested in /api/price: {symbol}")
            return jsonify({
                'symbol': symbol,
                'rate': "N/A",
                'high_24h': "N/A",
                'low_24h': "N/A",
                'source': 'Unknown',
                'status': 'error',
                'message': f'Unknown symbol: {symbol}',
                'call_price': "N/A",
                'put_price': "N/A",
                'volatility': "N/A",
                'expiry': "N/A",
                'risk_free_rate': "N/A",
                'timestamp': datetime.now().isoformat()
            }), 404


    except Exception as e:
        logger.error(f"Error in market_data endpoint for {symbol}: {str(e)}", exc_info=True)
        return jsonify({
            'symbol': symbol,
            'rate': "N/A",
            'high_24h': "N/A",
            'low_24h': "N/A",
            'source': 'Error',
            'status': 'error',
            'message': f'Internal server error: {str(e)}',
            'call_price': "N/A",
            'put_price': "N/A",
            'volatility': "N/A",
            'expiry': "N/A",
            'risk_free_rate': "N/A",
            'timestamp': datetime.now().isoformat()
        }), 500

@app.route("/api/otc_data/<pair>")
@login_required
def get_otc_data(pair):
    """API endpoint to get OTC data for a specific pair."""
    try:
        logger.info(f"Fetching OTC data for pair: {pair}")
        
        # Validate pair format
        if not pair.endswith('_OTC'):
            logger.error(f"Invalid pair format: {pair}")
            return jsonify({
                'error': 'Invalid pair format. Pair must end with _OTC',
                'status': 'error'
            }), 400

        # Validate pair exists in configured pairs
        if pair not in OTC_PAIRS:
            logger.error(f"Pair {pair} not found in configured OTC pairs")
            return jsonify({
                'error': f'Pair {pair} is not configured',
                'status': 'error'
            }), 404

        # Get real-time price data
        logger.info(f"Requesting real-time price for {pair}")
        price_data = otc_handler.get_realtime_price(pair, return_source=True)
        
        if price_data is None:
            logger.error(f"No data available for pair: {pair}")
            return jsonify({
                'error': 'No data available for this pair',
                'status': 'error'
            }), 404

        # If price_data is a tuple, it contains (price, source)
        if isinstance(price_data, tuple):
            rate, source = price_data
            logger.info(f"Successfully fetched data for {pair}: rate={rate}, source={source}")
            return jsonify({
                'rate': rate,
                'source': source,
                'status': 'success'
            })
        else:
            logger.info(f"Successfully fetched data for {pair}: rate={price_data}")
            return jsonify({
                'rate': price_data,
                'source': 'Unknown',
                'status': 'success'
            })

    except Exception as e:
        logger.error(f"Error fetching OTC data for {pair}: {str(e)}")
        return jsonify({
            'error': f'Failed to fetch OTC data: {str(e)}',
            'status': 'error'
        }), 500

# Add caching decorator
@lru_cache(maxsize=128)
def get_cached_user_data(user_id):
    db = get_db()
    user = db.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
    return dict(user) if user else None

@lru_cache(maxsize=128)
def get_cached_signals(user_id, limit=20):
    db = get_db()
    signals = db.execute('''
        SELECT * FROM signals 
        WHERE user_id = ? 
        ORDER BY created_at DESC 
        LIMIT ?
    ''', (user_id, limit)).fetchall()
    return [dict(signal) for signal in signals]

if __name__ == "__main__":
    # For local development, you can use either:
    socketio.run(app, debug=True)  # If you need SocketIO features
    #app.run(debug=True)              # For simple Flask run
