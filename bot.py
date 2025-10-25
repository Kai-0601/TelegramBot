import os
import sys
import json
import asyncio
import hmac
import hashlib
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
HYPERLIQUID_API = os.getenv('HYPERLIQUID_API', 'https://api.hyperliquid.xyz')
RAPIDAPI_KEY = os.getenv('RAPIDAPI_KEY')
RAPIDAPI_HOST = os.getenv('RAPIDAPI_HOST', 'twitter241.p.rapidapi.com')
MEXC_ACCESS_KEY = os.getenv('MEXC_ACCESS_KEY')
MEXC_SECRET_KEY = os.getenv('MEXC_SECRET_KEY')
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')
TETHER_MULTISIG = '0xC6CDE7C39eB2f0F0095F41570af89eFC2C1Ea828'
TETHER_TREASURY = '0x5754284f345afc66a98fbB0a0Afe71e0F007B949'

WHALES_FILE = os.path.join(os.path.dirname(__file__), 'whales.json')
TETHER_FILE = os.path.join(os.path.dirname(__file__), 'tether_last.json')
X_FILE = os.path.join(os.path.dirname(__file__), 'x_accounts.json')
X_LAST_FILE = os.path.join(os.path.dirname(__file__), 'x_last.json')
X_USER_IDS = os.path.join(os.path.dirname(__file__), 'x_user_ids.json')
MEXC_LAST_FILE = os.path.join(os.path.dirname(__file__), 'mexc_last.json')

if not TELEGRAM_TOKEN:
    raise ValueError("請在 .env 文件中設置 TELEGRAM_TOKEN")

ADD_ADDRESS, ADD_NAME = range(2)
BATCH_ADD_DATA = range(1)
ADD_X = range(1)

class WhaleTracker:
    def __init__(self):
        self.whales: Dict[str, str] = self.load_whales()
        self.last_positions: Dict[str, Dict] = {}
        self.subscribed_chats = set()
        
    def load_whales(self) -> Dict[str, str]:
        if os.path.exists(WHALES_FILE):
            try:
                with open(WHALES_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_whales(self):
        with open(WHALES_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.whales, f, ensure_ascii=False, indent=2)
    
    def add_whale(self, address: str, name: str = '') -> bool:
        if address not in self.whales:
            self.whales[address] = name or address[:8]
            self.save_whales()
            return True
        return False
    
    def remove_whale(self, address: str) -> bool:
        if address in self.whales:
            del self.whales[address]
            self.save_whales()
            if address in self.last_positions:
                del self.last_positions[address]
            return True
        return False
    
    async def fetch_positions(self, address: str) -> List[Dict]:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f'{HYPERLIQUID_API}/info',
                    json={'type': 'clearinghouseState', 'user': address},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('assetPositions', [])
            except Exception as e:
                print(f"Error fetching positions for {address}: {e}")
        return []
    
    def format_position(self, pos: Dict) -> str:
        position = pos.get('position', {})
        coin = position.get('coin', 'UNKNOWN')
        szi = float(position.get('szi', '0'))
        entry_px = float(position.get('entryPx', '0'))
        leverage = float(position.get('leverage', {}).get('value', '1'))
        liquidation_px = float(position.get('liquidationPx') or '0')
        
        unrealized_pnl = float(position.get('unrealizedPnl', '0'))
        position_value = abs(szi * entry_px)
        margin = position_value / leverage if leverage > 0 else position_value
        
        pnl_percent = (unrealized_pnl / margin * 100) if margin > 0 else 0
        
        direction = "🟢 做多" if szi > 0 else "🔴 做空"
        pnl_emoji = "💰" if unrealized_pnl > 0 else "💸" if unrealized_pnl < 0 else "➖"
        
        return f"""
{'═' * 30}
🪙 幣種: <b>{coin}</b>
📊 方向: {direction} | 槓桿: <b>{leverage:.1f}x</b>
📦 持倉量: ${position_value:,.2f} USDT
💵 保證金: ${margin:,.2f} USDT
📍 開倉價: ${entry_px:,.4f}
{pnl_emoji} 盈虧: ${unrealized_pnl:,.2f} USDT ({pnl_percent:+.2f}%)
⚠️ 強平價: ${liquidation_px:,.4f}
"""
    
    def positions_changed(self, address: str, new_positions: List) -> Tuple[bool, float]:
        if address not in self.last_positions:
            new_margins = {}
            for p in new_positions:
                coin = p['position']['coin']
                margin = float(p['position'].get('marginUsed', '0'))
                new_margins[coin] = margin
            self.last_positions[address] = new_margins
            return False, 0.0
        
        old_pos_dict = self.last_positions[address]
        old_total = sum(old_pos_dict.values())
        new_total = sum(float(p['position'].get('marginUsed', '0')) for p in new_positions)
        
        margin_diff = new_total - old_total
        
        if old_total > 0:
            margin_change_percent = abs(margin_diff / old_total * 100)
        else:
            margin_change_percent = 0
        
        if margin_change_percent >= 10:
            return True, margin_diff
        
        return False, 0.0

class TetherTracker:
    def __init__(self):
        self.last_tx_hash = self.load_last_tx()
        self.cached_mints = []
        self.cache_time = 0
    
    def load_last_tx(self) -> str:
        if os.path.exists(TETHER_FILE):
            try:
                with open(TETHER_FILE, 'r') as f:
                    data = json.load(f)
                    return data.get('last_tx', '')
            except:
                return ''
        return ''
    
    def save_last_tx(self, tx_hash: str):
        with open(TETHER_FILE, 'w') as f:
            json.dump({'last_tx': tx_hash}, f)
    
    async def fetch_tether_mints(self) -> List[Dict]:
        current_time = time.time()
        if self.cached_mints and (current_time - self.cache_time) < 300:
            return self.cached_mints
        
        if ETHERSCAN_API_KEY:
            mints = await self.fetch_tether_mints_etherscan()
            if mints:
                self.cached_mints = mints
                self.cache_time = current_time
                return mints
        
        mints = await self.fetch_tether_mints_blockscout()
        if mints:
            self.cached_mints = mints
            self.cache_time = current_time
        
        return mints
    
    async def fetch_tether_mints_etherscan(self) -> List[Dict]:
        async with aiohttp.ClientSession() as session:
            try:
                url = "https://api.etherscan.io/api"
                params = {
                    'module': 'account',
                    'action': 'txlist',
                    'address': TETHER_TREASURY,
                    'startblock': 0,
                    'endblock': 99999999,
                    'page': 1,
                    'offset': 100,
                    'sort': 'desc',
                    'apikey': ETHERSCAN_API_KEY
                }
                
                print(f"Fetching from Etherscan API: {TETHER_TREASURY}")
                
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        if data.get('status') == '1' and data.get('result'):
                            print(f"Etherscan: Found {len(data['result'])} transactions")
                            
                            txs = []
                            for tx in data['result']:
                                from_addr = tx.get('from', '').lower()
                                to_addr = tx.get('to', '').lower()
                                
                                if from_addr == TETHER_MULTISIG.lower() and to_addr == TETHER_TREASURY.lower():
                                    txs.append(tx)
                                    if len(txs) >= 10:
                                        break
                            
                            print(f"Found {len(txs)} Tether mints from Etherscan")
                            return txs
                        else:
                            print(f"Etherscan API error: {data.get('message', 'Unknown error')}")
            except Exception as e:
                print(f"Etherscan error: {e}")
        return []
    
    async def fetch_tether_mints_blockscout(self) -> List[Dict]:
        async with aiohttp.ClientSession() as session:
            try:
                url = f"https://eth.blockscout.com/api/v2/addresses/{TETHER_TREASURY}/transactions"
                print(f"Fetching from Blockscout: {TETHER_TREASURY}")
                
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        print(f"Blockscout: Found {len(data.get('items', []))} transactions")
                        
                        txs = []
                        for tx in data.get('items', [])[:100]:
                            from_addr = tx.get('from', {}).get('hash', '').lower()
                            to_addr = tx.get('to', {}).get('hash', '').lower()
                            
                            if from_addr == TETHER_MULTISIG.lower() and to_addr == TETHER_TREASURY.lower():
                                timestamp_str = tx.get('timestamp', '')
                                try:
                                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                                    timestamp = str(int(dt.timestamp()))
                                except:
                                    timestamp = str(int(time.time()))
                                
                                formatted_tx = {
                                    'hash': tx.get('hash', ''),
                                    'value': tx.get('value', '0'),
                                    'timeStamp': timestamp,
                                    'from': from_addr,
                                    'to': to_addr
                                }
                                txs.append(formatted_tx)
                                if len(txs) >= 10:
                                    break
                        
                        print(f"Found {len(txs)} Tether mints from Blockscout")
                        return txs
            except Exception as e:
                print(f"Blockscout error: {e}")
        return []
    
    def format_time_ago(self, timestamp: int) -> str:
        now = datetime.now(timezone.utc)
        tx_time = datetime.fromtimestamp(int(timestamp), timezone.utc)
        diff = now - tx_time
        
        days = diff.days
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60
        
        if days > 0:
            return f"{days}天{hours}小時前"
        elif hours > 0:
            return f"{hours}小時{minutes}分鐘前"
        else:
            return f"{minutes}分鐘前"

class XTracker:
    def __init__(self):
        self.accounts: Dict[str, str] = self.load_accounts()
        self.last_tweets: Dict[str, str] = self.load_last_tweets()
        self.user_ids: Dict[str, str] = self.load_user_ids()
    
    def load_accounts(self) -> Dict[str, str]:
        if os.path.exists(X_FILE):
            try:
                with open(X_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_accounts(self):
        with open(X_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.accounts, f, ensure_ascii=False, indent=2)
    
    def load_last_tweets(self) -> Dict[str, str]:
        if os.path.exists(X_LAST_FILE):
            try:
                with open(X_LAST_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_last_tweets(self):
        with open(X_LAST_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.last_tweets, f, ensure_ascii=False, indent=2)
    
    def load_user_ids(self) -> Dict[str, str]:
        if os.path.exists(X_USER_IDS):
            try:
                with open(X_USER_IDS, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_user_ids(self):
        with open(X_USER_IDS, 'w', encoding='utf-8') as f:
            json.dump(self.user_ids, f, ensure_ascii=False, indent=2)
    
    def add_account(self, username: str, user_id: str = None) -> bool:
        username = username.lstrip('@').lower()
        if username not in self.accounts:
            self.accounts[username] = username
            if user_id:
                self.user_ids[username] = user_id
            self.save_accounts()
            self.save_user_ids()
            return True
        return False
    
    def remove_account(self, username: str) -> bool:
        username = username.lstrip('@').lower()
        if username in self.accounts:
            del self.accounts[username]
            self.save_accounts()
            if username in self.last_tweets:
                del self.last_tweets[username]
                self.save_last_tweets()
            if username in self.user_ids:
                del self.user_ids[username]
                self.save_user_ids()
            return True
        return False
    
    async def get_user_id(self, username: str) -> Optional[str]:
        username = username.lstrip('@').lower()
        
        if username in self.user_ids:
            return self.user_ids[username]
        
        if not RAPIDAPI_KEY:
            print("Error: RAPIDAPI_KEY not set")
            return None
        
        async with aiohttp.ClientSession() as session:
            try:
                url = f"https://{RAPIDAPI_HOST}/user"
                querystring = {"username": username}
                headers = {
                    "x-rapidapi-key": RAPIDAPI_KEY,
                    "x-rapidapi-host": RAPIDAPI_HOST
                }
                
                print(f"Fetching user ID for @{username}")
                
                async with session.get(url, headers=headers, params=querystring, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        print(f"User API response: {data}")
                        
                        if isinstance(data, dict) and 'userId' in data:
                            user_id = str(data['userId'])
                            self.user_ids[username] = user_id
                            self.save_user_ids()
                            return user_id
                    else:
                        print(f"X API error (user): {resp.status}")
                        error_text = await resp.text()
                        print(f"Error: {error_text}")
            except Exception as e:
                print(f"Error getting user ID for {username}: {e}")
        return None
    
    async def fetch_user_tweets(self, username: str) -> List[Dict]:
        if not RAPIDAPI_KEY:
            print("Error: RAPIDAPI_KEY not set")
            return []
        
        username = username.lstrip('@').lower()
        user_id = await self.get_user_id(username)
        
        if not user_id:
            print(f"Could not get user ID for {username}")
            return []
        
        async with aiohttp.ClientSession() as session:
            try:
                url = f"https://{RAPIDAPI_HOST}/tweet-details"
                querystring = {"userId": user_id, "count": "10"}
                headers = {
                    "x-rapidapi-key": RAPIDAPI_KEY,
                    "x-rapidapi-host": RAPIDAPI_HOST
                }
                
                print(f"Fetching tweets for @{username} (ID: {user_id})")
                
                async with session.get(url, headers=headers, params=querystring, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        print(f"Tweets API response type: {type(data)}")
                        
                        tweets = []
                        if isinstance(data, list):
                            tweets = data[:10]
                        elif isinstance(data, dict) and 'tweets' in data:
                            tweets = data['tweets'][:10]
                        
                        print(f"Found {len(tweets)} tweets")
                        return tweets
                    else:
                        print(f"X API error (tweets): {resp.status}")
                        error_text = await resp.text()
                        print(f"Error response: {error_text}")
            except Exception as e:
                print(f"Error fetching tweets for {username}: {e}")
        return []
    
    def format_time_ago(self, created_at: str) -> str:
        try:
            tweet_time = datetime.strptime(created_at, '%a %b %d %H:%M:%S %z %Y')
            now = datetime.now(timezone.utc)
            diff = now - tweet_time
            
            days = diff.days
            hours = diff.seconds // 3600
            minutes = (diff.seconds % 3600) // 60
            
            if days > 0:
                return f"{days}天{hours}小時前"
            elif hours > 0:
                return f"{hours}小時{minutes}分鐘前"
            else:
                return f"{minutes}分鐘前"
        except:
            return "未知時間"

class MEXCTracker:
    def __init__(self):
        self.last_order_id = self.load_last_order()
    
    def load_last_order(self) -> str:
        if os.path.exists(MEXC_LAST_FILE):
            try:
                with open(MEXC_LAST_FILE, 'r') as f:
                    data = json.load(f)
                    return data.get('last_order', '')
            except:
                return ''
        return ''
    
    def save_last_order(self, order_id: str):
        with open(MEXC_LAST_FILE, 'w') as f:
            json.dump({'last_order': order_id}, f)
    
    def generate_signature(self, params: str) -> str:
        if not MEXC_SECRET_KEY:
            return ''
        return hmac.new(
            MEXC_SECRET_KEY.encode('utf-8'),
            params.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
    
    async def fetch_orders(self) -> List[Dict]:
        if not MEXC_ACCESS_KEY or not MEXC_SECRET_KEY:
            print("Error: MEXC keys not set")
            return []
        
        timestamp = int(time.time() * 1000)
        
        params = {
            'timestamp': timestamp,
            'recvWindow': 5000
        }
        
        query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
        
        signature = hmac.new(
            MEXC_SECRET_KEY.encode('utf-8'),
            query_string.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        
        params['signature'] = signature
        
        async with aiohttp.ClientSession() as session:
            try:
                url = "https://contract.mexc.com/api/v1/private/order/list/history"
                headers = {
                    "ApiKey": MEXC_ACCESS_KEY,
                    "Request-Time": str(timestamp),
                    "Content-Type": "application/json"
                }
                
                print(f"Fetching MEXC orders...")
                print(f"Query string: {query_string}")
                print(f"Signature: {signature[:20]}...")
                
                async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    print(f"MEXC response status: {resp.status}")
                    
                    if resp.status == 200:
                        data = await resp.json()
                        print(f"MEXC response: {data}")
                        
                        if data.get('success'):
                            orders = data.get('data', [])
                            print(f"Found {len(orders)} MEXC orders")
                            return orders
                        else:
                            print(f"MEXC API error: {data.get('message', 'Unknown error')}")
                    else:
                        error_text = await resp.text()
                        print(f"MEXC HTTP error: {resp.status}")
                        print(f"Error response: {error_text}")
            except Exception as e:
                print(f"Error fetching MEXC orders: {e}")
        return []

tracker = WhaleTracker()
tether_tracker = TetherTracker()
x_tracker = XTracker()
mexc_tracker = MEXCTracker()

def get_keyboard(address: str = None) -> InlineKeyboardMarkup:
    keyboard = []
    if address:
        keyboard.append([InlineKeyboardButton("🔄 立即更新", callback_data=f"refresh:{address}")])
        keyboard.append([InlineKeyboardButton("📋 複製地址", callback_data=f"copy:{address}")])
    else:
        keyboard.append([InlineKeyboardButton("🔄 立即更新", callback_data="refresh_all")])
    return InlineKeyboardMarkup(keyboard)

def get_whale_list_keyboard(action: str) -> InlineKeyboardMarkup:
    keyboard = []
    for address, name in tracker.whales.items():
        keyboard.append([InlineKeyboardButton(
            f"🐋 {name}", 
            callback_data=f"{action}:{address}"
        )])
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)

def get_batch_remove_keyboard() -> InlineKeyboardMarkup:
    keyboard = []
    for address, name in tracker.whales.items():
        keyboard.append([InlineKeyboardButton(
            f"☑️ {name}", 
            callback_data=f"toggle_remove:{address}"
        )])
    keyboard.append([
        InlineKeyboardButton("✅ 確認移除", callback_data="confirm_batch_remove"),
        InlineKeyboardButton("❌ 取消", callback_data="cancel")
    ])
    return InlineKeyboardMarkup(keyboard)

async def setup_commands(application: Application):
    commands = [
        BotCommand("start", "🤖 啟動機器人"),
        
        BotCommand("add", "🐋 新增巨鯨"),
        BotCommand("batchadd", "🐋 批量新增巨鯨"),
        BotCommand("remove", "🐋 移除巨鯨"),
        BotCommand("batchremove", "🐋 批量移除巨鯨"),
        BotCommand("list", "🐋 查看追蹤列表"),
        BotCommand("whalecheck", "🐋 查看特定巨鯨"),
        BotCommand("allwhale", "🐋 查看所有巨鯨持倉"),
        
        BotCommand("checktether", "💵 查看近10筆USDT鑄造"),
        
        BotCommand("addx", "🐦 新增X帳號追蹤"),
        BotCommand("removex", "🐦 移除X帳號追蹤"),
        BotCommand("listx", "🐦 查看追蹤的X帳號"),
        BotCommand("testx", "🐦 測試X帳號最新發文"),
        
        BotCommand("test", "🔧 測試API連接"),
    ]
    await application.bot.set_my_commands(commands)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tracker.subscribed_chats.add(chat_id)
    
    await update.message.reply_text(
        "🤖 <b>Hyperliquid Bot</b>\n\n"
        "🐋 <b>巨鯨追蹤:</b>\n"
        "/add - 新增巨鯨\n"
        "/batchadd - 批量新增巨鯨\n"
        "/remove - 移除巨鯨\n"
        "/batchremove - 批量移除巨鯨\n"
        "/list - 查看追蹤列表\n"
        "/whalecheck - 查看特定巨鯨\n"
        "/allwhale - 查看所有巨鯨持倉\n\n"
        "💵 <b>Tether 鑄造追蹤:</b>\n"
        "/checktether - 查看近10筆USDT鑄造\n\n"
        "🐦 <b>X 追蹤:</b>\n"
        "/addx - 新增X帳號追蹤\n"
        "/removex - 移除X帳號追蹤\n"
        "/listx - 查看追蹤的X帳號\n"
        "/testx - 測試X帳號最新發文\n\n"
        "📊 <b>MEXC 追蹤:</b>\n"
        "/checkmexc - 查看MEXC合約交易\n\n"
        "🔧 <b>系統功能:</b>\n"
        "/test - 測試API連接",
        parse_mode='HTML'
    )

async def test_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 正在測試API連接...")
    
    results = []
    results.append(f"📝 TELEGRAM_TOKEN: {'✅ 已設置' if TELEGRAM_TOKEN else '❌ 未設置'}")
    results.append(f"🌐 HYPERLIQUID_API: {'✅ 已設置' if HYPERLIQUID_API else '❌ 未設置'}")
    results.append(f"🔑 RAPIDAPI_KEY: {'✅ 已設置' if RAPIDAPI_KEY else '❌ 未設置'}")
    results.append(f"🔑 ETHERSCAN_API_KEY: {'✅ 已設置' if ETHERSCAN_API_KEY else '❌ 未設置'}")
    results.append(f"📊 MEXC_ACCESS_KEY: {'✅ 已設置' if MEXC_ACCESS_KEY else '❌ 未設置'}")
    results.append(f"📊 MEXC_SECRET_KEY: {'✅ 已設置' if MEXC_SECRET_KEY else '❌ 未設置'}")
    
    hyperliquid_test = "❌ 無法連接"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{HYPERLIQUID_API}/info',
                json={'type': 'meta'},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    hyperliquid_test = "✅ 連接成功"
    except Exception as e:
        hyperliquid_test = f"❌ 連接失敗: {str(e)[:30]}"
    
    results.append(f"🔗 Hyperliquid API: {hyperliquid_test}")
    
    etherscan_test = "❌ 無法連接"
    if ETHERSCAN_API_KEY:
        try:
            async with aiohttp.ClientSession() as session:
                url = "https://api.etherscan.io/api"
                params = {
                    'module': 'account',
                    'action': 'balance',
                    'address': TETHER_TREASURY,
                    'tag': 'latest',
                    'apikey': ETHERSCAN_API_KEY
                }
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('status') == '1':
                            etherscan_test = "✅ 連接成功"
                        elif data.get('message') == 'NOTOK':
                            etherscan_test = f"❌ API Key 無效或已過期"
                        else:
                            etherscan_test = f"❌ {data.get('result', 'Error')}"
                    else:
                        etherscan_test = f"❌ HTTP {resp.status}"
        except Exception as e:
            etherscan_test = f"❌ {str(e)[:20]}"
    else:
        etherscan_test = "❌ 未設置 API Key"
    results.append(f"🔗 Etherscan API: {etherscan_test}")
    
    rapidapi_test = "❌ 無法連接"
    if RAPIDAPI_KEY and RAPIDAPI_HOST:
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://{RAPIDAPI_HOST}/user"
                headers = {
                    "x-rapidapi-key": RAPIDAPI_KEY,
                    "x-rapidapi-host": RAPIDAPI_HOST
                }
                querystring = {"username": "elonmusk"}
                async with session.get(url, headers=headers, params=querystring, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        rapidapi_test = "✅ 連接成功"
                    elif resp.status == 404:
                        rapidapi_test = "❌ API端點不存在"
                    elif resp.status == 403:
                        rapidapi_test = "❌ API Key 無效或無權限"
                    elif resp.status == 429:
                        rapidapi_test = "❌ 請求次數超限"
                    else:
                        rapidapi_test = f"❌ HTTP {resp.status}"
        except Exception as e:
            rapidapi_test = f"❌ {str(e)[:20]}"
    else:
        rapidapi_test = "❌ 未設置 API Key 或 Host"
    results.append(f"🔗 X API: {rapidapi_test}")
    
    mexc_test = "❌ 無法連接"
    if MEXC_ACCESS_KEY and MEXC_SECRET_KEY:
        try:
            timestamp = int(time.time() * 1000)
            params = {
                'timestamp': timestamp,
                'recvWindow': 5000
            }
            query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
            signature = hmac.new(
                MEXC_SECRET_KEY.encode('utf-8'),
                query_string.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            params['signature'] = signature
            
            async with aiohttp.ClientSession() as session:
                url = "https://contract.mexc.com/api/v1/private/account/assets"
                headers = {
                    "ApiKey": MEXC_ACCESS_KEY,
                    "Request-Time": str(timestamp),
                    "Content-Type": "application/json"
                }
                async with session.get(url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('success'):
                            mexc_test = "✅ 連接成功"
                        else:
                            mexc_test = f"❌ {data.get('message', 'Error')[:30]}"
                    else:
                        error_text = await resp.text()
                        mexc_test = f"❌ HTTP {resp.status}: {error_text[:30]}"
        except Exception as e:
            mexc_test = f"❌ {str(e)[:30]}"
    else:
        mexc_test = "❌ 未設置 API Keys"
    results.append(f"🔗 MEXC API: {mexc_test}")
    
    result_text = "📊 <b>API 測試結果:</b>\n\n" + "\n".join(results)
    
    issues = [r for r in results if '❌' in r]
    if issues:
        result_text += "\n\n⚠️ <b>發現問題:</b>\n" + "\n".join(issues)
    else:
        result_text += "\n\n✅ 所有API運作正常！"
    
    await update.message.reply_text(result_text, parse_mode='HTML')

async def check_tether(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 查詢 USDT 鑄造記錄...")
    
    mints = await tether_tracker.fetch_tether_mints()
    
    if not mints:
        await update.message.reply_text("❌ 無法獲取數據\n請確認 .env 中已設置 ETHERSCAN_API_KEY")
        return
    
    text = "💵 <b>近10筆 USDT 鑄造:</b>\n\n"
    for i, tx in enumerate(mints[:10], 1):
        value_eth = int(tx['value']) / 10**18
        time_ago = tether_tracker.format_time_ago(tx['timeStamp'])
        text += f"{i}. 💰 {value_eth:,.0f} USDT\n   ⏰ {time_ago}\n\n"
    
    await update.message.reply_text(text, parse_mode='HTML')

async def check_mexc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 查詢您的 MEXC 合約交易...")
    
    if not MEXC_ACCESS_KEY or not MEXC_SECRET_KEY:
        await update.message.reply_text(
            "❌ MEXC API 未配置\n\n"
            "請在 .env 添加:\n"
            "MEXC_ACCESS_KEY=你的key\n"
            "MEXC_SECRET_KEY=你的secret"
        )
        return
    
    orders = await mexc_tracker.fetch_orders()
    
    if not orders:
        await update.message.reply_text(
            "❌ 無法獲取 MEXC 數據\n\n"
            "可能原因:\n"
            "1. API Keys 錯誤\n"
            "2. API 權限不足\n"
            "3. IP 未加入白名單\n"
            "4. 近期沒有交易記錄\n\n"
            "請檢查 MEXC 帳戶的 API 設置"
        )
        return
    
    text = "📊 <b>您的近期MEXC合約交易:</b>\n\n"
    for i, order in enumerate(orders[:10], 1):
        symbol = order.get('symbol', 'N/A')
        side = "做多" if order.get('side') == 1 else "做空"
        price = order.get('price', 0)
        vol = order.get('vol', 0)
        text += f"{i}. {symbol} {side}\n   價格: ${price} 數量: {vol}\n\n"
    
    await update.message.reply_text(text, parse_mode='HTML')

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 請輸入巨鯨地址:")
    return ADD_ADDRESS

async def add_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['whale_address'] = update.message.text.strip()
    await update.message.reply_text("📝 請輸入備註:")
    return ADD_NAME

async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    address = context.user_data.get('whale_address')
    name = update.message.text.strip()
    
    if tracker.add_whale(address, name):
        await update.message.reply_text(f"✅ 已新增: {name}")
    else:
        await update.message.reply_text("⚠️ 已存在")
    
    return ConversationHandler.END

async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ 已取消")
    return ConversationHandler.END

async def remove_whale(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tracker.whales:
        await update.message.reply_text("📭 無巨鯨")
        return
    
    keyboard = get_whale_list_keyboard("remove")
    await update.message.reply_text("選擇移除:", reply_markup=keyboard)

async def list_whales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tracker.whales:
        await update.message.reply_text("📭 無巨鯨")
        return
    
    text = "🐋 <b>巨鯨列表:</b>\n\n"
    for i, (addr, name) in enumerate(tracker.whales.items(), 1):
        text += f"{i}. {name}\n{addr}\n\n"
    
    await update.message.reply_text(text, parse_mode='HTML')

async def show_all_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tracker.whales:
        await update.message.reply_text("📭 無巨鯨")
        return
    
    taipei_time = datetime.now(timezone(timedelta(hours=8)))
    
    for address, name in tracker.whales.items():
        positions = await tracker.fetch_positions(address)
        if not positions:
            continue
        
        text = f"🐋 <b>{name}</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
        for pos in positions:
            text += tracker.format_position(pos)
        
        await update.message.reply_text(text, parse_mode='HTML', reply_markup=get_keyboard(address))
        await asyncio.sleep(1)

async def add_x_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📝 輸入X帳號 (不含@):")
    return ADD_X

async def add_x_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    username = update.message.text.strip().lstrip('@')
    await update.message.reply_text(f"🔍 驗證 @{username}...")
    
    user_id = await x_tracker.get_user_id(username)
    if not user_id:
        await update.message.reply_text(f"❌ 找不到 @{username}\n請確認 .env 中的 RAPIDAPI_KEY 設置正確")
        return ConversationHandler.END
    
    if x_tracker.add_account(username, user_id):
        await update.message.reply_text(f"✅ 已新增 @{username}")
    else:
        await update.message.reply_text(f"⚠️ 已存在")
    
    return ConversationHandler.END

async def add_x_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ 已取消")
    return ConversationHandler.END

async def list_x(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not x_tracker.accounts:
        await update.message.reply_text("📭 無X帳號")
        return
    
    text = "🐦 <b>X帳號列表:</b>\n\n"
    for i, username in enumerate(x_tracker.accounts.keys(), 1):
        text += f"{i}. @{username}\n"
    
    await update.message.reply_text(text, parse_mode='HTML')

async def remove_x(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not x_tracker.accounts:
        await update.message.reply_text("📭 目前沒有追蹤任何 X 帳號")
        return
    
    keyboard = []
    for username in x_tracker.accounts.keys():
        keyboard.append([InlineKeyboardButton(
            f"🐦 @{username}",
            callback_data=f"remove_x:{username}"
        )])
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    
    await update.message.reply_text(
        "請選擇要移除的 X 帳號:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def test_x(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not x_tracker.accounts:
        await update.message.reply_text("📭 目前沒有追蹤任何 X 帳號\n請先使用 /addx 新增帳號")
        return
    
    keyboard = []
    for username in x_tracker.accounts.keys():
        keyboard.append([InlineKeyboardButton(
            f"🐦 @{username}",
            callback_data=f"test_x:{username}"
        )])
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    
    await update.message.reply_text(
        "請選擇要測試的 X 帳號:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def whale_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tracker.whales:
        await update.message.reply_text("📭 目前沒有追蹤任何巨鯨")
        return
    
    keyboard = get_whale_list_keyboard("check")
    await update.message.reply_text("請選擇要查看的巨鯨:", reply_markup=keyboard)

async def batch_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 請輸入巨鯨資料,每行一個,格式:\n"
        "地址 備註名稱\n\n"
        "範例:\n"
        "0x123...abc 巨鯨A\n"
        "0x456...def 巨鯨B",
        parse_mode='HTML'
    )
    return BATCH_ADD_DATA

async def batch_add_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = update.message.text.strip().split('\n')
    added = 0
    
    for line in lines:
        parts = line.strip().split(None, 1)
        if len(parts) >= 1:
            address = parts[0]
            name = parts[1] if len(parts) > 1 else ''
            if tracker.add_whale(address, name):
                added += 1
    
    await update.message.reply_text(f"✅ 成功新增 {added}/{len(lines)} 個巨鯨")
    return ConversationHandler.END

async def batch_add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ 已取消批量新增")
    return ConversationHandler.END

async def batch_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tracker.whales:
        await update.message.reply_text("📭 目前沒有追蹤任何巨鯨")
        return
    
    context.user_data['remove_list'] = []
    keyboard = get_batch_remove_keyboard()
    await update.message.reply_text("請選擇要移除的巨鯨 (可多選):", reply_markup=keyboard)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "cancel":
        await query.edit_message_text("❌ 已取消")
        return
    
    if data.startswith("copy:"):
        address = data.split(":", 1)[1]
        await query.answer(f"地址: {address}", show_alert=True)
        return
    
    if data.startswith("refresh:"):
        address = data.split(":", 1)[1]
        await query.answer("🔄 更新中...")
        
        name = tracker.whales.get(address, address[:8])
        positions = await tracker.fetch_positions(address)
        
        if not positions:
            await query.message.reply_text(f"📭 {name} 無持倉")
            return
        
        taipei_time = datetime.now(timezone(timedelta(hours=8)))
        text = f"🐋 <b>{name}</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
        
        for pos in positions:
            text += tracker.format_position(pos)
        
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=get_keyboard(address))
        return
    
    if data.startswith("remove:"):
        address = data.split(":", 1)[1]
        name = tracker.whales.get(address, address[:8])
        
        if tracker.remove_whale(address):
            await query.edit_message_text(f"✅ 已移除: {name}")
        else:
            await query.edit_message_text("⚠️ 失敗")
        return
    
    if data.startswith("check:"):
        address = data.split(":", 1)[1]
        positions = await tracker.fetch_positions(address)
        
        if not positions:
            await query.edit_message_text(f"📭 該巨鯨目前沒有持倉")
            return
        
        name = tracker.whales.get(address, address[:8])
        taipei_time = datetime.now(timezone(timedelta(hours=8)))
        
        text = f"🐋 <b>{name}</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
        
        for pos in positions:
            text += tracker.format_position(pos)
        
        await query.message.reply_text(text, parse_mode='HTML', reply_markup=get_keyboard(address))
        await query.edit_message_text("✅ 已顯示巨鯨持倉")
        return
    
    if data.startswith("remove_x:"):
        username = data.split(":", 1)[1]
        
        if x_tracker.remove_account(username):
            await query.edit_message_text(f"✅ 已移除 X 帳號: @{username}")
        else:
            await query.edit_message_text("⚠️ 移除失敗")
        return
    
    if data.startswith("test_x:"):
        username = data.split(":", 1)[1]
        await query.answer("🔍 正在獲取最新發文...")
        
        tweets = await x_tracker.fetch_user_tweets(username)
        
        if not tweets:
            await query.message.reply_text(f"❌ 無法獲取 @{username} 的發文")
            await query.edit_message_text("❌ 測試失敗")
            return
        
        latest_tweet = tweets[0]
        tweet_text = latest_tweet.get('text') or latest_tweet.get('full_text') or '無內容'
        created_at = latest_tweet.get('created_at', '')
        tweet_id = latest_tweet.get('id_str') or latest_tweet.get('id') or ''
        time_ago = x_tracker.format_time_ago(created_at) if created_at else "未知時間"
        
        text = f"🐦 <b>X 測試結果</b>\n\n"
        text += f"👤 <b>@{username}</b> 的最新發文:\n\n"
        text += f"📝 {tweet_text}\n\n"
        text += f"⏰ 發布時間: {time_ago}\n"
        if tweet_id:
            text += f"🔗 https://twitter.com/{username}/status/{tweet_id}"
        
        await query.message.reply_text(text, parse_mode='HTML')
        await query.edit_message_text("✅ 測試完成")
        return
    
    if data.startswith("toggle_remove:"):
        address = data.split(":", 1)[1]
        remove_list = context.user_data.get('remove_list', [])
        
        if address in remove_list:
            remove_list.remove(address)
        else:
            remove_list.append(address)
        
        context.user_data['remove_list'] = remove_list
        
        keyboard = []
        for addr, name in tracker.whales.items():
            emoji = "✅" if addr in remove_list else "☑️"
            keyboard.append([InlineKeyboardButton(
                f"{emoji} {name}", 
                callback_data=f"toggle_remove:{addr}"
            )])
        keyboard.append([
            InlineKeyboardButton("✅ 確認移除", callback_data="confirm_batch_remove"),
            InlineKeyboardButton("❌ 取消", callback_data="cancel")
        ])
        
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
    if data == "confirm_batch_remove":
        remove_list = context.user_data.get('remove_list', [])
        
        if not remove_list:
            await query.edit_message_text("⚠️ 未選擇任何巨鯨")
            return
        
        removed = 0
        for address in remove_list:
            if tracker.remove_whale(address):
                removed += 1
        
        context.user_data['remove_list'] = []
        await query.edit_message_text(f"✅ 成功移除 {removed} 個巨鯨")
        return

async def auto_update(context: ContextTypes.DEFAULT_TYPE):
    if not tracker.whales or not tracker.subscribed_chats:
        return
    
    taipei_time = datetime.now(timezone(timedelta(hours=8)))
    is_30min_mark = (taipei_time.minute == 0 or taipei_time.minute == 30) and taipei_time.second < 60
    
    for address, name in tracker.whales.items():
        positions = await tracker.fetch_positions(address)
        
        if not positions:
            continue
        
        changed, margin_diff = tracker.positions_changed(address, positions)
        
        should_notify = False
        notification_type = ""
        
        if changed:
            should_notify = True
            notification_type = "🔔 持倉變動通知"
            
            new_margins = {}
            for p in positions:
                coin = p['position']['coin']
                margin = float(p['position'].get('marginUsed', '0'))
                new_margins[coin] = margin
            tracker.last_positions[address] = new_margins
            
        elif is_30min_mark:
            should_notify = True
            notification_type = "🔔 固定通知"
            
            new_margins = {}
            for p in positions:
                coin = p['position']['coin']
                margin = float(p['position'].get('marginUsed', '0'))
                new_margins[coin] = margin
            tracker.last_positions[address] = new_margins
        
        if should_notify:
            text = f"🐋 <b>{name}</b>\n{notification_type}\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
            
            for pos in positions:
                text += tracker.format_position(pos)
            
            for chat_id in tracker.subscribed_chats:
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode='HTML',
                        reply_markup=get_keyboard(address)
                    )
                except Exception as e:
                    print(f"Error sending message: {e}")
            
            await asyncio.sleep(1)

async def tether_update(context: ContextTypes.DEFAULT_TYPE):
    if not tracker.subscribed_chats:
        return
    
    mints = await tether_tracker.fetch_tether_mints()
    
    if not mints:
        return
    
    latest_tx = mints[0]['hash']
    
    if latest_tx != tether_tracker.last_tx_hash and tether_tracker.last_tx_hash != '':
        value_eth = int(mints[0]['value']) / 10**18
        time_ago = tether_tracker.format_time_ago(mints[0]['timeStamp'])
        
        text = f"💵 <b>Tether 鑄造通知</b>\n\n"
        text += f"💰 數量: {value_eth:,.0f} USDT\n"
        text += f"⏰ {time_ago}\n"
        text += f"🔗 {mints[0]['hash'][:16]}..."
        
        for chat_id in tracker.subscribed_chats:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
            except Exception as e:
                print(f"Error sending Tether notification: {e}")
    
    tether_tracker.last_tx_hash = latest_tx
    tether_tracker.save_last_tx(latest_tx)

async def x_update(context: ContextTypes.DEFAULT_TYPE):
    if not x_tracker.accounts or not tracker.subscribed_chats:
        return
    
    for username in x_tracker.accounts.keys():
        tweets = await x_tracker.fetch_user_tweets(username)
        
        if not tweets:
            continue
        
        latest_tweet = tweets[0]
        tweet_id = latest_tweet.get('id_str') or latest_tweet.get('id') or ''
        tweet_id = str(tweet_id)
        
        if tweet_id and tweet_id != x_tracker.last_tweets.get(username, ''):
            x_tracker.last_tweets[username] = tweet_id
            x_tracker.save_last_tweets()
            
            tweet_text = latest_tweet.get('text') or latest_tweet.get('full_text') or '無內容'
            created_at = latest_tweet.get('created_at', '')
            time_ago = x_tracker.format_time_ago(created_at) if created_at else "未知"
            
            text = f"🐦 <b>X 發文通知</b>\n\n"
            text += f"👤 @{username}\n\n"
            text += f"📝 {tweet_text}\n\n"
            text += f"⏰ {time_ago}\n"
            text += f"🔗 https://twitter.com/{username}/status/{tweet_id}"
            
            for chat_id in tracker.subscribed_chats:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
                except Exception as e:
                    print(f"Error sending X notification: {e}")
            
            await asyncio.sleep(2)

async def mexc_update(context: ContextTypes.DEFAULT_TYPE):
    if not tracker.subscribed_chats:
        return
    
    orders = await mexc_tracker.fetch_orders()
    
    if not orders:
        return
    
    latest_order = orders[0]
    order_id = str(latest_order.get('orderId', ''))
    
    if order_id and order_id != mexc_tracker.last_order_id and mexc_tracker.last_order_id != '':
        mexc_tracker.last_order_id = order_id
        mexc_tracker.save_last_order(order_id)
        
        symbol = latest_order.get('symbol', 'N/A')
        side = "做多" if latest_order.get('side') == 1 else "做空"
        price = latest_order.get('price', 0)
        vol = latest_order.get('vol', 0)
        
        text = f"📊 <b>您的 MEXC 交易通知</b>\n\n"
        text += f"🪙 {symbol}\n"
        text += f"📊 {side}\n"
        text += f"💵 價格: ${price}\n"
        text += f"📦 數量: {vol}"
        
        for chat_id in tracker.subscribed_chats:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
            except Exception as e:
                print(f"Error sending MEXC notification: {e}")
    
    if mexc_tracker.last_order_id == '':
        mexc_tracker.last_order_id = order_id
        mexc_tracker.save_last_order(order_id)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理所有錯誤"""
    print(f"Update {update} caused error {context.error}")
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ 發生錯誤,請稍後再試或聯繫管理員"
            )
    except Exception as e:
        print(f"Error sending error message: {e}")

# ==================== HTTP 健康檢查伺服器 ====================
async def health_check(request):
    """健康檢查端點 - 供 Render 檢測用"""
    return web.Response(text="✅ Telegram Bot is running!")

async def start_health_server():
    """啟動 HTTP 伺服器供 Render 檢測端口"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # Render 會自動提供 PORT 環境變數
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ HTTP health server started on port {port}")
    
    return site
# ============================================================

async def post_init(application: Application):
    print("📋 Setting up bot commands...")
    await setup_commands(application)
    print("✅ Bot commands setup complete")

def main():
    print("🤖 啟動中...")
    print(f"Token: {TELEGRAM_TOKEN[:10]}...")
    
    # 啟動 HTTP 健康檢查伺服器（用於 Render 端口檢測）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_health_server())
    
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    add_handler = ConversationHandler(
        entry_points=[CommandHandler('add', add_start)],
        states={
            ADD_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_address)],
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
        },
        fallbacks=[CommandHandler('cancel', add_cancel)],
    )
    
    batch_add_handler = ConversationHandler(
        entry_points=[CommandHandler('batchadd', batch_add_start)],
        states={
            BATCH_ADD_DATA: [MessageHandler(filters.TEXT & ~filters.COMMAND, batch_add_data)],
        },
        fallbacks=[CommandHandler('cancel', batch_add_cancel)],
    )
    
    add_x_handler = ConversationHandler(
        entry_points=[CommandHandler('addx', add_x_start)],
        states={
            ADD_X: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_x_account)],
        },
        fallbacks=[CommandHandler('cancel', add_x_cancel)],
    )
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test_api))
    application.add_handler(CommandHandler("checktether", check_tether))
    application.add_handler(CommandHandler("checkmexc", check_mexc))
    application.add_handler(add_handler)
    application.add_handler(CommandHandler("remove", remove_whale))
    application.add_handler(batch_add_handler)
    application.add_handler(CommandHandler("batchremove", batch_remove))
    application.add_handler(CommandHandler("list", list_whales))
    application.add_handler(CommandHandler("whalecheck", whale_check))
    application.add_handler(CommandHandler("allwhale", show_all_positions))
    application.add_handler(add_x_handler)
    application.add_handler(CommandHandler("removex", remove_x))
    application.add_handler(CommandHandler("listx", list_x))
    application.add_handler(CommandHandler("testx", test_x))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    application.add_error_handler(error_handler)
    
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(auto_update, interval=60, first=10)
        job_queue.run_repeating(tether_update, interval=300, first=30)
        job_queue.run_repeating(x_update, interval=120, first=20)
        job_queue.run_repeating(mexc_update, interval=180, first=40)
        print("✅ 定時任務已設置")
    else:
        print("⚠️ Job queue 未啟用")
    
    print("✅ 已啟動")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()