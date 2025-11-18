import os
import sys
import json
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from aiohttp import web
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
import re

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
HYPERLIQUID_API = os.getenv('HYPERLIQUID_API', 'https://api.hyperliquid.xyz')
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')

# X (Twitter) API 設定
TWITTER_BEARER_TOKEN = os.getenv('TWITTER_BEARER_TOKEN')

# AI 翻譯 API 設定
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')

WHALES_FILE = os.path.join(os.path.dirname(__file__), 'whales.json')
TETHER_LAST_FILE = os.path.join(os.path.dirname(__file__), 'tether_last.json')
TWITTER_ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), 'twitter_accounts.json')
TWITTER_LAST_TWEETS_FILE = os.path.join(os.path.dirname(__file__), 'twitter_last_tweets.json')

TETHER_CONTRACT = '0xdAC17F958D2ee523a2206206994597C13D831ec7'
TETHER_MULTISIG = '0xC6CDE7C39eB2f0F0095F41570af89eFC2C1Ea828'
TETHER_TREASURY = '0x5754284f345afc66a98fbB0a0Afe71e0F007B949'

ETHERSCAN_API = 'https://api.etherscan.io/v2/api'

# Conversation states
WAITING_FOR_TWITTER_USERNAME, WAITING_FOR_DISPLAY_NAME = range(2)

if not TELEGRAM_TOKEN:
    raise ValueError("請在 .env 文件中設置 TELEGRAM_TOKEN")

class TranslationService:
    """翻譯服務 - 優先使用 Gemini/OpenAI,失敗則使用 Google Translate"""
    
    def __init__(self):
        try:
            self.google_translator = GoogleTranslator(source='auto', target='zh-TW')
        except Exception as e:
            print(f"⚠️ Google Translator 初始化失敗: {e}")
            self.google_translator = None
        self.gemini_failed = False
        self.openai_failed = False
    
    async def translate_with_gemini(self, text: str) -> Optional[str]:
        """使用 Gemini API 翻譯"""
        if not GEMINI_API_KEY or self.gemini_failed:
            return None
        
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"
            
            payload = {
                "contents": [{
                    "parts": [{
                        "text": f"請將以下文字翻譯成繁體中文,只需要回傳翻譯結果,不要有任何其他說明:\n\n{text}"
                    }]
                }]
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        translated = data.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', '').strip()
                        if translated:
                            print(f"✅ Gemini 翻譯成功")
                            return translated
                    elif resp.status == 429:
                        print(f"⚠️ Gemini API 額度用完")
                        self.gemini_failed = True
                    else:
                        print(f"⚠️ Gemini API 錯誤: {resp.status}")
        except Exception as e:
            print(f"❌ Gemini 翻譯錯誤: {e}")
        
        return None
    
    async def translate_with_openai(self, text: str) -> Optional[str]:
        """使用 OpenAI API 翻譯"""
        if not OPENAI_API_KEY or self.openai_failed:
            return None
        
        try:
            url = "https://api.openai.com/v1/chat/completions"
            
            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            }
            
            payload = {
                "model": "gpt-3.5-turbo",
                "messages": [
                    {"role": "system", "content": "你是一個專業的翻譯助手,請將用戶的文字翻譯成繁體中文,只需回傳翻譯結果。"},
                    {"role": "user", "content": text}
                ],
                "max_tokens": 1000,
                "temperature": 0.3
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        translated = data.get('choices', [{}])[0].get('message', {}).get('content', '').strip()
                        if translated:
                            print(f"✅ OpenAI 翻譯成功")
                            return translated
                    elif resp.status == 429:
                        print(f"⚠️ OpenAI API 額度用完")
                        self.openai_failed = True
                    else:
                        print(f"⚠️ OpenAI API 錯誤: {resp.status}")
        except Exception as e:
            print(f"❌ OpenAI 翻譯錯誤: {e}")
        
        return None
    
    async def translate_with_google(self, text: str) -> str:
        """使用 Google Translate 翻譯"""
        if not self.google_translator:
            return text
        
        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: self.google_translator.translate(text))
            print(f"✅ Google Translate 翻譯成功")
            return result
        except Exception as e:
            print(f"❌ Google Translate 翻譯錯誤: {e}")
            return text
    
    async def translate(self, text: str) -> str:
        """自動選擇最佳翻譯服務"""
        if not text or len(text) < 5:
            return text
        
        result = await self.translate_with_gemini(text)
        if result:
            return result
        
        result = await self.translate_with_openai(text)
        if result:
            return result
        
        print(f"ℹ️ 使用 Google Translate 作為後備翻譯")
        return await self.translate_with_google(text)

class TwitterMonitor:
    """Twitter/X 監控類"""
    
    def __init__(self):
        self.accounts: Dict[str, str] = self.load_accounts()
        self.last_tweets: Dict[str, str] = self.load_last_tweets()
        self.translator = TranslationService()
    
    def load_accounts(self) -> Dict[str, str]:
        if os.path.exists(TWITTER_ACCOUNTS_FILE):
            try:
                with open(TWITTER_ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"載入 Twitter 帳號失敗: {e}")
                return {}
        return {}
    
    def save_accounts(self):
        try:
            with open(TWITTER_ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.accounts, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"儲存 Twitter 帳號失敗: {e}")
    
    def load_last_tweets(self) -> Dict[str, str]:
        if os.path.exists(TWITTER_LAST_TWEETS_FILE):
            try:
                with open(TWITTER_LAST_TWEETS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"載入最後推文 ID 失敗: {e}")
                return {}
        return {}
    
    def save_last_tweets(self):
        try:
            with open(TWITTER_LAST_TWEETS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.last_tweets, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"儲存最後推文 ID 失敗: {e}")
    
    def add_account(self, username: str, display_name: str = None) -> bool:
        try:
            username = username.lstrip('@').lower().strip()
            if not display_name:
                display_name = username
            self.accounts[username] = display_name
            self.save_accounts()
            return True
        except Exception as e:
            print(f"添加帳號失敗: {e}")
            return False
    
    def remove_account(self, username: str) -> bool:
        try:
            username = username.lstrip('@').lower()
            if username in self.accounts:
                del self.accounts[username]
                if username in self.last_tweets:
                    del self.last_tweets[username]
                self.save_accounts()
                self.save_last_tweets()
                return True
            return False
        except Exception as e:
            print(f"移除帳號失敗: {e}")
            return False
    
    async def get_user_id(self, username: str) -> Optional[str]:
        if not TWITTER_BEARER_TOKEN:
            return None
        
        username = username.lstrip('@')
        
        async with aiohttp.ClientSession() as session:
            try:
                headers = {
                    'Authorization': f'Bearer {TWITTER_BEARER_TOKEN}'
                }
                
                url = f'https://api.twitter.com/2/users/by/username/{username}'
                
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get('data', {}).get('id')
                    else:
                        print(f"❌ 獲取用戶 ID 失敗: {resp.status}")
            except Exception as e:
                print(f"❌ 獲取用戶 ID 錯誤: {e}")
        
        return None
    
    async def check_new_tweets(self, username: str, max_results: int = 10) -> List[Dict]:
        """檢查新推文,可指定數量"""
        if not TWITTER_BEARER_TOKEN:
            print(f"❌ Twitter Bearer Token 未設置")
            return []
        
        username = username.lstrip('@').lower()
        user_id = await self.get_user_id(username)
        
        if not user_id:
            print(f"❌ 無法獲取用戶 ID: {username}")
            return []
        
        async with aiohttp.ClientSession() as session:
            try:
                headers = {
                    'Authorization': f'Bearer {TWITTER_BEARER_TOKEN}'
                }
                
                params = {
                    'max_results': min(max_results, 100),
                    'tweet.fields': 'created_at,text,author_id',
                    'exclude': 'retweets,replies'
                }
                
                url = f'https://api.twitter.com/2/users/{user_id}/tweets'
                
                async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        tweets = data.get('data', [])
                        
                        print(f"✅ 獲取 {len(tweets)} 條推文: @{username}")
                        return tweets
                    elif resp.status == 429:
                        print(f"⚠️ Twitter API 速率限制")
                    else:
                        error_text = await resp.text()
                        print(f"❌ Twitter API 錯誤 {resp.status}: {error_text[:200]}")
            except Exception as e:
                print(f"❌ 檢查推文錯誤: {e}")
                import traceback
                traceback.print_exc()
        
        return []
    
    async def check_new_tweets_auto(self, username: str) -> List[Dict]:
        """自動檢查新推文 (用於定時任務)"""
        if not TWITTER_BEARER_TOKEN:
            return []
        
        username = username.lstrip('@').lower()
        user_id = await self.get_user_id(username)
        
        if not user_id:
            return []
        
        async with aiohttp.ClientSession() as session:
            try:
                headers = {
                    'Authorization': f'Bearer {TWITTER_BEARER_TOKEN}'
                }
                
                params = {
                    'max_results': 10,
                    'tweet.fields': 'created_at,text,author_id',
                    'exclude': 'retweets,replies'
                }
                
                if username in self.last_tweets:
                    params['since_id'] = self.last_tweets[username]
                
                url = f'https://api.twitter.com/2/users/{user_id}/tweets'
                
                async with session.get(url, headers=headers, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        tweets = data.get('data', [])
                        
                        if tweets:
                            self.last_tweets[username] = tweets[0]['id']
                            self.save_last_tweets()
                            print(f"✅ 找到 {len(tweets)} 條新推文: @{username}")
                            return tweets
            except Exception as e:
                print(f"❌ 檢查推文錯誤: {e}")
        
        return []
    
    async def format_tweet_notification(self, username: str, tweet: Dict, show_full: bool = True) -> str:
        """格式化推文通知"""
        display_name = self.accounts.get(username, username)
        tweet_id = tweet.get('id', '')
        text = tweet.get('text', '')
        created_at = tweet.get('created_at', '')
        
        try:
            dt = datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%S.%fZ')
            dt = dt.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
            time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
        except:
            time_str = created_at
        
        if show_full:
            print(f"🔄 開始翻譯推文...")
            translated_text = await self.translator.translate(text)
            
            notification = f"""
🐦 <b>X (Twitter) 發文通知</b>

👤 <b>用戶:</b> @{username} ({display_name})
🕐 <b>時間:</b> {time_str} (台北時間)

━━━━━━━━━━━━━━━━━━━━

📝 <b>原文:</b>
{text}

━━━━━━━━━━━━━━━━━━━━

🇹🇼 <b>繁體中文翻譯:</b>
{translated_text}

━━━━━━━━━━━━━━━━━━━━

🔗 <b>查看推文:</b>
https://twitter.com/{username}/status/{tweet_id}
"""
        else:
            notification = f"""
🐦 <b>X 新推文</b> - @{username}

{text[:100]}{'...' if len(text) > 100 else ''}

🔗 https://twitter.com/{username}/status/{tweet_id}
"""
        
        return notification

class TetherMonitor:
    def __init__(self):
        self.last_block_checked = self.load_last_block()
        self.last_tx_hash = ''
    
    def load_last_block(self) -> int:
        if os.path.exists(TETHER_LAST_FILE):
            try:
                with open(TETHER_LAST_FILE, 'r') as f:
                    data = json.load(f)
                    return data.get('last_block', 0)
            except:
                return 0
        return 0
    
    def save_last_block(self, block_number: int):
        with open(TETHER_LAST_FILE, 'w') as f:
            json.dump({'last_block': block_number}, f)
    
    async def get_latest_block(self) -> Optional[int]:
        if not ETHERSCAN_API_KEY:
            return None
        
        async with aiohttp.ClientSession() as session:
            try:
                params = {
                    'chainid': '1',
                    'module': 'proxy',
                    'action': 'eth_blockNumber',
                    'apikey': ETHERSCAN_API_KEY
                }
                
                async with session.get(ETHERSCAN_API, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get('result')
                        
                        if result:
                            if isinstance(result, str):
                                if result.startswith('0x'):
                                    return int(result, 16)
                                else:
                                    try:
                                        return int(result)
                                    except:
                                        pass
            except Exception as e:
                print(f"❌ 獲取最新區塊錯誤: {e}")
        
        return None
    
    async def check_tether_mints(self) -> List[Dict]:
        if not ETHERSCAN_API_KEY:
            return []
        
        latest_block = await self.get_latest_block()
        if not latest_block:
            return []
        
        if self.last_block_checked == 0:
            self.last_block_checked = latest_block - 1000
        
        async with aiohttp.ClientSession() as session:
            try:
                params = {
                    'chainid': '1',
                    'module': 'account',
                    'action': 'tokentx',
                    'contractaddress': TETHER_CONTRACT,
                    'address': TETHER_TREASURY,
                    'startblock': self.last_block_checked,
                    'endblock': latest_block,
                    'sort': 'asc',
                    'apikey': ETHERSCAN_API_KEY
                }
                
                async with session.get(ETHERSCAN_API, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        if data.get('status') == '1' and data.get('result'):
                            result = data['result']
                            
                            mints = []
                            for tx in result:
                                from_addr = tx.get('from', '').lower()
                                to_addr = tx.get('to', '').lower()
                                
                                if (from_addr == TETHER_MULTISIG.lower() and 
                                    to_addr == TETHER_TREASURY.lower()):
                                    mints.append(tx)
                            
                            self.last_block_checked = latest_block
                            self.save_last_block(latest_block)
                            
                            return mints
                        else:
                            self.last_block_checked = latest_block
                            self.save_last_block(latest_block)
            except Exception as e:
                print(f"❌ 檢查 Tether 鑄造錯誤: {e}")
        
        return []
    
    async def get_recent_mints(self, limit: int = 10) -> List[Dict]:
        if not ETHERSCAN_API_KEY:
            return []
        
        async with aiohttp.ClientSession() as session:
            try:
                params = {
                    'chainid': '1',
                    'module': 'account',
                    'action': 'tokentx',
                    'contractaddress': TETHER_CONTRACT,
                    'address': TETHER_TREASURY,
                    'page': 1,
                    'offset': 500,
                    'sort': 'desc',
                    'apikey': ETHERSCAN_API_KEY
                }
                
                async with session.get(ETHERSCAN_API, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        if data.get('status') == '1' and data.get('result'):
                            result = data['result']
                            
                            mints = []
                            for tx in result:
                                from_addr = tx.get('from', '').lower()
                                to_addr = tx.get('to', '').lower()
                                
                                if (from_addr == TETHER_MULTISIG.lower() and 
                                    to_addr == TETHER_TREASURY.lower()):
                                    mints.append(tx)
                                    
                                    if len(mints) >= limit:
                                        break
                            
                            return mints
            except Exception as e:
                print(f"❌ 獲取最近鑄造錯誤: {e}")
        
        return []
    
    def format_mint_notification(self, tx: Dict) -> str:
        tx_hash = tx.get('hash', '')
        value = int(tx.get('value', '0'))
        usdt_amount = value / 1_000_000
        block_number = tx.get('blockNumber', '')
        timestamp = int(tx.get('timeStamp', '0'))
        
        dt = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8)))
        time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
        
        return f"""
🚨 <b>Tether (USDT) 鑄造警報!</b>

剛剛有新的 USDT 被鑄造:

🔗 <b>交易哈希:</b>
<code>{tx_hash}</code>

📤 <b>發送方:</b>
{TETHER_MULTISIG[:10]}...{TETHER_MULTISIG[-8:]}
(Tether: Multisig)

📥 <b>接收方:</b>
{TETHER_TREASURY[:10]}...{TETHER_TREASURY[-8:]}
(Tether: Treasury)

💰 <b>數量:</b>
<b>{usdt_amount:,.0f} USDT</b>

📦 <b>區塊高度:</b>
{block_number}

🕐 <b>時間:</b>
{time_str} (台北時間)

🔍 <b>查看交易:</b>
https://etherscan.io/tx/{tx_hash}
"""

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
                print(f"獲取 {address} 持倉錯誤: {e}")
        return []
    
    async def fetch_user_fills(self, address: str) -> List[Dict]:
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f'{HYPERLIQUID_API}/info',
                    json={'type': 'userFills', 'user': address},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data if isinstance(data, list) else []
            except Exception as e:
                print(f"獲取 {address} 交易歷史錯誤: {e}")
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
    
    def detect_position_changes(self, address: str, new_positions: List) -> Tuple[List[str], Dict]:
        notifications = []
        changes = {}
        
        new_pos_dict = {}
        for p in new_positions:
            coin = p['position']['coin']
            szi = float(p['position'].get('szi', '0'))
            margin = float(p['position'].get('marginUsed', '0'))
            entry_px = float(p['position'].get('entryPx', '0'))
            new_pos_dict[coin] = {
                'szi': szi,
                'margin': margin,
                'entry_px': entry_px
            }
        
        if address not in self.last_positions:
            self.last_positions[address] = new_pos_dict
            return [], {}
        
        old_pos_dict = self.last_positions[address]
        
        for coin, new_data in new_pos_dict.items():
            if coin not in old_pos_dict:
                direction = "🟢 做多" if new_data['szi'] > 0 else "🔴 做空"
                notifications.append(
                    f"🆕 <b>開倉</b>\n"
                    f"幣種: <b>{coin}</b>\n"
                    f"方向: {direction}\n"
                    f"保證金: ${new_data['margin']:,.2f} USDT\n"
                    f"開倉價: ${new_data['entry_px']:,.4f}"
                )
                changes[coin] = 'open'
        
        for coin, old_data in old_pos_dict.items():
            if coin not in new_pos_dict:
                direction = "🟢 做多" if old_data['szi'] > 0 else "🔴 做空"
                notifications.append(
                    f"🔚 <b>平倉</b>\n"
                    f"幣種: <b>{coin}</b>\n"
                    f"方向: {direction}\n"
                    f"原保證金: ${old_data['margin']:,.2f} USDT\n"
                    f"開倉價: ${old_data['entry_px']:,.4f}"
                )
                changes[coin] = 'close'
        
        for coin in set(new_pos_dict.keys()) & set(old_pos_dict.keys()):
            old_margin = old_pos_dict[coin]['margin']
            new_margin = new_pos_dict[coin]['margin']
            margin_diff = new_margin - old_margin
            
            if abs(margin_diff / old_margin) > 0.1 if old_margin > 0 else False:
                direction = "🟢 做多" if new_pos_dict[coin]['szi'] > 0 else "🔴 做空"
                
                if margin_diff > 0:
                    notifications.append(
                        f"📈 <b>加倉</b>\n"
                        f"幣種: <b>{coin}</b>\n"
                        f"方向: {direction}\n"
                        f"保證金變化: ${old_margin:,.2f} → ${new_margin:,.2f} USDT\n"
                        f"增加: ${margin_diff:,.2f} USDT"
                    )
                    changes[coin] = 'add'
                else:
                    notifications.append(
                        f"📉 <b>減倉</b>\n"
                        f"幣種: <b>{coin}</b>\n"
                        f"方向: {direction}\n"
                        f"保證金變化: ${old_margin:,.2f} → ${new_margin:,.2f} USDT\n"
                        f"減少: ${abs(margin_diff):,.2f} USDT"
                    )
                    changes[coin] = 'reduce'
        
        self.last_positions[address] = new_pos_dict
        
        return notifications, changes

tracker = WhaleTracker()
tether_monitor = TetherMonitor()
twitter_monitor = TwitterMonitor()

def get_keyboard(address: str = None) -> InlineKeyboardMarkup:
    keyboard = []
    if address:
        keyboard.append([InlineKeyboardButton("🔄 立即更新", callback_data=f"refresh:{address}")])
        keyboard.append([InlineKeyboardButton("📋 複製地址", callback_data=f"copy:{address}")])
        keyboard.append([InlineKeyboardButton("📜 查看歷史", callback_data=f"history:{address}")])
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

def get_twitter_list_keyboard(action: str) -> InlineKeyboardMarkup:
    keyboard = []
    for username, display_name in twitter_monitor.accounts.items():
        keyboard.append([InlineKeyboardButton(
            f"🐦 @{username} ({display_name})", 
            callback_data=f"{action}:{username}"
        )])
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)

async def setup_commands(application: Application):
    commands = [
        BotCommand("start", "🤖 啟動機器人"),
        BotCommand("list", "🐋 查看追蹤列表"),
        BotCommand("whalecheck", "🐋 查看特定巨鯨"),
        BotCommand("allwhale", "🐋 查看所有巨鯨持倉"),
        BotCommand("history", "📜 查看巨鯨歷史紀錄"),
        BotCommand("checktether", "💵 查看 Tether 鑄造狀態"),
        BotCommand("tetherhistory", "📋 查看 Tether 轉帳紀錄"),
        BotCommand("xlist", "🐦 查看追蹤的 X 帳號"),
        BotCommand("addx", "➕ 添加 X 帳號追蹤"),
        BotCommand("removex", "➖ 移除 X 帳號追蹤"),
        BotCommand("checkx", "🔍 查看 X 推文"),
        BotCommand("test", "🔧 測試API連接"),
    ]
    await application.bot.set_my_commands(commands)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        tracker.subscribed_chats.add(chat_id)
        
        await update.message.reply_text(
            "🤖 <b>Hyperliquid 巨鯨追蹤機器人</b>\n"
            "🧑 <b>作者:Kai0601</b>\n\n"
            "🐋 <b>巨鯨追蹤:</b>\n"
            "/list - 查看追蹤列表\n"
            "/whalecheck - 查看特定巨鯨\n"
            "/allwhale - 查看所有巨鯨持倉\n"
            "/history - 查看巨鯨歷史紀錄\n\n"
            "💵 <b>Tether 監控:</b>\n"
            "/checktether - 查看 Tether 鑄造狀態\n"
            "/tetherhistory - 查看 Tether 轉帳紀錄\n\n"
            "🐦 <b>X (Twitter) 追蹤:</b>\n"
            "/xlist - 查看追蹤的 X 帳號\n"
            "/addx - 添加 X 帳號追蹤\n"
            "/removex - 移除 X 帳號追蹤\n"
            "/checkx - 查看 X 推文\n\n"
            "🔧 <b>系統功能:</b>\n"
            "/test - 測試API連接\n\n"
            "📢 <b>自動通知:</b>\n"
            "• 巨鯨開倉/平倉/加減倉\n"
            "• Tether 鑄造事件\n"
            "• X (Twitter) 發文提醒 (每 3 分鐘)\n"
            "• 每30分鐘定時更新",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"❌ start 命令錯誤: {e}")
        import traceback
        traceback.print_exc()

async def addx_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """開始添加 X 帳號的流程"""
    try:
        await update.message.reply_text(
            "請輸入要追蹤的 X 帳號用戶名\n\n"
            "範例: <code>realDonaldTrump</code> 或 <code>@elonmusk</code>\n\n"
            "輸入 /cancel 取消操作",
            parse_mode='HTML'
        )
        return WAITING_FOR_TWITTER_USERNAME
    except Exception as e:
        print(f"❌ addx_start 錯誤: {e}")
        return ConversationHandler.END

async def addx_receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """接收用戶名"""
    try:
        username = update.message.text.strip().lstrip('@')
        
        if not username:
            await update.message.reply_text("❌ 用戶名無效,請重新輸入")
            return WAITING_FOR_TWITTER_USERNAME
        
        context.user_data['twitter_username'] = username
        
        await update.message.reply_text(
            f"✅ 用戶名: <code>@{username}</code>\n\n"
            f"請輸入顯示名稱 (可選)\n\n"
            f"範例: <code>川普</code> 或 <code>馬斯克</code>\n\n"
            f"直接按 /skip 跳過,使用用戶名作為顯示名稱",
            parse_mode='HTML'
        )
        return WAITING_FOR_DISPLAY_NAME
    except Exception as e:
        print(f"❌ addx_receive_username 錯誤: {e}")
        return ConversationHandler.END

async def addx_receive_display_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """接收顯示名稱"""
    try:
        username = context.user_data.get('twitter_username')
        display_name = update.message.text.strip()
        
        if not display_name:
            display_name = username
        
        success = twitter_monitor.add_account(username, display_name)
        
        if success:
            await update.message.reply_text(
                f"✅ 已成功添加追蹤!\n\n"
                f"🐦 用戶: @{username}\n"
                f"📝 顯示名稱: {display_name}\n\n"
                f"⚡ 系統將每 3 分鐘自動檢查新推文\n"
                f"📢 發現新推文時會立即通知您",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text("❌ 添加失敗,請稍後再試")
        
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        print(f"❌ addx_receive_display_name 錯誤: {e}")
        return ConversationHandler.END

async def addx_skip_display_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """跳過顯示名稱輸入"""
    try:
        username = context.user_data.get('twitter_username')
        
        success = twitter_monitor.add_account(username, username)
        
        if success:
            await update.message.reply_text(
                f"✅ 已成功添加追蹤!\n\n"
                f"🐦 用戶: @{username}\n"
                f"📝 顯示名稱: {username}\n\n"
                f"⚡ 系統將每 3 分鐘自動檢查新推文\n"
                f"📢 發現新推文時會立即通知您",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text("❌ 添加失敗,請稍後再試")
        
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        print(f"❌ addx_skip_display_name 錯誤: {e}")
        return ConversationHandler.END

async def addx_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消添加操作"""
    await update.message.reply_text("❌ 已取消添加 X 帳號操作")
    context.user_data.clear()
    return ConversationHandler.END

async def checkx_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """選擇要查看的 X 用戶"""
    try:
        if not twitter_monitor.accounts:
            await update.message.reply_text("📭 目前沒有追蹤任何 X 帳號\n\n使用 /addx 添加追蹤帳號")
            return
        
        if not TWITTER_BEARER_TOKEN:
            await update.message.reply_text(
                "❌ 未設置 Twitter Bearer Token\n\n"
                "請在 .env 文件中添加:\n"
                "TWITTER_BEARER_TOKEN=你的Token"
            )
            return
        
        keyboard = get_twitter_list_keyboard("checkx_user")
        await update.message.reply_text(
            "🐦 <b>選擇要查看推文的用戶:</b>\n\n"
            "點擊下方按鈕查看該用戶的最新推文",
            parse_mode='HTML',
            reply_markup=keyboard
        )
    except Exception as e:
        print(f"❌ checkx_command 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def test_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("🔍 正在測試API連接...")
        
        results = []
        results.append(f"📝 TELEGRAM_TOKEN: {'✅ 已設置' if TELEGRAM_TOKEN else '❌ 未設置'}")
        results.append(f"🌐 HYPERLIQUID_API: {'✅ 已設置' if HYPERLIQUID_API else '❌ 未設置'}")
        results.append(f"🔑 ETHERSCAN_API_KEY: {'✅ 已設置' if ETHERSCAN_API_KEY else '❌ 未設置'}")
        results.append(f"🐦 TWITTER_BEARER_TOKEN: {'✅ 已設置' if TWITTER_BEARER_TOKEN else '❌ 未設置'}")
        results.append(f"🤖 GEMINI_API_KEY: {'✅ 已設置' if GEMINI_API_KEY else '❌ 未設置'}")
        results.append(f"🤖 OPENAI_API_KEY: {'✅ 已設置' if OPENAI_API_KEY else '❌ 未設置'}")
        
        # 測試 Hyperliquid
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
        
        # 測試 Etherscan
        etherscan_test = "❌ 無法連接"
        if ETHERSCAN_API_KEY:
            try:
                block_num = await tether_monitor.get_latest_block()
                if block_num:
                    etherscan_test = f"✅ 連接成功 (區塊: {block_num:,})"
                else:
                    etherscan_test = "❌ 無法獲取區塊號"
            except Exception as e:
                etherscan_test = f"❌ {str(e)[:30]}"
        else:
            etherscan_test = "❌ 未設置 API Key"
        
        results.append(f"🔗 Etherscan API: {etherscan_test}")
        
        result_text = "📊 <b>API 測試結果:</b>\n\n" + "\n".join(results)
        
        issues = [r for r in results if '❌' in r or '⚠️' in r]
        if issues:
            result_text += "\n\n⚠️ <b>發現問題:</b>\n" + "\n".join(issues)
        else:
            result_text += "\n\n✅ 所有API運作正常!"
        
        await update.message.reply_text(result_text, parse_mode='HTML')
    except Exception as e:
        print(f"❌ test_api 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def check_tether(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("🔍 檢查 Tether 鑄造狀態...")
        
        if not ETHERSCAN_API_KEY:
            await update.message.reply_text(
                "❌ 未設置 Etherscan API Key\n\n"
                "請在 .env 文件中添加:\n"
                "ETHERSCAN_API_KEY=你的API密鑰"
            )
            return
        
        latest_block = await tether_monitor.get_latest_block()
        
        text = f"💵 <b>Tether 監控狀態</b>\n\n"
        text += f"🔧 使用 Etherscan V2 API\n"
        if latest_block:
            text += f"📦 當前區塊: {latest_block:,}\n"
        else:
            text += f"📦 當前區塊: ❌ 獲取失敗\n"
        text += f"📦 最後檢查區塊: {tether_monitor.last_block_checked:,}\n"
        text += f"✅ 監控中: Multisig → Treasury 轉帳\n\n"
        text += f"🔗 合約地址:\n"
        text += f"• USDT: <code>{TETHER_CONTRACT}</code>\n"
        text += f"• Multisig: <code>{TETHER_MULTISIG}</code>\n"
        text += f"• Treasury: <code>{TETHER_TREASURY}</code>"
        
        await update.message.reply_text(text, parse_mode='HTML')
    except Exception as e:
        print(f"❌ check_tether 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def tether_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        keyboard = [
            [
                InlineKeyboardButton("📊 近 5 筆", callback_data="tether_history:5"),
                InlineKeyboardButton("📊 近 10 筆", callback_data="tether_history:10")
            ],
            [
                InlineKeyboardButton("📊 近 15 筆", callback_data="tether_history:15"),
                InlineKeyboardButton("📊 近 20 筆", callback_data="tether_history:20")
            ],
            [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
        ]
        
        await update.message.reply_text(
            "💵 <b>Tether 轉帳紀錄查詢</b>\n\n"
            "請選擇要查詢的筆數:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        print(f"❌ tether_history_command 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def list_whales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not tracker.whales:
            await update.message.reply_text("📭 無巨鯨")
            return
        
        text = "🐋 <b>巨鯨列表:</b>\n\n"
        for i, (addr, name) in enumerate(tracker.whales.items(), 1):
            text += f"{i}. {name}\n{addr}\n\n"
        
        await update.message.reply_text(text, parse_mode='HTML')
    except Exception as e:
        print(f"❌ list_whales 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def xlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not twitter_monitor.accounts:
            await update.message.reply_text(
                "📭 目前沒有追蹤任何 X 帳號\n\n"
                "使用 /addx 添加追蹤帳號"
            )
            return
        
        text = "🐦 <b>追蹤的 X (Twitter) 帳號:</b>\n\n"
        for i, (username, display_name) in enumerate(twitter_monitor.accounts.items(), 1):
            text += f"{i}. @{username} ({display_name})\n"
            if username in twitter_monitor.last_tweets:
                text += f"   最後檢查: ✅\n"
            else:
                text += f"   最後檢查: 🆕 尚未檢查\n"
            text += "\n"
        
        text += "⚡ <b>即時監控:</b> 每 3 分鐘自動檢查\n"
        text += "📢 發現新推文會立即通知"
        
        await update.message.reply_text(text, parse_mode='HTML')
    except Exception as e:
        print(f"❌ xlist_command 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def removex_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not twitter_monitor.accounts:
            await update.message.reply_text("📭 目前沒有追蹤任何 X 帳號")
            return
        
        keyboard = get_twitter_list_keyboard("removex")
        await update.message.reply_text("請選擇要移除的 X 帳號:", reply_markup=keyboard)
    except Exception as e:
        print(f"❌ removex_command 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def show_all_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
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
    except Exception as e:
        print(f"❌ show_all_positions 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def whale_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not tracker.whales:
            await update.message.reply_text("📭 目前沒有追蹤任何巨鯨")
            return
        
        keyboard = get_whale_list_keyboard("check")
        await update.message.reply_text("請選擇要查看的巨鯨:", reply_markup=keyboard)
    except Exception as e:
        print(f"❌ whale_check 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not tracker.whales:
            await update.message.reply_text("📭 目前沒有追蹤任何巨鯨")
            return
        
        keyboard = get_whale_list_keyboard("history")
        await update.message.reply_text("請選擇要查看歷史的巨鯨:", reply_markup=keyboard)
    except Exception as e:
        print(f"❌ history_command 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    try:
        await query.answer()
        
        data = query.data
        
        if data == "cancel":
            await query.edit_message_text("❌ 已取消")
            return
        
        if data.startswith("checkx_user:"):
            username = data.split(":", 1)[1]
            display_name = twitter_monitor.accounts.get(username, username)
            
            keyboard = [
                [
                    InlineKeyboardButton("1 篇", callback_data=f"checkx_count:{username}:1"),
                    InlineKeyboardButton("3 篇", callback_data=f"checkx_count:{username}:3"),
                    InlineKeyboardButton("5 篇", callback_data=f"checkx_count:{username}:5")
                ],
                [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
            ]
            
            await query.edit_message_text(
                f"🐦 <b>@{username}</b> ({display_name})\n\n"
                f"請選擇要查看幾篇推文:",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        if data.startswith("checkx_count:"):
            parts = data.split(":")
            username = parts[1]
            count = int(parts[2])
            display_name = twitter_monitor.accounts.get(username, username)
            
            await query.edit_message_text(f"🔍 正在獲取 @{username} 的最新 {count} 篇推文...")
            
            tweets = await twitter_monitor.check_new_tweets(username, max_results=count)
            
            if tweets:
                tweets = tweets[:count]
                
                for i, tweet in enumerate(reversed(tweets), 1):
                    notification = await twitter_monitor.format_tweet_notification(username, tweet, show_full=True)
                    notification = f"📄 <b>第 {i}/{count} 篇</b>\n\n" + notification
                    await query.message.reply_text(notification, parse_mode='HTML')
                    await asyncio.sleep(2)
                
                await query.message.reply_text(f"✅ 已顯示 @{username} 的 {len(tweets)} 篇推文")
            else:
                await query.message.reply_text(f"ℹ️ @{username} 目前沒有推文")
            return
        
        if data.startswith("removex:"):
            username = data.split(":", 1)[1]
            display_name = twitter_monitor.accounts.get(username, username)
            
            if twitter_monitor.remove_account(username):
                await query.edit_message_text(
                    f"✅ 已移除追蹤\n\n"
                    f"🐦 用戶: @{username}\n"
                    f"📝 顯示名稱: {display_name}"
                )
            else:
                await query.edit_message_text("❌ 移除失敗")
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
        
        if data.startswith("history:"):
            address = data.split(":", 1)[1]
            context.user_data['history_address'] = address
            
            keyboard = [
                [
                    InlineKeyboardButton("🟢 買入紀錄", callback_data=f"history_filter:{address}:buy"),
                    InlineKeyboardButton("🔴 賣出紀錄", callback_data=f"history_filter:{address}:sell")
                ],
                [
                    InlineKeyboardButton("📊 最近10筆", callback_data=f"history_filter:{address}:10"),
                    InlineKeyboardButton("📊 最近20筆", callback_data=f"history_filter:{address}:20"),
                    InlineKeyboardButton("📊 最近30筆", callback_data=f"history_filter:{address}:30")
                ],
                [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
            ]
            
            name = tracker.whales.get(address, address[:8])
            await query.edit_message_text(
                f"📜 <b>{name}</b> 歷史查詢\n\n請選擇查詢方式:",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        if data.startswith("history_filter:"):
            parts = data.split(":")
            address = parts[1]
            filter_type = parts[2]
            
            await query.answer("📜 正在查詢歷史...")
            
            name = tracker.whales.get(address, address[:8])
            fills = await tracker.fetch_user_fills(address)
            
            if not fills:
                await query.message.reply_text(f"📭 {name} 暫無歷史紀錄")
                return
            
            if filter_type == "buy":
                filtered_fills = [f for f in fills if f.get('side') == 'B']
                title = "買入紀錄"
            elif filter_type == "sell":
                filtered_fills = [f for f in fills if f.get('side') == 'A']
                title = "賣出紀錄"
            else:
                limit = int(filter_type)
                filtered_fills = fills[:limit]
                title = f"最近 {limit} 筆"
            
            if not filtered_fills:
                await query.message.reply_text(f"📭 {name} 無符合條件的紀錄")
                return
            
            text = f"📜 <b>{name}</b> - {title}\n\n"
            
            for i, fill in enumerate(filtered_fills, 1):
                coin = fill.get('coin', 'UNKNOWN')
                side = fill.get('side', '')
                px = float(fill.get('px', 0))
                sz = float(fill.get('sz', 0))
                time = fill.get('time', 0)
                
                usdt_amount = px * sz
                
                dt = datetime.fromtimestamp(time / 1000, timezone(timedelta(hours=8)))
                time_str = dt.strftime('%m-%d %H:%M')
                
                side_emoji = "🟢" if side == "B" else "🔴"
                side_text = "買入" if side == "B" else "賣出"
                
                text += f"{i}. {side_emoji} <b>{coin}</b> {side_text}\n"
                text += f"   價格: ${px:,.4f}\n"
                text += f"   數量: ${usdt_amount:,.2f} USDT\n"
                text += f"   時間: {time_str}\n\n"
                
                if len(text) > 2550:
                    text += f"還有 {len(filtered_fills) - i} 筆紀錄,剩餘紀錄需自行查找"
                    break
            
            await query.message.reply_text(text, parse_mode='HTML')
            await query.edit_message_text("✅ 已顯示歷史紀錄")
            return
        
        if data.startswith("tether_history:"):
            limit = int(data.split(":")[1])
            await query.answer("📋 正在查詢 Tether 轉帳紀錄...")
            
            if not ETHERSCAN_API_KEY:
                await query.edit_message_text(
                    "❌ 未設置 Etherscan API Key\n\n"
                    "請在 .env 文件中添加:\n"
                    "ETHERSCAN_API_KEY=你的API密鑰"
                )
                return
            
            mints = await tether_monitor.get_recent_mints(limit)
            
            if not mints:
                await query.edit_message_text(
                    "📭 暫無 Tether 轉帳紀錄\n\n"
                    "可能的原因:\n"
                    "1. API 限制或延遲\n"
                    "2. 最近沒有鑄造活動\n"
                    "3. 請檢查控制台日誌以獲取更多信息"
                )
                return
            
            text = f"💵 <b>Tether 近 {len(mints)} 筆轉帳紀錄</b>\n\n"
            text += f"📤 從: Tether Multisig\n"
            text += f"📥 到: Tether Treasury\n\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n\n"
            
            for i, mint in enumerate(mints, 1):
                tx_hash = mint.get('hash', '')
                value = int(mint.get('value', '0'))
                usdt_amount = value / 1_000_000
                timestamp = int(mint.get('timeStamp', '0'))
                block_number = mint.get('blockNumber', '')
                
                dt = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8)))
                time_str = dt.strftime('%Y-%m-%d %H:%M')
                
                now = datetime.now(timezone(timedelta(hours=8)))
                diff = now - dt
                
                if diff.days > 0:
                    time_ago = f"{diff.days}天前"
                elif diff.seconds >= 3600:
                    hours = diff.seconds // 3600
                    time_ago = f"{hours}小時前"
                else:
                    minutes = diff.seconds // 60
                    time_ago = f"{minutes}分鐘前"
                
                text += f"<b>{i}.</b> 💰 <b>{usdt_amount:,.0f} USDT</b>\n"
                text += f"   🕐 {time_str} ({time_ago})\n"
                text += f"   📦 區塊: {block_number}\n"
                text += f"   🔗 <code>{tx_hash[:16]}...{tx_hash[-8:]}</code>\n\n"
            
            text += "━━━━━━━━━━━━━━━━━━━━\n"
            text += f"💡 點擊交易哈希可複製完整內容"
            
            await query.message.reply_text(text, parse_mode='HTML')
            await query.edit_message_text("✅ 已顯示 Tether 轉帳紀錄")
            return
        
    except Exception as e:
        print(f"❌ button_callback 錯誤: {e}")
        import traceback
        traceback.print_exc()
        try:
            await query.answer("發生錯誤,請稍後再試")
        except:
            pass

async def auto_update(context: ContextTypes.DEFAULT_TYPE):
    try:
        if not tracker.whales or not tracker.subscribed_chats:
            return
        
        taipei_time = datetime.now(timezone(timedelta(hours=8)))
        is_30min_mark = (taipei_time.minute == 0 or taipei_time.minute == 30) and taipei_time.second < 60
        
        for address, name in tracker.whales.items():
            positions = await tracker.fetch_positions(address)
            
            if not positions:
                continue
            
            notifications, changes = tracker.detect_position_changes(address, positions)
            
            if notifications:
                for notification in notifications:
                    text = f"🐋 <b>{name}</b>\n⚡ <b>即時交易通知</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)\n\n{notification}"
                    
                    for chat_id in tracker.subscribed_chats:
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=text,
                                parse_mode='HTML',
                                reply_markup=get_keyboard(address)
                            )
                        except Exception as e:
                            print(f"發送通知錯誤: {e}")
                    
                    await asyncio.sleep(1)
            
            if is_30min_mark:
                text = f"🐋 <b>{name}</b>\n🔔 定時更新\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
                
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
                        print(f"發送訊息錯誤: {e}")
                
                await asyncio.sleep(1)
    except Exception as e:
        print(f"❌ auto_update 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def tether_update(context: ContextTypes.DEFAULT_TYPE):
    try:
        if not tracker.subscribed_chats or not ETHERSCAN_API_KEY:
            return
        
        mints = await tether_monitor.check_tether_mints()
        
        if mints:
            for mint in mints:
                tx_hash = mint.get('hash', '')
                
                if tx_hash and tx_hash != tether_monitor.last_tx_hash:
                    notification = tether_monitor.format_mint_notification(mint)
                    
                    for chat_id in tracker.subscribed_chats:
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=notification,
                                parse_mode='HTML'
                            )
                        except Exception as e:
                            print(f"發送 Tether 通知錯誤: {e}")
                    
                    tether_monitor.last_tx_hash = tx_hash
                    await asyncio.sleep(2)
    except Exception as e:
        print(f"❌ Tether 更新錯誤: {e}")
        import traceback
        traceback.print_exc()

async def twitter_update(context: ContextTypes.DEFAULT_TYPE):
    """Twitter 即時更新 - 每 3 分鐘檢查一次"""
    try:
        if not tracker.subscribed_chats or not TWITTER_BEARER_TOKEN or not twitter_monitor.accounts:
            return
        
        print(f"🐦 開始檢查 Twitter 更新...")
        
        for username in twitter_monitor.accounts.keys():
            tweets = await twitter_monitor.check_new_tweets_auto(username)
            
            if tweets:
                for tweet in reversed(tweets):
                    notification = await twitter_monitor.format_tweet_notification(username, tweet, show_full=False)
                    
                    for chat_id in tracker.subscribed_chats:
                        try:
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=notification,
                                parse_mode='HTML'
                            )
                        except Exception as e:
                            print(f"發送 Twitter 通知錯誤: {e}")
                    
                    await asyncio.sleep(3)
    except Exception as e:
        print(f"❌ Twitter 更新錯誤: {e}")
        import traceback
        traceback.print_exc()

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"❌ 全局錯誤處理器: 更新 {update} 導致錯誤 {context.error}")
    import traceback
    traceback.print_exc()
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ 發生錯誤,請稍後再試或聯繫管理員"
            )
    except Exception as e:
        print(f"發送錯誤訊息失敗: {e}")

async def health_check(request):
    return web.Response(text="✅ Telegram Bot 運行中!")

async def start_health_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ HTTP health server 已啟動在 port {port}")
    
    return site

async def post_init(application: Application):
    try:
        print("📋 設置機器人命令...")
        await setup_commands(application)
        print("✅ 機器人命令設置完成")
    except Exception as e:
        print(f"❌ post_init 錯誤: {e}")
        import traceback
        traceback.print_exc()

def main():
    try:
        print("🤖 啟動中...")
        print(f"Token: {TELEGRAM_TOKEN[:10]}...")
        print(f"📡 使用 Etherscan V2 API: {ETHERSCAN_API}")
        print(f"🔧 翻譯服務: Gemini/OpenAI/Google Translate")
        print(f"⚡ Twitter 監控頻率: 每 180 秒 (3 分鐘)")
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(start_health_server())
        
        application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
        
        addx_conv_handler = ConversationHandler(
            entry_points=[CommandHandler('addx', addx_start)],
            states={
                WAITING_FOR_TWITTER_USERNAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, addx_receive_username)
                ],
                WAITING_FOR_DISPLAY_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, addx_receive_display_name),
                    CommandHandler('skip', addx_skip_display_name)
                ],
            },
            fallbacks=[CommandHandler('cancel', addx_cancel)],
        )
        
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("test", test_api))
        application.add_handler(CommandHandler("list", list_whales))
        application.add_handler(CommandHandler("whalecheck", whale_check))
        application.add_handler(CommandHandler("allwhale", show_all_positions))
        application.add_handler(CommandHandler("history", history_command))
        application.add_handler(CommandHandler("checktether", check_tether))
        application.add_handler(CommandHandler("tetherhistory", tether_history_command))
        application.add_handler(CommandHandler("xlist", xlist_command))
        application.add_handler(addx_conv_handler)
        application.add_handler(CommandHandler("removex", removex_command))
        application.add_handler(CommandHandler("checkx", checkx_command))
        application.add_handler(CallbackQueryHandler(button_callback))
        
        application.add_error_handler(error_handler)
        
        job_queue = application.job_queue
        if job_queue:
            job_queue.run_repeating(auto_update, interval=60, first=10)
            job_queue.run_repeating(tether_update, interval=300, first=30)
            job_queue.run_repeating(twitter_update, interval=180, first=60)
            print("✅ 定時任務已設置")
            print("   - 巨鯨監控: 每 60 秒")
            print("   - Tether 監控: 每 300 秒 (5 分鐘)")
            print("   - Twitter 監控: 每 180 秒 (3 分鐘)")
        else:
            print("⚠️ Job queue 未啟用")
        
        print("✅ 已啟動")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        print(f"❌ 主程式錯誤: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()