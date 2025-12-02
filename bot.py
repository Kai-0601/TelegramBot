import os
import sys
import json
import asyncio
import hmac
import hashlib
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, 
    CommandHandler, 
    CallbackQueryHandler, 
    ContextTypes, 
    ConversationHandler, 
    MessageHandler, 
    filters
)
from aiohttp import web
from dotenv import load_dotenv
from deep_translator import GoogleTranslator
import re

# 載入環境變數
load_dotenv()

# Telegram Bot Token
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
HYPERLIQUID_API = os.getenv('HYPERLIQUID_API', 'https://api.hyperliquid.xyz')
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')
TWITTER_BEARER_TOKEN = os.getenv('TWITTER_BEARER_TOKEN')
MEXC_API_KEY = os.getenv('MEXC_API_KEY')
MEXC_SECRET_KEY = os.getenv('MEXC_SECRET_KEY')

# 檔案路徑
WHALES_FILE = os.path.join(os.path.dirname(__file__), 'whales.json')
TETHER_LAST_FILE = os.path.join(os.path.dirname(__file__), 'tether_last.json')
TWITTER_ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), 'twitter_accounts.json')
TWITTER_LAST_TWEETS_FILE = os.path.join(os.path.dirname(__file__), 'twitter_last_tweets.json')
MEXC_TRADES_FILE = os.path.join(os.path.dirname(__file__), 'mexc_trades.json')
SUBSCRIBED_CHATS_FILE = os.path.join(os.path.dirname(__file__), 'subscribed_chats.json')  # 新增

# Tether 合約地址
TETHER_CONTRACT = '0xdAC17F958D2ee523a2206206994597C13D831ec7'
TETHER_MULTISIG = '0xC6CDE7C39eB2f0F0095F41570af89eFC2C1Ea828'
TETHER_TREASURY = '0x5754284f345afc66a98fbB0a0Afe71e0F007B949'
ETHERSCAN_API = 'https://api.etherscan.io/v2/api'

# Conversation states
WAITING_FOR_TWITTER_USERNAME, WAITING_FOR_DISPLAY_NAME = range(2)
WAITING_FOR_WHALE_ADDRESS, WAITING_FOR_WHALE_NAME = range(2, 4)

# 全局變量
last_scheduled_push_time = ""
last_mexc_positions = {}
last_mexc_push_time = ""

if not TELEGRAM_TOKEN:
    raise ValueError("請在 .env 文件中設置 TELEGRAM_TOKEN")

# ========== MEXC 倉位追蹤類別 ==========

class MEXCTracker:
    """MEXC 合約倉位追蹤類（單一帳戶）"""
    
    def __init__(self):
        self.base_url = "https://contract.mexc.com"
        self.api_key = MEXC_API_KEY
        self.secret_key = MEXC_SECRET_KEY
        self.trades_history = self.load_trades_history()
        
        if self.api_key and self.secret_key:
            print(f"✅ MEXC Tracker 初始化完成")
        else:
            print(f"⚠️ MEXC API 憑證未設置")
    
    def _generate_signature(self, timestamp: str, query_string: str = "") -> str:
        """生成 MEXC Contract API (V1) 簽名"""
        payload = f"{self.api_key}{timestamp}{query_string}"
        signature = hmac.new(
            self.secret_key.encode('utf-8'),
            payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def load_trades_history(self) -> List[Dict]:
        """載入交易歷史"""
        if os.path.exists(MEXC_TRADES_FILE):
            try:
                with open(MEXC_TRADES_FILE, 'r', encoding='utf-8') as f:
                    trades = json.load(f)
                    print(f"✅ 載入 MEXC 交易歷史: {len(trades)} 筆")
                    return trades
            except Exception as e:
                print(f"⚠️ 載入 MEXC 交易歷史失敗: {e}")
                return []
        return []
    
    def save_trades_history(self):
        """儲存交易歷史"""
        try:
            with open(MEXC_TRADES_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.trades_history, f, ensure_ascii=False, indent=2)
            print(f"✅ 儲存 MEXC 交易歷史成功")
        except Exception as e:
            print(f"❌ 儲存 MEXC 交易歷史失敗: {e}")
    
    async def fetch_positions(self) -> List[Dict]:
        """獲取 MEXC 倉位"""
        if not self.api_key or not self.secret_key:
            print("⚠️ MEXC API 憑證未設置")
            return []
        
        async with aiohttp.ClientSession() as session:
            try:
                timestamp = str(int(time.time() * 1000))
                query_string = ""
                signature = self._generate_signature(timestamp, query_string)
                
                headers = {
                    'ApiKey': self.api_key,
                    'Request-Time': timestamp,
                    'Signature': signature,
                    'Content-Type': 'application/json'
                }
                
                url = f'{self.base_url}/api/v1/private/position/open_positions'
                
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('success'):
                            positions = data.get('data', [])
                            active_positions = [p for p in positions if float(p.get('holdVol', 0)) != 0]
                            print(f"✅ 獲取 MEXC 持倉: {len(active_positions)} 個")
                            return active_positions
                        else:
                            print(f"❌ MEXC API 返回錯誤: {data.get('message', 'Unknown error')} (Code: {data.get('code')})")
                    else:
                        error_text = await resp.text()
                        print(f"❌ MEXC API 請求失敗: {resp.status} - {error_text[:200]}")
            except Exception as e:
                print(f"❌ 獲取 MEXC 持倉錯誤: {e}")
        
        return []
    
    async def fetch_deals(self, symbol: str = None, limit: int = 100) -> List[Dict]:
        """獲取 MEXC 成交歷史"""
        if not self.api_key or not self.secret_key:
            print("⚠️ MEXC API 憑證未設置")
            return []
        
        async with aiohttp.ClientSession() as session:
            try:
                timestamp = str(int(time.time() * 1000))
                
                params = {
                    'page_num': 1,
                    'page_size': limit
                }
                
                if symbol:
                    params['symbol'] = symbol
                
                query_string = '&'.join([f"{k}={v}" for k, v in sorted(params.items())])
                signature = self._generate_signature(timestamp, query_string)
                
                headers = {
                    'ApiKey': self.api_key,
                    'Request-Time': timestamp,
                    'Signature': signature,
                    'Content-Type': 'application/json'
                }
                
                url = f'{self.base_url}/api/v1/private/deal/list'
                
                async with session.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('success'):
                            deals = data.get('data', [])
                            print(f"✅ 獲取 MEXC 成交記錄: {len(deals)} 筆")
                            return deals
                        else:
                            print(f"❌ MEXC API 返回錯誤: {data.get('message', 'Unknown error')}")
                    else:
                        error_text = await resp.text()
                        print(f"❌ MEXC API 請求失敗: {resp.status} - {error_text[:200]}")
            except Exception as e:
                print(f"❌ 獲取 MEXC 成交記錄錯誤: {e}")
        
        return []
    
    def calculate_hold_duration(self, pos: Dict) -> str:
        """計算持倉時間"""
        try:
            open_time = int(pos.get('openTime', 0))
            if open_time == 0:
                return "未知"
            
            open_dt = datetime.fromtimestamp(open_time / 1000, timezone.utc)
            now_dt = datetime.now(timezone.utc)
            duration = now_dt - open_dt
            
            days = duration.days
            hours = duration.seconds // 3600
            minutes = (duration.seconds % 3600) // 60
            
            if days > 0:
                return f"{days}天 {hours}小時 {minutes}分鐘"
            elif hours > 0:
                return f"{hours}小時 {minutes}分鐘"
            else:
                return f"{minutes}分鐘"
        except:
            return "未知"
    
    def format_position(self, pos: Dict) -> str:
        """格式化 MEXC 持倉信息（統一格式）"""
        symbol = pos.get('symbol', 'UNKNOWN')
        position_type = pos.get('positionType', 1)
        hold_vol = float(pos.get('holdVol', 0))
        open_avg_price = float(pos.get('openAvgPrice', 0))
        leverage = int(pos.get('leverage', 1))
        unrealized_pnl = float(pos.get('unrealised', 0))
        liquidation_price = float(pos.get('liquidatePrice', 0))
        hold_fee = float(pos.get('holdFee', 0))
        
        position_value = hold_vol * open_avg_price
        margin = position_value / leverage if leverage > 0 else position_value
        pnl_percent = (unrealized_pnl / margin * 100) if margin > 0 else 0
        hold_duration = self.calculate_hold_duration(pos)
        
        direction = "🟢 做多" if position_type == 1 else "🔴 做空"
        pnl_emoji = "💰" if unrealized_pnl > 0 else "💸" if unrealized_pnl < 0 else "➖"
        
        return f"""
{'═' * 30}
🪙 幣種: <b>{symbol}</b>
📊 方向: {direction} | 槓桿: <b>{leverage}x</b>
📦 持倉量: ${position_value:,.2f} USDT
💵 保證金: ${margin:,.2f} USDT
📍 開倉價: ${open_avg_price:.4f}
{pnl_emoji} 盈虧: ${unrealized_pnl:,.2f} USDT ({pnl_percent:+.2f}%)
💳 持倉手續費: ${hold_fee:.2f} USDT
⏱️ 持倉時間: {hold_duration}
⚠️ 強平價: ${liquidation_price:.4f}
"""
    
    def record_trade(self, trade_info: Dict):
        """記錄交易"""
        trade_info['timestamp'] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        self.trades_history.append(trade_info)
        self.save_trades_history()
        print(f"✅ 記錄交易: {trade_info.get('symbol')} {trade_info.get('action')}")
    
    def calculate_statistics(self, days: int = None) -> Dict:
        """計算統計數據"""
        if not self.trades_history:
            return {
                'total_trades': 0,
                'win_trades': 0,
                'lose_trades': 0,
                'win_rate': 0,
                'total_pnl': 0,
                'total_profit': 0,
                'total_loss': 0
            }
        
        now = datetime.now(timezone(timedelta(hours=8)))
        
        if days:
            cutoff = now - timedelta(days=days)
            trades = [t for t in self.trades_history 
                     if datetime.fromisoformat(t.get('timestamp', '')) >= cutoff]
        else:
            trades = self.trades_history
        
        total_trades = len(trades)
        win_trades = len([t for t in trades if t.get('pnl', 0) > 0])
        lose_trades = len([t for t in trades if t.get('pnl', 0) < 0])
        win_rate = (win_trades / total_trades * 100) if total_trades > 0 else 0
        
        total_pnl = sum([t.get('pnl', 0) for t in trades])
        total_profit = sum([t.get('pnl', 0) for t in trades if t.get('pnl', 0) > 0])
        total_loss = sum([t.get('pnl', 0) for t in trades if t.get('pnl', 0) < 0])
        
        return {
            'total_trades': total_trades,
            'win_trades': win_trades,
            'lose_trades': lose_trades,
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'total_profit': total_profit,
            'total_loss': total_loss
        }
    
    def format_statistics(self, stats: Dict, period: str = "全部") -> str:
        """格式化統計信息"""
        pnl_emoji = "💰" if stats['total_pnl'] > 0 else "💸" if stats['total_pnl'] < 0 else "➖"
        
        return f"""
📊 <b>MEXC 交易統計 ({period})</b>

{'═' * 30}

📈 <b>交易次數統計:</b>
總交易次數: {stats['total_trades']} 筆
✅ 盈利次數: {stats['win_trades']} 筆
❌ 虧損次數: {stats['lose_trades']} 筆
🎯 勝率: <b>{stats['win_rate']:.2f}%</b>

{'═' * 30}

{pnl_emoji} <b>盈虧統計:</b>
總盈虧: <b>${stats['total_pnl']:,.2f} USDT</b>
💰 總盈利: ${stats['total_profit']:,.2f} USDT
💸 總虧損: ${stats['total_loss']:,.2f} USDT

{'═' * 30}
"""
    
    def format_trade_history(self, trades: List[Dict], limit: int = 20) -> str:
        """格式化交易歷史"""
        if not trades:
            return "📭 沒有交易記錄"
        
        recent_trades = trades[-limit:]
        text = f"📜 <b>MEXC 交易歷史 (最近 {len(recent_trades)} 筆)</b>\n\n"
        
        for trade in reversed(recent_trades):
            symbol = trade.get('symbol', 'UNKNOWN')
            action = trade.get('action', '')
            pnl = trade.get('pnl', 0)
            timestamp = trade.get('timestamp', '')
            
            try:
                dt = datetime.fromisoformat(timestamp)
                time_str = dt.strftime('%m-%d %H:%M')
            except:
                time_str = timestamp
            
            action_emoji = "🆕" if action == 'open' else "🔚" if action == 'close' else "📈" if action == 'add' else "📉"
            pnl_emoji = "💰" if pnl > 0 else "💸" if pnl < 0 else "➖"
            
            text += f"{action_emoji} {symbol} | {action}\n"
            if pnl != 0:
                text += f"   {pnl_emoji} 盈虧: ${pnl:,.2f} USDT\n"
            text += f"   🕐 {time_str}\n\n"
        
        return text
    
    def detect_position_changes(self, old_positions: Dict, new_positions: List[Dict]) -> Tuple[List[str], Dict]:
        """檢測 MEXC 倉位變化"""
        notifications = []
        changes = {}
        
        new_pos_dict = {}
        for p in new_positions:
            symbol = p.get('symbol', '')
            position_type = p.get('positionType', 1)
            hold_vol = float(p.get('holdVol', 0))
            open_avg_price = float(p.get('openAvgPrice', 0))
            
            key = f"{symbol}_{position_type}"
            new_pos_dict[key] = {
                'hold_vol': hold_vol,
                'open_avg_price': open_avg_price,
                'position_type': position_type
            }
        
        for key, new_data in new_pos_dict.items():
            if key not in old_positions:
                symbol = key.rsplit('_', 1)[0]
                direction = "🟢 做多" if new_data['position_type'] == 1 else "🔴 做空"
                notifications.append(
                    f"🆕 <b>開倉</b>\n"
                    f"幣種: <b>{symbol}</b>\n"
                    f"方向: {direction}\n"
                    f"持倉量: {new_data['hold_vol']:.4f}\n"
                    f"開倉價: ${new_data['open_avg_price']:.4f}"
                )
                changes[key] = 'open'
                
                self.record_trade({
                    'symbol': symbol,
                    'action': 'open',
                    'direction': 'long' if new_data['position_type'] == 1 else 'short',
                    'volume': new_data['hold_vol'],
                    'price': new_data['open_avg_price'],
                    'pnl': 0
                })
                
                print(f"📊 檢測到 MEXC 開倉: {symbol} {direction}")
        
        for key, old_data in old_positions.items():
            if key not in new_pos_dict:
                symbol = key.rsplit('_', 1)[0]
                direction = "🟢 做多" if old_data['position_type'] == 1 else "🔴 做空"
                notifications.append(
                    f"🔚 <b>平倉</b>\n"
                    f"幣種: <b>{symbol}</b>\n"
                    f"方向: {direction}\n"
                    f"原持倉量: {old_data['hold_vol']:.4f}\n"
                    f"開倉價: ${old_data['open_avg_price']:.4f}"
                )
                changes[key] = 'close'
                
                self.record_trade({
                    'symbol': symbol,
                    'action': 'close',
                    'direction': 'long' if old_data['position_type'] == 1 else 'short',
                    'volume': old_data['hold_vol'],
                    'price': old_data['open_avg_price'],
                    'pnl': 0
                })
                
                print(f"📊 檢測到 MEXC 平倉: {symbol} {direction}")
        
        for key in set(new_pos_dict.keys()) & set(old_positions.keys()):
            old_vol = old_positions[key]['hold_vol']
            new_vol = new_pos_dict[key]['hold_vol']
            vol_diff = new_vol - old_vol
            
            if abs(vol_diff / old_vol) > 0.1 if old_vol > 0 else False:
                symbol = key.rsplit('_', 1)[0]
                direction = "🟢 做多" if new_pos_dict[key]['position_type'] == 1 else "🔴 做空"
                
                if vol_diff > 0:
                    notifications.append(
                        f"📈 <b>加倉</b>\n"
                        f"幣種: <b>{symbol}</b>\n"
                        f"方向: {direction}\n"
                        f"持倉變化: {old_vol:.4f} → {new_vol:.4f}\n"
                        f"增加: {vol_diff:.4f}"
                    )
                    changes[key] = 'add'
                    
                    self.record_trade({
                        'symbol': symbol,
                        'action': 'add',
                        'direction': 'long' if new_pos_dict[key]['position_type'] == 1 else 'short',
                        'volume': vol_diff,
                        'price': new_pos_dict[key]['open_avg_price'],
                        'pnl': 0
                    })
                    
                    print(f"📊 檢測到 MEXC 加倉: {symbol} {direction}")
                else:
                    notifications.append(
                        f"📉 <b>減倉</b>\n"
                        f"幣種: <b>{symbol}</b>\n"
                        f"方向: {direction}\n"
                        f"持倉變化: {old_vol:.4f} → {new_vol:.4f}\n"
                        f"減少: {abs(vol_diff):.4f}"
                    )
                    changes[key] = 'reduce'
                    
                    self.record_trade({
                        'symbol': symbol,
                        'action': 'reduce',
                        'direction': 'long' if new_pos_dict[key]['position_type'] == 1 else 'short',
                        'volume': abs(vol_diff),
                        'price': new_pos_dict[key]['open_avg_price'],
                        'pnl': 0
                    })
                    
                    print(f"📊 檢測到 MEXC 減倉: {symbol} {direction}")
        
        return notifications, new_pos_dict

# ========== 翻譯服務 ==========

class TranslationService:
    """翻譯服務"""
    
    def __init__(self):
        try:
            self.google_translator = GoogleTranslator(source='auto', target='zh-TW')
            print("✅ Google Translator 初始化成功")
        except Exception as e:
            print(f"⚠️ Google Translator 初始化失敗: {e}")
            self.google_translator = None
    
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
        """翻譯文字"""
        if not text or len(text) < 5:
            return text
        
        return await self.translate_with_google(text)

# ========== Twitter 監控 ==========

class TwitterMonitor:
    """Twitter/X 監控類"""
    
    def __init__(self):
        self.accounts: Dict[str, str] = self.load_accounts()
        self.last_tweets: Dict[str, str] = self.load_last_tweets()
        self.translator = TranslationService()
        print(f"✅ Twitter Monitor 初始化完成，追蹤 {len(self.accounts)} 個帳號")
    
    def load_accounts(self) -> Dict[str, str]:
        """載入追蹤帳號列表"""
        if os.path.exists(TWITTER_ACCOUNTS_FILE):
            try:
                with open(TWITTER_ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                    accounts = json.load(f)
                    print(f"✅ 載入 Twitter 帳號: {len(accounts)} 個")
                    return accounts
            except Exception as e:
                print(f"⚠️ 載入 Twitter 帳號失敗: {e}")
                return {}
        return {}
    
    def save_accounts(self):
        """儲存追蹤帳號列表"""
        try:
            with open(TWITTER_ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.accounts, f, ensure_ascii=False, indent=2)
            print(f"✅ 儲存 Twitter 帳號成功")
        except Exception as e:
            print(f"❌ 儲存 Twitter 帳號失敗: {e}")
    
    def load_last_tweets(self) -> Dict[str, str]:
        """載入最後推文 ID 記錄"""
        if os.path.exists(TWITTER_LAST_TWEETS_FILE):
            try:
                with open(TWITTER_LAST_TWEETS_FILE, 'r', encoding='utf-8') as f:
                    last_tweets = json.load(f)
                    print(f"✅ 載入最後推文 ID: {len(last_tweets)} 個")
                    return last_tweets
            except Exception as e:
                print(f"⚠️ 載入最後推文 ID 失敗: {e}")
                return {}
        return {}
    
    def save_last_tweets(self):
        """儲存最後推文 ID 記錄"""
        try:
            with open(TWITTER_LAST_TWEETS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.last_tweets, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"❌ 儲存最後推文 ID 失敗: {e}")
    
    def add_account(self, username: str, display_name: str = None) -> bool:
        """添加追蹤帳號"""
        try:
            username = username.lstrip('@').lower().strip()
            if not display_name:
                display_name = username
            self.accounts[username] = display_name
            self.save_accounts()
            print(f"✅ 添加 Twitter 帳號: @{username}")
            return True
        except Exception as e:
            print(f"❌ 添加帳號失敗: {e}")
            return False
    
    def remove_account(self, username: str) -> bool:
        """移除追蹤帳號"""
        try:
            username = username.lstrip('@').lower()
            if username in self.accounts:
                del self.accounts[username]
                if username in self.last_tweets:
                    del self.last_tweets[username]
                self.save_accounts()
                self.save_last_tweets()
                print(f"✅ 移除 Twitter 帳號: @{username}")
                return True
            return False
        except Exception as e:
            print(f"❌ 移除帳號失敗: {e}")
            return False
    
    async def get_user_id(self, username: str) -> Optional[str]:
        """獲取用戶 ID"""
        if not TWITTER_BEARER_TOKEN:
            print("⚠️ Twitter Bearer Token 未設置")
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
                        user_id = data.get('data', {}).get('id')
                        print(f"✅ 獲取用戶 ID: @{username} = {user_id}")
                        return user_id
                    else:
                        print(f"❌ 獲取用戶 ID 失敗: {resp.status}")
            except Exception as e:
                print(f"❌ 獲取用戶 ID 錯誤: {e}")
        
        return None
    
    async def check_new_tweets_auto(self, username: str) -> List[Dict]:
        """自動檢查新推文 - 只返回最新的一篇"""
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
                    'max_results': 5,
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
                            latest_tweet = tweets[0]
                            self.last_tweets[username] = latest_tweet['id']
                            self.save_last_tweets()
                            print(f"✅ 找到 1 條最新推文: @{username}")
                            return [latest_tweet]
            except Exception as e:
                print(f"❌ 檢查推文錯誤: {e}")
        
        return []
    
    async def check_new_tweets(self, username: str, max_results: int = 10) -> List[Dict]:
        """檢查新推文"""
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
        
        print(f"🔄 開始翻譯推文 (@{username})...")
        translated_text = await self.translator.translate(text)
        
        notification = f"""
🐦 <b>X (Twitter) 最新推文</b>

👤 <b>用戶:</b> @{username} ({display_name})
🕐 <b>發文時間:</b> {time_str} (台北時間)

━━━━━━━━━━━━━━━━━━━━

📝 <b>原文內容:</b>
{text}

━━━━━━━━━━━━━━━━━━━━

🇹🇼 <b>繁體中文翻譯:</b>
{translated_text}

━━━━━━━━━━━━━━━━━━━━

🔗 <b>查看原文連結:</b>
https://twitter.com/{username}/status/{tweet_id}
"""
        
        return notification

# ========== Tether 監控 ==========

class TetherMonitor:
    """Tether 鑄造監控類"""
    
    def __init__(self):
        self.last_block_checked = self.load_last_block()
        self.last_tx_hash = ''
        print(f"✅ Tether Monitor 初始化完成，最後區塊: {self.last_block_checked}")
    
    def load_last_block(self) -> int:
        """載入最後檢查的區塊號"""
        if os.path.exists(TETHER_LAST_FILE):
            try:
                with open(TETHER_LAST_FILE, 'r') as f:
                    data = json.load(f)
                    block = data.get('last_block', 0)
                    print(f"✅ 載入最後檢查區塊: {block}")
                    return block
            except:
                return 0
        return 0
    
    def save_last_block(self, block_number: int):
        """儲存最後檢查的區塊號"""
        with open(TETHER_LAST_FILE, 'w') as f:
            json.dump({'last_block': block_number}, f)
        print(f"✅ 儲存最後檢查區塊: {block_number}")
    
    async def get_latest_block(self) -> Optional[int]:
        """獲取最新區塊號"""
        if not ETHERSCAN_API_KEY:
            print("⚠️ Etherscan API Key 未設置")
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
                                    block_num = int(result, 16)
                                    print(f"✅ 獲取最新區塊: {block_num}")
                                    return block_num
                                else:
                                    try:
                                        block_num = int(result)
                                        print(f"✅ 獲取最新區塊: {block_num}")
                                        return block_num
                                    except:
                                        pass
            except Exception as e:
                print(f"❌ 獲取最新區塊錯誤: {e}")
        
        return None
    
    async def check_tether_mints(self) -> List[Dict]:
        """檢查 Tether 鑄造事件"""
        if not ETHERSCAN_API_KEY:
            return []
        
        latest_block = await self.get_latest_block()
        if not latest_block:
            return []
        
        if self.last_block_checked == 0:
            self.last_block_checked = latest_block - 1000
            print(f"📊 初始化最後區塊: {self.last_block_checked}")
        
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
                            
                            if mints:
                                print(f"✅ 發現 {len(mints)} 筆 Tether 鑄造")
                            
                            return mints
                        else:
                            self.last_block_checked = latest_block
                            self.save_last_block(latest_block)
            except Exception as e:
                print(f"❌ 檢查 Tether 鑄造錯誤: {e}")
        
        return []
    
    async def get_recent_mints(self, limit: int = 10) -> List[Dict]:
        """獲取最近的鑄造記錄"""
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
                            
                            print(f"✅ 獲取 {len(mints)} 筆最近鑄造記錄")
                            return mints
            except Exception as e:
                print(f"❌ 獲取最近鑄造錯誤: {e}")
        
        return []
    
    def format_mint_notification(self, tx: Dict) -> str:
        """格式化鑄造通知"""
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

# ========== Hyperliquid 巨鯨追蹤 ==========

class WhaleTracker:
    """巨鯨追蹤類"""
    
    def __init__(self):
        self.whales: Dict[str, str] = self.load_whales()
        self.last_positions: Dict[str, Dict] = {}
        self.subscribed_chats = self.load_subscribed_chats()  # 修改：從文件載入
        print(f"✅ Whale Tracker 初始化完成，追蹤 {len(self.whales)} 個巨鯨，{len(self.subscribed_chats)} 個訂閱")
        
    def load_whales(self) -> Dict[str, str]:
        """載入巨鯨列表"""
        if os.path.exists(WHALES_FILE):
            try:
                with open(WHALES_FILE, 'r', encoding='utf-8') as f:
                    whales = json.load(f)
                    print(f"✅ 載入巨鯨列表: {len(whales)} 個")
                    return whales
            except:
                return {}
        return {}
    
    def save_whales(self):
        """儲存巨鯨列表"""
        with open(WHALES_FILE, 'w', encoding='utf-8') as f:
            json.dump(self.whales, f, ensure_ascii=False, indent=2)
        print(f"✅ 儲存巨鯨列表成功")
    
    def load_subscribed_chats(self) -> set:
        """載入訂閱列表 - 新增方法"""
        if os.path.exists(SUBSCRIBED_CHATS_FILE):
            try:
                with open(SUBSCRIBED_CHATS_FILE, 'r', encoding='utf-8') as f:
                    chats = json.load(f)
                    print(f"✅ 載入訂閱列表: {len(chats)} 個")
                    return set(chats)
            except Exception as e:
                print(f"⚠️ 載入訂閱列表失敗: {e}")
                return set()
        return set()
    
    def save_subscribed_chats(self):
        """儲存訂閱列表 - 新增方法"""
        try:
            with open(SUBSCRIBED_CHATS_FILE, 'w', encoding='utf-8') as f:
                json.dump(list(self.subscribed_chats), f, ensure_ascii=False, indent=2)
            print(f"✅ 儲存訂閱列表成功: {len(self.subscribed_chats)} 個")
        except Exception as e:
            print(f"❌ 儲存訂閱列表失敗: {e}")
    
    def add_whale(self, address: str, name: str) -> bool:
        """新增巨鯨"""
        try:
            if not address.startswith('0x') or len(address) != 42:
                print(f"❌ 地址格式不正確: {address}")
                return False
            
            address = address.lower()
            self.whales[address] = name
            self.save_whales()
            print(f"✅ 新增巨鯨: {name} ({address})")
            return True
        except Exception as e:
            print(f"❌ 新增巨鯨失敗: {e}")
            return False
    
    def remove_whale(self, address: str) -> bool:
        """移除巨鯨"""
        try:
            address = address.lower()
            if address in self.whales:
                name = self.whales[address]
                del self.whales[address]
                if address in self.last_positions:
                    del self.last_positions[address]
                self.save_whales()
                print(f"✅ 移除巨鯨: {name} ({address})")
                return True
            return False
        except Exception as e:
            print(f"❌ 移除巨鯨失敗: {e}")
            return False
    
    async def fetch_positions(self, address: str) -> List[Dict]:
        """獲取巨鯨持倉"""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f'{HYPERLIQUID_API}/info',
                    json={'type': 'clearinghouseState', 'user': address},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        positions = data.get('assetPositions', [])
                        print(f"✅ 獲取 {address[:10]}... 持倉: {len(positions)} 個")
                        return positions
            except Exception as e:
                print(f"❌ 獲取 {address[:10]}... 持倉錯誤: {e}")
        return []
    
    async def fetch_user_fills(self, address: str) -> List[Dict]:
        """獲取巨鯨交易歷史"""
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(
                    f'{HYPERLIQUID_API}/info',
                    json={'type': 'userFills', 'user': address},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        fills = data if isinstance(data, list) else []
                        print(f"✅ 獲取 {address[:10]}... 交易歷史: {len(fills)} 筆")
                        return fills
            except Exception as e:
                print(f"❌ 獲取 {address[:10]}... 交易歷史錯誤: {e}")
        return []
    
    def format_position(self, pos: Dict) -> str:
        """格式化持倉信息"""
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
        """檢測倉位變化"""
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
                print(f"📊 檢測到開倉: {coin} {direction}")
        
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
                print(f"📊 檢測到平倉: {coin} {direction}")
        
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
                    print(f"📊 檢測到加倉: {coin} {direction}")
                else:
                    notifications.append(
                        f"📉 <b>減倉</b>\n"
                        f"幣種: <b>{coin}</b>\n"
                        f"方向: {direction}\n"
                        f"保證金變化: ${old_margin:,.2f} → ${new_margin:,.2f} USDT\n"
                        f"減少: ${abs(margin_diff):,.2f} USDT"
                    )
                    changes[coin] = 'reduce'
                    print(f"📊 檢測到減倉: {coin} {direction}")
        
        self.last_positions[address] = new_pos_dict
        
        return notifications, changes

# ========== 初始化全局物件 ==========

print("\n" + "="*60)
print("🚀 初始化全局物件...")
print("="*60)

tracker = WhaleTracker()
mexc_tracker = MEXCTracker()
tether_monitor = TetherMonitor()
twitter_monitor = TwitterMonitor()

print("="*60)
print("✅ 所有物件初始化完成")
print("="*60 + "\n")

# ========== Telegram Bot 輔助函數 ==========

def get_keyboard(address: str = None) -> InlineKeyboardMarkup:
    """生成鍵盤按鈕"""
    keyboard = []
    if address:
        keyboard.append([InlineKeyboardButton("🔄 立即更新", callback_data=f"refresh:{address}")])
        keyboard.append([InlineKeyboardButton("📋 複製地址", callback_data=f"copy:{address}")])
        keyboard.append([InlineKeyboardButton("📜 查看歷史", callback_data=f"history:{address}")])
    else:
        keyboard.append([InlineKeyboardButton("🔄 立即更新", callback_data="refresh_all")])
    return InlineKeyboardMarkup(keyboard)

def get_mexc_keyboard() -> InlineKeyboardMarkup:
    """生成 MEXC 鍵盤按鈕"""
    keyboard = []
    keyboard.append([InlineKeyboardButton("🔄 立即更新", callback_data="mexc_refresh")])
    keyboard.append([
        InlineKeyboardButton("📊 每日統計", callback_data="mexc_stats:1"),
        InlineKeyboardButton("📊 每週統計", callback_data="mexc_stats:7")
    ])
    keyboard.append([InlineKeyboardButton("📜 交易歷史", callback_data="mexc_history")])
    return InlineKeyboardMarkup(keyboard)

def get_whale_list_keyboard(action: str) -> InlineKeyboardMarkup:
    """生成巨鯨列表鍵盤"""
    keyboard = []
    for address, name in tracker.whales.items():
        keyboard.append([InlineKeyboardButton(
            f"🐋 {name}", 
            callback_data=f"{action}:{address}"
        )])
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)

def get_twitter_list_keyboard(action: str) -> InlineKeyboardMarkup:
    """生成 Twitter 帳號列表鍵盤"""
    keyboard = []
    for username, display_name in twitter_monitor.accounts.items():
        keyboard.append([InlineKeyboardButton(
            f"🐦 @{username} ({display_name})", 
            callback_data=f"{action}:{username}"
        )])
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    return InlineKeyboardMarkup(keyboard)

# ========== Telegram Bot 命令處理 ==========

async def setup_commands(application: Application):
    """設置機器人命令"""
    print("📋 設置機器人命令...")
    commands = [
        BotCommand("start", "🤖 啟動機器人"),
        BotCommand("list", "🐋 查看 Hyperliquid 追蹤列表"),
        BotCommand("addwhale", "➕ 新增 Hyperliquid 巨鯨追蹤"),
        BotCommand("delwhale", "➖ 移除 Hyperliquid 巨鯨追蹤"),
        BotCommand("whalecheck", "🐋 查看特定 Hyperliquid 巨鯨"),
        BotCommand("allwhale", "🐋 查看所有 Hyperliquid 巨鯨持倉"),
        BotCommand("history", "📜 查看 Hyperliquid 巨鯨歷史紀錄"),
        BotCommand("mexc", "💼 查看 MEXC 帳號持倉"),
        BotCommand("mexcstats", "📊 查看 MEXC 統計數據"),
        BotCommand("mexchistory", "📜 查看 MEXC 交易歷史"),
        BotCommand("checktether", "💵 查看 Tether 鑄造狀態"),
        BotCommand("tetherhistory", "📋 查看 Tether 轉帳紀錄"),
        BotCommand("xlist", "🐦 查看追蹤的 X 帳號"),
        BotCommand("addx", "➕ 添加 X 帳號追蹤"),
        BotCommand("removex", "➖ 移除 X 帳號追蹤"),
        BotCommand("checkx", "🔍 查看 X 推文"),
        BotCommand("test", "🔧 測試API連接"),
    ]
    await application.bot.set_my_commands(commands)
    print("✅ 機器人命令設置完成")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """啟動命令"""
    try:
        chat_id = update.effective_chat.id
        tracker.subscribed_chats.add(chat_id)
        tracker.save_subscribed_chats()  # 修改：保存訂閱列表
        print(f"✅ 新用戶訂閱: {chat_id}，總訂閱數: {len(tracker.subscribed_chats)}")
        
        await update.message.reply_text(
            "🤖 <b>加密貨幣巨鯨追蹤機器人</b>\n"
            "🧑 <b>作者:Kai0601</b>\n\n"
            "🐋 <b>Hyperliquid 巨鯨追蹤:</b>\n"
            "/list - 查看追蹤列表\n"
            "/addwhale - 新增巨鯨追蹤\n"
            "/delwhale - 移除巨鯨追蹤\n"
            "/whalecheck - 查看特定巨鯨\n"
            "/allwhale - 查看所有巨鯨持倉\n"
            "/history - 查看巨鯨歷史紀錄\n\n"
            "💼 <b>MEXC 倉位追蹤:</b>\n"
            "/mexc - 查看帳號持倉\n"
            "/mexcstats - 查看統計數據\n"
            "/mexchistory - 查看交易歷史\n\n"
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
            "✅ <b>您已訂閱自動通知!</b>\n"
            "• Hyperliquid 定時推送: 每小時 00 分、30 分\n"
            "• MEXC 定時推送: 每 15 分鐘 (00, 15, 30, 45 分)\n"
            "• 即時交易通知: 檢測到變化立即推送",
            parse_mode='HTML'
        )
    except Exception as e:
        print(f"❌ start 命令錯誤: {e}")
        import traceback
        traceback.print_exc()

# Hyperliquid 巨鯨追蹤命令

async def addwhale_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """開始新增巨鯨的流程"""
    try:
        print(f"➕ 用戶 {update.effective_chat.id} 開始新增 Hyperliquid 巨鯨")
        await update.message.reply_text(
            "🐋 <b>新增 Hyperliquid 巨鯨追蹤</b>\n\n"
            "請輸入巨鯨的錢包地址\n\n"
            "範例: <code>0x1234567890abcdef1234567890abcdef12345678</code>\n\n"
            "💡 地址必須是 42 個字元，以 0x 開頭\n\n"
            "輸入 /cancel 取消操作",
            parse_mode='HTML'
        )
        return WAITING_FOR_WHALE_ADDRESS
    except Exception as e:
        print(f"❌ addwhale_start 錯誤: {e}")
        return ConversationHandler.END

async def addwhale_receive_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """接收巨鯨地址"""
    try:
        address = update.message.text.strip()
        
        if not address.startswith('0x') or len(address) != 42:
            await update.message.reply_text(
                "❌ 地址格式不正確！\n\n"
                "請確認地址:\n"
                "• 以 0x 開頭\n"
                "• 總長度為 42 個字元\n\n"
                "請重新輸入或 /cancel 取消"
            )
            return WAITING_FOR_WHALE_ADDRESS
        
        if address.lower() in tracker.whales:
            whale_name = tracker.whales[address.lower()]
            await update.message.reply_text(
                f"⚠️ 此地址已在追蹤列表中！\n\n"
                f"🐋 名稱: {whale_name}\n"
                f"📍 地址: <code>{address}</code>",
                parse_mode='HTML'
            )
            return ConversationHandler.END
        
        await update.message.reply_text("🔍 正在驗證地址...")
        
        positions = await tracker.fetch_positions(address)
        
        context.user_data['whale_address'] = address
        context.user_data['has_positions'] = len(positions) > 0
        
        await update.message.reply_text(
            f"✅ 地址驗證成功！\n\n"
            f"📍 地址: <code>{address}</code>\n"
            f"📊 當前持倉: {len(positions)} 個\n\n"
            f"請輸入巨鯨的顯示名稱\n\n"
            f"範例: <code>巨鯨A</code> 或 <code>機構投資者</code>",
            parse_mode='HTML'
        )
        return WAITING_FOR_WHALE_NAME
    except Exception as e:
        print(f"❌ addwhale_receive_address 錯誤: {e}")
        return ConversationHandler.END

async def addwhale_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """接收巨鯨名稱"""
    try:
        address = context.user_data.get('whale_address')
        name = update.message.text.strip()
        
        if not name:
            await update.message.reply_text("❌ 名稱不能為空，請重新輸入")
            return WAITING_FOR_WHALE_NAME
        
        if len(name) > 50:
            await update.message.reply_text("❌ 名稱過長（最多50字元），請重新輸入")
            return WAITING_FOR_WHALE_NAME
        
        success = tracker.add_whale(address, name)
        
        if success:
            has_positions = context.user_data.get('has_positions', False)
            
            await update.message.reply_text(
                f"✅ <b>成功新增 Hyperliquid 巨鯨追蹤！</b>\n\n"
                f"🐋 名稱: {name}\n"
                f"📍 地址: <code>{address}</code>\n"
                f"📊 當前持倉: {'有持倉' if has_positions else '暫無持倉'}\n\n"
                f"⚡ 系統將每分鐘自動檢查巨鯨動態\n"
                f"📢 發現交易變動時會立即通知您\n"
                f"🕐 每小時 00 分、30 分推送持倉報告",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text("❌ 新增失敗，請稍後再試")
        
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        print(f"❌ addwhale_receive_name 錯誤: {e}")
        return ConversationHandler.END

async def addwhale_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消新增操作"""
    await update.message.reply_text("❌ 已取消新增 Hyperliquid 巨鯨操作")
    context.user_data.clear()
    return ConversationHandler.END

async def delwhale_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除巨鯨追蹤"""
    try:
        if not tracker.whales:
            await update.message.reply_text("📭 目前沒有追蹤任何 Hyperliquid 巨鯨")
            return
        
        keyboard = get_whale_list_keyboard("delwhale")
        await update.message.reply_text(
            "🐋 <b>選擇要移除的 Hyperliquid 巨鯨:</b>\n\n"
            "⚠️ 移除後將停止監控該地址的所有交易活動",
            parse_mode='HTML',
            reply_markup=keyboard
        )
    except Exception as e:
        print(f"❌ delwhale_command 錯誤: {e}")

async def list_whales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看巨鯨列表"""
    try:
        if not tracker.whales:
            await update.message.reply_text("📭 目前沒有追蹤任何 Hyperliquid 巨鯨")
            return
        
        text = "🐋 <b>Hyperliquid 巨鯨列表:</b>\n\n"
        for i, (addr, name) in enumerate(tracker.whales.items(), 1):
            text += f"{i}. {name}\n{addr}\n\n"
        
        await update.message.reply_text(text, parse_mode='HTML')
    except Exception as e:
        print(f"❌ list_whales 錯誤: {e}")

async def show_all_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示所有 Hyperliquid 巨鯨持倉"""
    try:
        if not tracker.whales:
            await update.message.reply_text("📭 目前沒有追蹤任何 Hyperliquid 巨鯨")
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

async def whale_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """選擇要查看的 Hyperliquid 巨鯨"""
    try:
        if not tracker.whales:
            await update.message.reply_text("📭 目前沒有追蹤任何 Hyperliquid 巨鯨")
            return
        
        keyboard = get_whale_list_keyboard("check")
        await update.message.reply_text("請選擇要查看的 Hyperliquid 巨鯨:", reply_markup=keyboard)
    except Exception as e:
        print(f"❌ whale_check 錯誤: {e}")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """選擇要查看歷史的 Hyperliquid 巨鯨"""
    try:
        if not tracker.whales:
            await update.message.reply_text("📭 目前沒有追蹤任何 Hyperliquid 巨鯨")
            return
        
        keyboard = get_whale_list_keyboard("history")
        await update.message.reply_text("請選擇要查看歷史的 Hyperliquid 巨鯨:", reply_markup=keyboard)
    except Exception as e:
        print(f"❌ history_command 錯誤: {e}")

# MEXC 命令

async def mexc_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看 MEXC 帳號持倉"""
    try:
        if not MEXC_API_KEY or not MEXC_SECRET_KEY:
            await update.message.reply_text(
                "❌ 未設置 MEXC API 憑證\n\n"
                "請在 .env 文件中添加:\n"
                "MEXC_API_KEY=你的API_KEY\n"
                "MEXC_SECRET_KEY=你的SECRET_KEY"
            )
            return
        
        await update.message.reply_text("🔍 正在獲取 MEXC 持倉...")
        
        positions = await mexc_tracker.fetch_positions()
        
        if not positions:
            await update.message.reply_text("📭 MEXC 帳號目前沒有持倉")
            return
        
        taipei_time = datetime.now(timezone(timedelta(hours=8)))
        text = f"💼 <b>MEXC 帳號</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
        
        for pos in positions:
            text += mexc_tracker.format_position(pos)
        
        await update.message.reply_text(text, parse_mode='HTML', reply_markup=get_mexc_keyboard())
    except Exception as e:
        print(f"❌ mexc_command 錯誤: {e}")

async def mexcstats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看 MEXC 統計數據"""
    try:
        keyboard = [
            [
                InlineKeyboardButton("📊 每日統計", callback_data="mexc_stats:1"),
                InlineKeyboardButton("📊 每週統計", callback_data="mexc_stats:7")
            ],
            [
                InlineKeyboardButton("📊 每月統計", callback_data="mexc_stats:30"),
                InlineKeyboardButton("📊 全部統計", callback_data="mexc_stats:0")
            ],
            [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
        ]
        
        await update.message.reply_text(
            "📊 <b>MEXC 交易統計</b>\n\n"
            "請選擇統計週期:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        print(f"❌ mexcstats_command 錯誤: {e}")

async def mexchistory_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看 MEXC 交易歷史"""
    try:
        keyboard = [
            [
                InlineKeyboardButton("📜 近 10 筆", callback_data="mexc_history:10"),
                InlineKeyboardButton("📜 近 20 筆", callback_data="mexc_history:20")
            ],
            [
                InlineKeyboardButton("📜 近 50 筆", callback_data="mexc_history:50"),
                InlineKeyboardButton("📜 近 100 筆", callback_data="mexc_history:100")
            ],
            [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
        ]
        
        await update.message.reply_text(
            "📜 <b>MEXC 交易歷史</b>\n\n"
            "請選擇查看筆數:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        print(f"❌ mexchistory_command 錯誤: {e}")

# Twitter 追蹤命令

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
                f"📢 發現新推文時會立即通知您\n"
                f"   • 顯示原文內容\n"
                f"   • 繁體中文翻譯\n"
                f"   • 發文時間\n"
                f"   • 原文連結",
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
                f"📢 發現新推文時會立即通知您\n"
                f"   • 顯示原文內容\n"
                f"   • 繁體中文翻譯\n"
                f"   • 發文時間\n"
                f"   • 原文連結",
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

async def xlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看追蹤的 X 帳號列表"""
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
        text += "📢 發現新推文會立即通知 (含原文+翻譯+連結)"
        
        await update.message.reply_text(text, parse_mode='HTML')
    except Exception as e:
        print(f"❌ xlist_command 錯誤: {e}")

async def removex_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除 X 帳號追蹤"""
    try:
        if not twitter_monitor.accounts:
            await update.message.reply_text("📭 目前沒有追蹤任何 X 帳號")
            return
        
        keyboard = get_twitter_list_keyboard("removex")
        await update.message.reply_text("請選擇要移除的 X 帳號:", reply_markup=keyboard)
    except Exception as e:
        print(f"❌ removex_command 錯誤: {e}")

# Tether 監控命令

async def check_tether(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看 Tether 鑄造狀態"""
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

async def tether_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tether 轉帳紀錄查詢"""
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

# 測試命令

async def test_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """測試 API 連接"""
    try:
        await update.message.reply_text("🔍 正在測試API連接...")
        
        results = []
        results.append(f"📝 TELEGRAM_TOKEN: {'✅ 已設置' if TELEGRAM_TOKEN else '❌ 未設置'}")
        results.append(f"🌐 HYPERLIQUID_API: {'✅ 已設置' if HYPERLIQUID_API else '❌ 未設置'}")
        results.append(f"🔑 ETHERSCAN_API_KEY: {'✅ 已設置' if ETHERSCAN_API_KEY else '❌ 未設置'}")
        results.append(f"🐦 TWITTER_BEARER_TOKEN: {'✅ 已設置' if TWITTER_BEARER_TOKEN else '❌ 未設置'}")
        results.append(f"💼 MEXC_API_KEY: {'✅ 已設置' if MEXC_API_KEY else '❌ 未設置'}")
        results.append(f"🔐 MEXC_SECRET_KEY: {'✅ 已設置' if MEXC_SECRET_KEY else '❌ 未設置'}")
        results.append(f"👥 訂閱用戶數: {len(tracker.subscribed_chats)}")
        
        # 測試 Hyperliquid API
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
            hyperliquid_test = f"❌ 連接失敗"
        
        results.append(f"🔗 Hyperliquid API: {hyperliquid_test}")
        
        # 測試 Etherscan API
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

# 按鈕回調處理

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理所有按鈕回調"""
    query = update.callback_query
    
    try:
        await query.answer()
        
        data = query.data
        print(f"🔘 按鈕回調: {data}")
        
        if data == "cancel":
            await query.edit_message_text("❌ 已取消")
            return
        
        # Hyperliquid 相關回調
        if data.startswith("delwhale:"):
            address = data.split(":", 1)[1]
            success = tracker.remove_whale(address)
            if success:
                await query.edit_message_text("✅ 已移除 Hyperliquid 巨鯨追蹤")
            else:
                await query.edit_message_text("❌ 移除失敗")
            return
        
        if data.startswith("check:"):
            address = data.split(":", 1)[1]
            name = tracker.whales.get(address, "未知")
            
            await query.edit_message_text(f"🔍 正在獲取 {name} 的持倉...")
            
            positions = await tracker.fetch_positions(address)
            
            if not positions:
                await query.message.reply_text(f"📭 {name} 目前沒有持倉")
                return
            
            taipei_time = datetime.now(timezone(timedelta(hours=8)))
            text = f"🐋 <b>{name}</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
            
            for pos in positions:
                text += tracker.format_position(pos)
            
            await query.message.reply_text(text, parse_mode='HTML', reply_markup=get_keyboard(address))
            return
        
        if data.startswith("history:"):
            address = data.split(":", 1)[1]
            name = tracker.whales.get(address, "未知")
            
            await query.edit_message_text(f"🔍 正在獲取 {name} 的交易歷史...")
            
            fills = await tracker.fetch_user_fills(address)
            
            if not fills:
                await query.message.reply_text(f"📭 {name} 沒有交易歷史")
                return
            
            keyboard = [
                [
                    InlineKeyboardButton("最近 10 筆", callback_data=f"history_filter:{address}:10"),
                    InlineKeyboardButton("最近 20 筆", callback_data=f"history_filter:{address}:20")
                ],
                [
                    InlineKeyboardButton("最近 50 筆", callback_data=f"history_filter:{address}:50"),
                    InlineKeyboardButton("最近 100 筆", callback_data=f"history_filter:{address}:100")
                ],
                [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
            ]
            
            await query.message.reply_text(
                f"📜 <b>{name} 的交易歷史</b>\n\n"
                f"總共有 {len(fills)} 筆交易記錄\n\n"
                f"請選擇要查看的筆數:",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        if data.startswith("history_filter:"):
            parts = data.split(":")
            address = parts[1]
            limit = int(parts[2])
            name = tracker.whales.get(address, "未知")
            
            fills = await tracker.fetch_user_fills(address)
            fills = fills[:limit]
            
            text = f"📜 <b>{name} 最近 {len(fills)} 筆交易</b>\n\n"
            
            for fill in fills:
                coin = fill.get('coin', 'UNKNOWN')
                side = fill.get('side', '')
                px = float(fill.get('px', 0))
                sz = float(fill.get('sz', 0))
                timestamp = int(fill.get('time', 0))
                
                dt = datetime.fromtimestamp(timestamp / 1000, timezone(timedelta(hours=8)))
                time_str = dt.strftime('%m-%d %H:%M')
                
                side_emoji = "🟢" if side == "B" else "🔴"
                side_text = "買入" if side == "B" else "賣出"
                
                text += f"{side_emoji} {coin} {side_text} {sz:.4f} @ ${px:.4f}\n"
                text += f"   {time_str}\n\n"
            
            max_length = 4000
            if len(text) > max_length:
                parts = [text[i:i+max_length] for i in range(0, len(text), max_length)]
                for part in parts:
                    await query.message.reply_text(part, parse_mode='HTML')
            else:
                await query.message.reply_text(text, parse_mode='HTML')
            return
        
        if data.startswith("refresh:"):
            address = data.split(":", 1)[1]
            name = tracker.whales.get(address, "未知")
            
            positions = await tracker.fetch_positions(address)
            
            if not positions:
                await query.answer(f"{name} 目前沒有持倉", show_alert=True)
                return
            
            taipei_time = datetime.now(timezone(timedelta(hours=8)))
            text = f"🐋 <b>{name}</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
            
            for pos in positions:
                text += tracker.format_position(pos)
            
            await query.message.edit_text(text, parse_mode='HTML', reply_markup=get_keyboard(address))
            await query.answer("✅ 已更新")
            return
        
        if data.startswith("copy:"):
            address = data.split(":", 1)[1]
            await query.answer(f"地址: {address}", show_alert=True)
            return
        
        # MEXC 相關回調
        if data == "mexc_refresh":
            positions = await mexc_tracker.fetch_positions()
            
            if not positions:
                await query.answer("MEXC 帳號目前沒有持倉", show_alert=True)
                return
            
            taipei_time = datetime.now(timezone(timedelta(hours=8)))
            text = f"💼 <b>MEXC 帳號</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
            
            for pos in positions:
                text += mexc_tracker.format_position(pos)
            
            await query.message.edit_text(text, parse_mode='HTML', reply_markup=get_mexc_keyboard())
            await query.answer("✅ 已更新")
            return
        
        if data.startswith("mexc_stats:"):
            days = int(data.split(":")[1])
            
            if days == 0:
                period = "全部"
                stats = mexc_tracker.calculate_statistics()
            else:
                period = f"近 {days} 天"
                stats = mexc_tracker.calculate_statistics(days=days)
            
            text = mexc_tracker.format_statistics(stats, period)
            await query.message.reply_text(text, parse_mode='HTML')
            return
        
        if data == "mexc_history":
            keyboard = [
                [
                    InlineKeyboardButton("📜 近 10 筆", callback_data="mexc_history:10"),
                    InlineKeyboardButton("📜 近 20 筆", callback_data="mexc_history:20")
                ],
                [
                    InlineKeyboardButton("📜 近 50 筆", callback_data="mexc_history:50"),
                    InlineKeyboardButton("📜 近 100 筆", callback_data="mexc_history:100")
                ],
                [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
            ]
            
            await query.message.reply_text(
                "📜 <b>MEXC 交易歷史</b>\n\n"
                "請選擇查看筆數:",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        if data.startswith("mexc_history:"):
            limit = int(data.split(":")[1])
            
            text = mexc_tracker.format_trade_history(mexc_tracker.trades_history, limit)
            await query.message.reply_text(text, parse_mode='HTML')
            return
        
        # Twitter 相關回調
        if data.startswith("checkx_user:"):
            username = data.split(":", 1)[1]
            
            await query.edit_message_text(f"🔍 正在獲取 @{username} 的推文...")
            
            tweets = await twitter_monitor.check_new_tweets(username, max_results=10)
            
            if not tweets:
                await query.message.reply_text(f"📭 @{username} 目前沒有推文或無法獲取")
                return
            
            keyboard = [
                [
                    InlineKeyboardButton("最近 5 筆", callback_data=f"checkx_count:{username}:5"),
                    InlineKeyboardButton("最近 10 筆", callback_data=f"checkx_count:{username}:10")
                ],
                [InlineKeyboardButton("❌ 取消", callback_data="cancel")]
            ]
            
            await query.message.reply_text(
                f"🐦 <b>@{username} 的推文</b>\n\n"
                f"請選擇要查看的筆數:",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            return
        
        if data.startswith("checkx_count:"):
            parts = data.split(":")
            username = parts[1]
            count = int(parts[2])
            
            tweets = await twitter_monitor.check_new_tweets(username, max_results=count)
            
            if not tweets:
                await query.message.reply_text(f"📭 無法獲取 @{username} 的推文")
                return
            
            for tweet in tweets:
                notification = await twitter_monitor.format_tweet_notification(username, tweet, show_full=True)
                await query.message.reply_text(notification, parse_mode='HTML')
                await asyncio.sleep(2)
            
            return
        
        if data.startswith("removex:"):
            username = data.split(":", 1)[1]
            success = twitter_monitor.remove_account(username)
            if success:
                await query.edit_message_text(f"✅ 已移除 @{username} 的追蹤")
            else:
                await query.edit_message_text("❌ 移除失敗")
            return
        
        # Tether 相關回調
        if data.startswith("tether_history:"):
            limit = int(data.split(":")[1])
            
            await query.edit_message_text(f"🔍 正在查詢最近 {limit} 筆 Tether 鑄造記錄...")
            
            mints = await tether_monitor.get_recent_mints(limit)
            
            if not mints:
                await query.message.reply_text("📭 沒有找到 Tether 鑄造記錄")
                return
            
            for mint in mints:
                notification = tether_monitor.format_mint_notification(mint)
                await query.message.reply_text(notification, parse_mode='HTML')
                await asyncio.sleep(1)
            
            return
        
    except Exception as e:
        print(f"❌ button_callback 錯誤: {e}")
        import traceback
        traceback.print_exc()
        try:
            await query.answer("發生錯誤,請稍後再試")
        except:
            pass

# ========== 定時任務 ==========

async def auto_update(context: ContextTypes.DEFAULT_TYPE):
    """Hyperliquid 巨鯨持倉自動更新"""
    global last_scheduled_push_time
    
    try:
        # 添加調試日誌
        print(f"\n{'='*60}")
        print(f"🔄 auto_update 觸發")
        print(f"追蹤巨鯨數: {len(tracker.whales)}")
        print(f"訂閱用戶數: {len(tracker.subscribed_chats)}")
        print(f"訂閱用戶列表: {list(tracker.subscribed_chats)}")
        print(f"{'='*60}\n")
        
        if not tracker.whales or not tracker.subscribed_chats:
            print(f"⚠️ 跳過推送: whales={len(tracker.whales)}, subscribed={len(tracker.subscribed_chats)}")
            return
        
        taipei_time = datetime.now(timezone(timedelta(hours=8)))
        current_hour = taipei_time.hour
        current_minute = taipei_time.minute
        
        # 計算當前時間標記
        if current_minute >= 30:
            current_time_mark = f"{current_hour:02d}:30"
        else:
            current_time_mark = f"{current_hour:02d}:00"
        
        # 擴大推送窗口到 5 分鐘
        in_push_window = (0 <= current_minute <= 4) or (30 <= current_minute <= 34)
        should_push = in_push_window and last_scheduled_push_time != current_time_mark
        
        print(f"⏰ 當前時間: {taipei_time.strftime('%H:%M:%S')}")
        print(f"📍 時間標記: {current_time_mark}")
        print(f"🔔 推送窗口: {in_push_window}")
        print(f"📮 應該推送: {should_push}")
        print(f"🕐 上次推送: {last_scheduled_push_time}")
        
        if should_push:
            print(f"\n{'='*60}")
            print(f"🕐 Hyperliquid 定時推送觸發: {taipei_time.strftime('%H:%M:%S')}")
            print(f"{'='*60}\n")
            
            last_scheduled_push_time = current_time_mark
        
        for address, name in tracker.whales.items():
            print(f"🔍 檢查巨鯨: {name} ({address[:10]}...)")
            
            positions = await tracker.fetch_positions(address)
            
            if not positions:
                print(f"📭 {name} 無持倉")
                continue
            
            notifications, changes = tracker.detect_position_changes(address, positions)
            
            # 即時通知
            if notifications:
                print(f"⚡ 檢測到 {len(notifications)} 個變化")
                for notification in notifications:
                    text = f"🐋 <b>{name}</b>\n⚡ <b>即時交易通知</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)\n\n{notification}"
                    
                    for chat_id in tracker.subscribed_chats:
                        try:
                            print(f"📤 發送即時通知到 {chat_id}")
                            await context.bot.send_message(
                                chat_id=chat_id,
                                text=text,
                                parse_mode='HTML',
                                reply_markup=get_keyboard(address)
                            )
                            print(f"✅ 成功發送到 {chat_id}")
                        except Exception as e:
                            print(f"❌ 發送即時通知錯誤 (chat_id: {chat_id}): {e}")
                    
                    await asyncio.sleep(1)
            
            # 定時推送
            if should_push:
                print(f"🔔 發送定時報告: {name}")
                text = f"🐋 <b>{name}</b>\n🔔 <b>定時持倉報告</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
                
                for pos in positions:
                    text += tracker.format_position(pos)
                
                for chat_id in tracker.subscribed_chats:
                    try:
                        print(f"📤 發送定時報告到 {chat_id}")
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode='HTML',
                            reply_markup=get_keyboard(address)
                        )
                        print(f"✅ 成功發送到 {chat_id}")
                    except Exception as e:
                        print(f"❌ 發送定時報告錯誤 (chat_id: {chat_id}): {e}")
                
                await asyncio.sleep(1)
    
    except Exception as e:
        print(f"❌ auto_update 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def mexc_auto_update(context: ContextTypes.DEFAULT_TYPE):
    """MEXC 倉位自動更新 - 每15分鐘推送一次"""
    global last_mexc_positions, last_mexc_push_time
    
    try:
        # 添加調試日誌
        print(f"\n{'='*60}")
        print(f"🔄 mexc_auto_update 觸發")
        print(f"訂閱用戶數: {len(tracker.subscribed_chats)}")
        print(f"MEXC API 設置: {bool(MEXC_API_KEY and MEXC_SECRET_KEY)}")
        print(f"{'='*60}\n")
        
        if not tracker.subscribed_chats or not MEXC_API_KEY or not MEXC_SECRET_KEY:
            print(f"⚠️ 跳過 MEXC 推送: subscribed={len(tracker.subscribed_chats)}, api_key={bool(MEXC_API_KEY)}")
            return
        
        taipei_time = datetime.now(timezone(timedelta(hours=8)))
        current_minute = taipei_time.minute
        
        # 計算當前應該推送的時間標記 (每15分鐘: 00, 15, 30, 45)
        if 0 <= current_minute < 15:
            current_time_mark = f"{taipei_time.hour:02d}:00"
        elif 15 <= current_minute < 30:
            current_time_mark = f"{taipei_time.hour:02d}:15"
        elif 30 <= current_minute < 45:
            current_time_mark = f"{taipei_time.hour:02d}:30"
        else:
            current_time_mark = f"{taipei_time.hour:02d}:45"
        
        # 擴大推送窗口到 3 分鐘
        in_push_window = (
            (0 <= current_minute <= 2) or 
            (15 <= current_minute <= 17) or 
            (30 <= current_minute <= 32) or 
            (45 <= current_minute <= 47)
        )
        
        should_push = in_push_window and last_mexc_push_time != current_time_mark
        
        print(f"⏰ MEXC 當前時間: {taipei_time.strftime('%H:%M:%S')}")
        print(f"📍 MEXC 時間標記: {current_time_mark}")
        print(f"🔔 MEXC 推送窗口: {in_push_window}")
        print(f"📮 MEXC 應該推送: {should_push}")
        print(f"🕐 MEXC 上次推送: {last_mexc_push_time}")
        
        if should_push:
            print(f"\n{'='*60}")
            print(f"🕐 MEXC 定時推送觸發: {taipei_time.strftime('%H:%M:%S')}")
            print(f"{'='*60}\n")
            
            last_mexc_push_time = current_time_mark
        
        positions = await mexc_tracker.fetch_positions()
        
        if not positions and not last_mexc_positions:
            print(f"📭 MEXC 無持倉")
            return
        
        notifications, new_pos_dict = mexc_tracker.detect_position_changes(last_mexc_positions, positions)
        
        # 即時通知 - 檢測到變化時立即發送
        if notifications:
            print(f"⚡ MEXC 檢測到 {len(notifications)} 個變化")
            for notification in notifications:
                text = f"💼 <b>MEXC 帳號</b>\n⚡ <b>即時交易通知</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)\n\n{notification}"
                
                for chat_id in tracker.subscribed_chats:
                    try:
                        print(f"📤 發送 MEXC 即時通知到 {chat_id}")
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode='HTML',
                            reply_markup=get_mexc_keyboard()
                        )
                        print(f"✅ 成功發送到 {chat_id}")
                    except Exception as e:
                        print(f"❌ 發送 MEXC 即時通知錯誤: {e}")
                
                await asyncio.sleep(1)
        
        last_mexc_positions = new_pos_dict
        
        # 定時推送 - 每15分鐘發送一次完整持倉報告
        if should_push and positions:
            print(f"🔔 發送 MEXC 定時報告")
            text = f"💼 <b>MEXC 帳號</b>\n🔔 <b>定時持倉報告</b>\n🕐 {taipei_time.strftime('%m-%d %H:%M:%S')} (台北)"
            
            for pos in positions:
                text += mexc_tracker.format_position(pos)
            
            for chat_id in tracker.subscribed_chats:
                try:
                    print(f"📤 發送 MEXC 定時報告到 {chat_id}")
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode='HTML',
                        reply_markup=get_mexc_keyboard()
                    )
                    print(f"✅ 成功發送到 {chat_id}")
                except Exception as e:
                    print(f"❌ 發送 MEXC 定時報告錯誤: {e}")
            
            await asyncio.sleep(1)
    
    except Exception as e:
        print(f"❌ mexc_auto_update 錯誤: {e}")
        import traceback
        traceback.print_exc()

async def tether_update(context: ContextTypes.DEFAULT_TYPE):
    """Tether 鑄造監控更新"""
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
                            print(f"❌ 發送 Tether 通知錯誤: {e}")
                    
                    tether_monitor.last_tx_hash = tx_hash
                    await asyncio.sleep(2)
    except Exception as e:
        print(f"❌ Tether 更新錯誤: {e}")

async def twitter_update(context: ContextTypes.DEFAULT_TYPE):
    """Twitter 即時更新"""
    try:
        if not tracker.subscribed_chats or not TWITTER_BEARER_TOKEN or not twitter_monitor.accounts:
            return
        
        for username in twitter_monitor.accounts.keys():
            tweets = await twitter_monitor.check_new_tweets_auto(username)
            
            if tweets:
                tweet = tweets[0]
                
                notification = await twitter_monitor.format_tweet_notification(username, tweet, show_full=True)
                
                for chat_id in tracker.subscribed_chats:
                    try:
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=notification,
                            parse_mode='HTML'
                        )
                    except Exception as e:
                        print(f"❌ 發送 Twitter 通知錯誤: {e}")
                
                await asyncio.sleep(2)
        
    except Exception as e:
        print(f"❌ Twitter 更新錯誤: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """全局錯誤處理"""
    print(f"❌ 全局錯誤: {context.error}")
    import traceback
    traceback.print_exc()

async def health_check(request):
    """健康檢查"""
    return web.Response(text="✅ Bot 運行中!")

async def start_health_server():
    """啟動健康檢查服務器"""
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ Health server 啟動 port {port}")
    
    return site

async def post_init(application: Application):
    """初始化後執行"""
    try:
        print("📋 設置命令...")
        await setup_commands(application)
        print("✅ 命令設置完成")
    except Exception as e:
        print(f"❌ post_init 錯誤: {e}")

def main():
    """主程式入口"""
    try:
        print("\n" + "="*60)
        print("🤖 Telegram Bot 啟動中...")
        print("="*60)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        loop.run_until_complete(start_health_server())
        
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .post_init(post_init)
            .build()
        )
        
        # 添加 Twitter 追蹤對話處理器
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
        
        # 添加 Hyperliquid 巨鯨追蹤對話處理器
        addwhale_conv_handler = ConversationHandler(
            entry_points=[CommandHandler('addwhale', addwhale_start)],
            states={
                WAITING_FOR_WHALE_ADDRESS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, addwhale_receive_address)
                ],
                WAITING_FOR_WHALE_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, addwhale_receive_name)
                ],
            },
            fallbacks=[CommandHandler('cancel', addwhale_cancel)],
        )
        
        # 註冊所有命令處理器
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("test", test_api))
        
        # Hyperliquid 命令
        application.add_handler(CommandHandler("list", list_whales))
        application.add_handler(addwhale_conv_handler)
        application.add_handler(CommandHandler("delwhale", delwhale_command))
        application.add_handler(CommandHandler("whalecheck", whale_check))
        application.add_handler(CommandHandler("allwhale", show_all_positions))
        application.add_handler(CommandHandler("history", history_command))
        
        # MEXC 命令
        application.add_handler(CommandHandler("mexc", mexc_command))
        application.add_handler(CommandHandler("mexcstats", mexcstats_command))
        application.add_handler(CommandHandler("mexchistory", mexchistory_command))
        
        # Tether 命令
        application.add_handler(CommandHandler("checktether", check_tether))
        application.add_handler(CommandHandler("tetherhistory", tether_history_command))
        
        # Twitter 命令
        application.add_handler(CommandHandler("xlist", xlist_command))
        application.add_handler(addx_conv_handler)
        application.add_handler(CommandHandler("removex", removex_command))
        application.add_handler(CommandHandler("checkx", checkx_command))
        
        application.add_handler(CallbackQueryHandler(button_callback))
        
        application.add_error_handler(error_handler)
        
        # 設置定時任務
        job_queue = application.job_queue
        if job_queue:
            job_queue.run_repeating(auto_update, interval=60, first=10)
            job_queue.run_repeating(mexc_auto_update, interval=60, first=20)
            job_queue.run_repeating(tether_update, interval=300, first=30)
            job_queue.run_repeating(twitter_update, interval=180, first=60)
            print("✅ 定時任務已設置:")
            print("   • Hyperliquid 巨鯨監控: 每 60 秒檢查一次")
            print("   • MEXC 倉位監控: 每 60 秒檢查一次")
            print("   • Hyperliquid 定時推送: 每小時 00 分、30 分 (5分鐘窗口)")
            print("   • MEXC 定時推送: 每 15 分鐘 (00, 15, 30, 45 分, 3分鐘窗口)")
            print("   • Tether: 每 300 秒")
            print("   • Twitter: 每 180 秒")
        
        print("="*60)
        print("✅ Bot 啟動成功")
        print(f"📊 當前追蹤: {len(tracker.whales)} 個巨鯨")
        print(f"👥 當前訂閱: {len(tracker.subscribed_chats)} 個用戶")
        print("="*60)
        
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
        
    except Exception as e:
        print(f"❌ 主程式錯誤: {e}")
        import traceback
        traceback.print_exc()

if __name__ == '__main__':
    main()