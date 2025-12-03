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

# Twitter 雙 API 支援
TWITTER_BEARER_TOKEN_1 = os.getenv('TWITTER_BEARER_TOKEN_1')
TWITTER_BEARER_TOKEN_2 = os.getenv('TWITTER_BEARER_TOKEN_2')

# 翻譯服務 API（支援多個 Google Translate 配置）
TRANSLATE_PROXY_1 = os.getenv('TRANSLATE_PROXY_1', '')
TRANSLATE_PROXY_2 = os.getenv('TRANSLATE_PROXY_2', '')

# 檔案路徑
WHALES_FILE = os.path.join(os.path.dirname(__file__), 'whales.json')
TETHER_LAST_FILE = os.path.join(os.path.dirname(__file__), 'tether_last.json')
TWITTER_ACCOUNTS_FILE = os.path.join(os.path.dirname(__file__), 'twitter_accounts.json')
TWITTER_LAST_TWEETS_FILE = os.path.join(os.path.dirname(__file__), 'twitter_last_tweets.json')
SUBSCRIBED_CHATS_FILE = os.path.join(os.path.dirname(__file__), 'subscribed_chats.json')
TWITTER_API_STATUS_FILE = os.path.join(os.path.dirname(__file__), 'twitter_api_status.json')
TRANSLATOR_STATUS_FILE = os.path.join(os.path.dirname(__file__), 'translator_status.json')

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

if not TELEGRAM_TOKEN:
    raise ValueError("請在 .env 文件中設置 TELEGRAM_TOKEN")

# ========== 翻譯服務 (支援雙 API 切換) ==========

class TranslationService:
    """翻譯服務 - 支援多個翻譯引擎輪換（類似 X API 邏輯）"""
    
    def __init__(self):
        self.translators = []
        self.current_translator_index = 0
        self.translator_status = self.load_translator_status()
        
        # 初始化多個翻譯引擎（每個都是獨立的實例）
        try:
            # 翻譯器 1 - 主要
            translator1 = GoogleTranslator(source='auto', target='zh-TW')
            self.translators.append(('Translator-1', translator1))
            print("✅ Google Translator 1 初始化成功")
        except Exception as e:
            print(f"⚠️ Google Translator 1 初始化失敗: {e}")
        
        try:
            # 翻譯器 2 - 備用（使用不同的源語言設定）
            translator2 = GoogleTranslator(source='en', target='zh-TW')
            self.translators.append(('Translator-2', translator2))
            print("✅ Google Translator 2 初始化成功")
        except Exception as e:
            print(f"⚠️ Google Translator 2 初始化失敗: {e}")
        
        try:
            # 翻譯器 3 - 額外備用
            translator3 = GoogleTranslator(source='auto', target='zh-CN')  # 使用簡體中文作為備選
            self.translators.append(('Translator-3-CN', translator3))
            print("✅ Google Translator 3 初始化成功")
        except Exception as e:
            print(f"⚠️ Google Translator 3 初始化失敗: {e}")
        
        if not self.translators:
            print("❌ 所有翻譯器初始化失敗")
        
        print(f"✅ 翻譯服務初始化完成，可用翻譯器: {len(self.translators)} 個")
    
    def load_translator_status(self) -> Dict:
        """載入翻譯器狀態"""
        if os.path.exists(TRANSLATOR_STATUS_FILE):
            try:
                with open(TRANSLATOR_STATUS_FILE, 'r', encoding='utf-8') as f:
                    status = json.load(f)
                    print(f"✅ 載入翻譯器狀態")
                    return status
            except:
                pass
        
        # 默認狀態
        return {
            'failed_translators': [],
            'last_reset': datetime.now(timezone(timedelta(hours=8))).isoformat()
        }
    
    def save_translator_status(self):
        """儲存翻譯器狀態"""
        try:
            with open(TRANSLATOR_STATUS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.translator_status, f, ensure_ascii=False, indent=2)
            print(f"✅ 儲存翻譯器狀態成功")
        except Exception as e:
            print(f"❌ 儲存翻譯器狀態失敗: {e}")
    
    def check_and_reset_translator_status(self):
        """檢查是否需要重置翻譯器狀態（每天重置）"""
        try:
            last_reset = datetime.fromisoformat(self.translator_status.get('last_reset', ''))
            now = datetime.now(timezone(timedelta(hours=8)))
            
            # 如果超過24小時，重置狀態
            if (now - last_reset).total_seconds() > 86400:
                print("🔄 重置翻譯器狀態（24小時已過）")
                self.translator_status = {
                    'failed_translators': [],
                    'last_reset': now.isoformat()
                }
                self.save_translator_status()
                return True
        except:
            pass
        
        return False
    
    def get_current_translator(self) -> Optional[Tuple[str, any]]:
        """獲取當前可用的翻譯器（類似 X API 邏輯）"""
        if not self.translators:
            return None
        
        # 檢查並重置狀態
        self.check_and_reset_translator_status()
        
        failed_translators = set(self.translator_status.get('failed_translators', []))
        
        # 嘗試找到可用的翻譯器
        attempts = 0
        while attempts < len(self.translators):
            translator_name, translator = self.translators[self.current_translator_index]
            
            if translator_name not in failed_translators:
                print(f"✅ 使用翻譯器: {translator_name}")
                return translator_name, translator
            
            # 切換到下一個翻譯器
            self.current_translator_index = (self.current_translator_index + 1) % len(self.translators)
            attempts += 1
        
        print("❌ 所有翻譯器都已失敗")
        return None
    
    def mark_translator_failed(self, translator_name: str):
        """標記翻譯器為失敗"""
        if translator_name not in self.translator_status['failed_translators']:
            self.translator_status['failed_translators'].append(translator_name)
            self.save_translator_status()
            print(f"⚠️ {translator_name} 已標記為失敗")
    
    def switch_to_next_translator(self):
        """切換到下一個翻譯器"""
        self.current_translator_index = (self.current_translator_index + 1) % len(self.translators)
        print(f"🔄 切換到下一個翻譯器")
    
    async def translate_with_rotation(self, text: str) -> Tuple[str, str]:
        """使用輪換機制翻譯（類似 X API 邏輯）"""
        if not self.translators:
            return text, "無可用翻譯器"
        
        translator_info = self.get_current_translator()
        if not translator_info:
            return text, "所有翻譯器額度已用完"
        
        translator_name, translator = translator_info
        
        try:
            print(f"🔄 使用翻譯器: {translator_name}")
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, lambda: translator.translate(text))
            print(f"✅ {translator_name} 翻譯成功")
            
            # 成功後切換到下一個翻譯器，實現負載均衡
            self.switch_to_next_translator()
            
            return result, translator_name
        
        except Exception as e:
            print(f"❌ {translator_name} 翻譯失敗: {e}")
            error_msg = str(e).lower()
            
            # 如果是速率限制錯誤，標記為失敗並切換
            if any(keyword in error_msg for keyword in ['rate', 'limit', 'quota', '429', 'too many']):
                print(f"⚠️ {translator_name} 達到速率限制，標記為失敗")
                self.mark_translator_failed(translator_name)
            
            # 切換到下一個翻譯器並重試
            self.switch_to_next_translator()
            
            # 嘗試下一個翻譯器
            next_translator = self.get_current_translator()
            if next_translator and next_translator[0] != translator_name:
                return await self.translate_with_rotation(text)
            
            return text, f"翻譯失敗: {str(e)[:50]}"
    
    async def translate(self, text: str) -> str:
        """翻譯文字"""
        if not text or len(text) < 5:
            return text
        
        result, status = await self.translate_with_rotation(text)
        return result
    
    def reset_failed_translators(self):
        """重置失敗的翻譯器（每天重置一次）"""
        self.translator_status['failed_translators'] = []
        self.translator_status['last_reset'] = datetime.now(timezone(timedelta(hours=8))).isoformat()
        self.save_translator_status()
        print("✅ 翻譯器狀態已重置")
    
    def get_status(self) -> str:
        """獲取翻譯器狀態"""
        total = len(self.translators)
        failed = set(self.translator_status.get('failed_translators', []))
        available = total - len(failed)
        
        status = f"📊 翻譯器狀態:\n"
        status += f"總數: {total}\n"
        status += f"可用: {available}\n"
        status += f"失敗: {len(failed)}\n\n"
        
        for name, _ in self.translators:
            if name in failed:
                status += f"❌ {name}: 已達速率限制\n"
            else:
                status += f"✅ {name}: 可用\n"
        
        last_reset = self.translator_status.get('last_reset', 'Unknown')
        try:
            reset_dt = datetime.fromisoformat(last_reset)
            status += f"\n🕐 上次重置: {reset_dt.strftime('%Y-%m-%d %H:%M:%S')}"
        except:
            pass
        
        return status

# ========== Twitter 監控 (支援雙 API 切換 + 完整推文內容) ==========

class TwitterMonitor:
    """Twitter/X 監控類 - 支援雙 API 自動切換 + 獲取完整推文"""
    
    def __init__(self):
        self.accounts: Dict[str, str] = self.load_accounts()
        self.last_tweets: Dict[str, str] = self.load_last_tweets()
        self.translator = TranslationService()
        
        # 雙 API 配置
        self.api_tokens = []
        if TWITTER_BEARER_TOKEN_1:
            self.api_tokens.append(('API-1', TWITTER_BEARER_TOKEN_1))
        if TWITTER_BEARER_TOKEN_2:
            self.api_tokens.append(('API-2', TWITTER_BEARER_TOKEN_2))
        
        self.current_api_index = 0
        self.api_status = self.load_api_status()
        
        print(f"✅ Twitter Monitor 初始化完成")
        print(f"   • 追蹤 {len(self.accounts)} 個帳號")
        print(f"   • 可用 API: {len(self.api_tokens)} 個")
    
    def load_api_status(self) -> Dict:
        """載入 API 狀態"""
        if os.path.exists(TWITTER_API_STATUS_FILE):
            try:
                with open(TWITTER_API_STATUS_FILE, 'r', encoding='utf-8') as f:
                    status = json.load(f)
                    print(f"✅ 載入 Twitter API 狀態")
                    return status
            except:
                pass
        
        # 默認狀態
        return {
            'failed_apis': [],
            'last_reset': datetime.now(timezone(timedelta(hours=8))).isoformat()
        }
    
    def save_api_status(self):
        """儲存 API 狀態"""
        try:
            with open(TWITTER_API_STATUS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.api_status, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"❌ 儲存 Twitter API 狀態失敗: {e}")
    
    def check_and_reset_api_status(self):
        """檢查是否需要重置 API 狀態（每天重置）"""
        try:
            last_reset = datetime.fromisoformat(self.api_status.get('last_reset', ''))
            now = datetime.now(timezone(timedelta(hours=8)))
            
            # 如果超過24小時，重置狀態
            if (now - last_reset).total_seconds() > 86400:
                print("🔄 重置 Twitter API 狀態（24小時已過）")
                self.api_status = {
                    'failed_apis': [],
                    'last_reset': now.isoformat()
                }
                self.save_api_status()
                return True
        except:
            pass
        
        return False
    
    def get_current_api(self) -> Optional[Tuple[str, str]]:
        """獲取當前可用的 API"""
        if not self.api_tokens:
            return None
        
        # 檢查並重置狀態
        self.check_and_reset_api_status()
        
        failed_apis = set(self.api_status.get('failed_apis', []))
        
        # 嘗試找到可用的 API
        attempts = 0
        while attempts < len(self.api_tokens):
            api_name, token = self.api_tokens[self.current_api_index]
            
            if api_name not in failed_apis:
                print(f"✅ 使用 Twitter {api_name}")
                return api_name, token
            
            # 切換到下一個 API
            self.current_api_index = (self.current_api_index + 1) % len(self.api_tokens)
            attempts += 1
        
        print("❌ 所有 Twitter API 都已失敗")
        return None
    
    def mark_api_failed(self, api_name: str):
        """標記 API 為失敗"""
        if api_name not in self.api_status['failed_apis']:
            self.api_status['failed_apis'].append(api_name)
            self.save_api_status()
            print(f"⚠️ Twitter {api_name} 已標記為失敗")
    
    def switch_to_next_api(self):
        """切換到下一個 API"""
        self.current_api_index = (self.current_api_index + 1) % len(self.api_tokens)
        print(f"🔄 切換到下一個 Twitter API")
    
    def get_api_status_text(self) -> str:
        """獲取 API 狀態文字"""
        if not self.api_tokens:
            return "❌ 未設置 Twitter API"
        
        failed = set(self.api_status.get('failed_apis', []))
        total = len(self.api_tokens)
        available = total - len(failed)
        
        status = f"📊 Twitter API 狀態:\n"
        status += f"總數: {total}\n"
        status += f"可用: {available}\n"
        status += f"失敗: {len(failed)}\n\n"
        
        for api_name, _ in self.api_tokens:
            if api_name in failed:
                status += f"❌ {api_name}: 已達速率限制\n"
            else:
                status += f"✅ {api_name}: 可用\n"
        
        last_reset = self.api_status.get('last_reset', 'Unknown')
        try:
            reset_dt = datetime.fromisoformat(last_reset)
            status += f"\n🕐 上次重置: {reset_dt.strftime('%Y-%m-%d %H:%M:%S')}"
        except:
            pass
        
        return status
    
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
        api_info = self.get_current_api()
        if not api_info:
            print("⚠️ 沒有可用的 Twitter API")
            return None
        
        api_name, token = api_info
        username = username.lstrip('@')
        
        async with aiohttp.ClientSession() as session:
            try:
                headers = {
                    'Authorization': f'Bearer {token}'
                }
                
                url = f'https://api.twitter.com/2/users/by/username/{username}'
                
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        user_id = data.get('data', {}).get('id')
                        print(f"✅ 獲取用戶 ID: @{username} = {user_id}")
                        return user_id
                    elif resp.status == 429:
                        print(f"⚠️ {api_name} 達到速率限制")
                        self.mark_api_failed(api_name)
                        self.switch_to_next_api()
                        # 嘗試用下一個 API
                        return await self.get_user_id(username)
                    else:
                        print(f"❌ 獲取用戶 ID 失敗: {resp.status}")
            except Exception as e:
                print(f"❌ 獲取用戶 ID 錯誤: {e}")
        
        return None
    
    def extract_full_text(self, tweet: Dict) -> str:
        """提取完整推文文本（解決 t.co 短連結問題）"""
        # Twitter API v2 返回的完整文本
        # 優先使用 note_tweet.text（超長推文）
        if 'note_tweet' in tweet and 'text' in tweet['note_tweet']:
            full_text = tweet['note_tweet']['text']
            print(f"✅ 使用 note_tweet 完整文本，長度: {len(full_text)}")
            return full_text
        
        # 使用普通 text
        text = tweet.get('text', '')
        
        # 檢查是否有 entities（包含 URLs）
        entities = tweet.get('entities', {})
        urls = entities.get('urls', [])
        
        # 替換所有 t.co 短連結為完整 URL
        for url_obj in urls:
            short_url = url_obj.get('url', '')
            expanded_url = url_obj.get('expanded_url', '')
            display_url = url_obj.get('display_url', '')
            
            # 如果有展開的 URL，替換短連結
            if short_url and expanded_url:
                text = text.replace(short_url, expanded_url)
                print(f"✅ 替換短連結: {short_url} -> {expanded_url}")
        
        print(f"✅ 提取完整文本，長度: {len(text)}")
        return text
    
    async def check_new_tweets_auto(self, username: str) -> List[Dict]:
        """自動檢查新推文 - 只返回最新的一篇（獲取完整文本）"""
        api_info = self.get_current_api()
        if not api_info:
            print("⚠️ 沒有可用的 Twitter API")
            return []
        
        api_name, token = api_info
        username = username.lstrip('@').lower()
        user_id = await self.get_user_id(username)
        
        if not user_id:
            return []
        
        async with aiohttp.ClientSession() as session:
            try:
                headers = {
                    'Authorization': f'Bearer {token}'
                }
                
                # 修改參數以獲取完整文本
                params = {
                    'max_results': 5,
                    'tweet.fields': 'created_at,text,author_id,entities,note_tweet',  # 添加 note_tweet
                    'expansions': 'author_id',
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
                    elif resp.status == 429:
                        print(f"⚠️ {api_name} 達到速率限制")
                        self.mark_api_failed(api_name)
                        self.switch_to_next_api()
                        # 不重試，等待下次輪詢
                        return []
            except Exception as e:
                print(f"❌ 檢查推文錯誤: {e}")
        
        return []
    
    async def check_new_tweets(self, username: str, max_results: int = 10) -> List[Dict]:
        """檢查新推文（獲取完整文本）"""
        api_info = self.get_current_api()
        if not api_info:
            print("❌ 沒有可用的 Twitter API")
            return []
        
        api_name, token = api_info
        username = username.lstrip('@').lower()
        user_id = await self.get_user_id(username)
        
        if not user_id:
            print(f"❌ 無法獲取用戶 ID: {username}")
            return []
        
        async with aiohttp.ClientSession() as session:
            try:
                headers = {
                    'Authorization': f'Bearer {token}'
                }
                
                # 修改參數以獲取完整文本
                params = {
                    'max_results': min(max_results, 100),
                    'tweet.fields': 'created_at,text,author_id,entities,note_tweet',  # 添加 note_tweet
                    'expansions': 'author_id',
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
                        print(f"⚠️ {api_name} 達到速率限制")
                        self.mark_api_failed(api_name)
                        self.switch_to_next_api()
                        
                        # 嘗試用下一個 API
                        next_api = self.get_current_api()
                        if next_api and next_api[0] != api_name:
                            return await self.check_new_tweets(username, max_results)
                    else:
                        error_text = await resp.text()
                        print(f"❌ Twitter API 錯誤 {resp.status}: {error_text[:200]}")
            except Exception as e:
                print(f"❌ 檢查推文錯誤: {e}")
        
        return []
    
    async def format_tweet_notification(self, username: str, tweet: Dict, show_full: bool = True) -> str:
        """格式化推文通知（使用完整文本）"""
        display_name = self.accounts.get(username, username)
        tweet_id = tweet.get('id', '')
        
        # 使用完整文本提取方法
        text = self.extract_full_text(tweet)
        
        created_at = tweet.get('created_at', '')
        
        try:
            dt = datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%S.%fZ')
            dt = dt.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8)))
            time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
        except:
            time_str = created_at
        
        print(f"🔄 開始翻譯推文 (@{username})，文本長度: {len(text)}")
        translated_text = await self.translator.translate(text)
        print(f"✅ 翻譯完成，翻譯長度: {len(translated_text)}")
        
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
        self.subscribed_chats = self.load_subscribed_chats()
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
        """載入訂閱列表"""
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
        """儲存訂閱列表"""
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
tether_monitor = TetherMonitor()
twitter_monitor = TwitterMonitor()

print("="*60)
print("✅ 所有物件初始化完成")
print(f"   • 翻譯器: {len(twitter_monitor.translator.translators)} 個")
print("="*60 + "\n")

# ========== 輔助函數 ==========

def get_keyboard(address: str) -> InlineKeyboardMarkup:
    """生成持倉查詢鍵盤"""
    keyboard = [
        [
            InlineKeyboardButton("🔄 更新", callback_data=f"refresh:{address}"),
            InlineKeyboardButton("📜 歷史", callback_data=f"history:{address}")
        ],
        [
            InlineKeyboardButton("📋 複製地址", callback_data=f"copy:{address}")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_whale_list_keyboard(action: str) -> InlineKeyboardMarkup:
    """生成巨鯨列表鍵盤"""
    keyboard = []
    
    for address, name in tracker.whales.items():
        short_addr = f"{address[:6]}...{address[-4:]}"
        button_text = f"{name} ({short_addr})"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"{action}:{address}")])
    
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    
    return InlineKeyboardMarkup(keyboard)

def get_twitter_list_keyboard(action: str) -> InlineKeyboardMarkup:
    """生成 Twitter 列表鍵盤"""
    keyboard = []
    
    for username, display_name in twitter_monitor.accounts.items():
        button_text = f"@{username} ({display_name})"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"{action}:{username}")])
    
    keyboard.append([InlineKeyboardButton("❌ 取消", callback_data="cancel")])
    
    return InlineKeyboardMarkup(keyboard)

# ========== 設置 Bot 命令 ==========

async def setup_commands(application: Application):
    """設置 Bot 命令列表"""
    commands = [
        BotCommand("start", "開始使用 Bot / 查看指令列表"),
        BotCommand("list", "查看 Hyperliquid 巨鯨列表"),
        BotCommand("whalecheck", "查看指定巨鯨持倉"),
        BotCommand("allwhale", "查看所有巨鯨持倉"),
        BotCommand("history", "查看巨鯨交易歷史"),
        BotCommand("checktether", "查看 Tether 鑄造狀態"),
        BotCommand("tetherhistory", "查看 Tether 鑄造歷史"),
        BotCommand("xlist", "查看追蹤的 X 帳號列表"),
        BotCommand("checkx", "查看指定 X 用戶推文"),
    ]
    
    await application.bot.set_my_commands(commands)
    print("✅ Bot 命令設置完成")

# ========== Telegram Bot 命令處理 ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """開始命令 - 首次訂閱，後續顯示指令列表"""
    chat_id = update.effective_chat.id
    
    # 檢查是否已經訂閱
    is_new_subscriber = chat_id not in tracker.subscribed_chats
    
    if is_new_subscriber:
        # 首次使用 - 訂閱通知
        tracker.subscribed_chats.add(chat_id)
        tracker.save_subscribed_chats()
        
        welcome_text = """
🎉 <b>歡迎使用加密貨幣追蹤 Bot！</b>

您已成功訂閱所有通知服務！

━━━━━━━━━━━━━━━━━━━━

📊 <b>系統將自動推送以下通知：</b>

🐋 <b>Hyperliquid 巨鯨追蹤</b>
  • 每 15 分鐘自動檢查巨鯨動態
  • 發現交易變動立即通知
  • 每小時 00 分、30 分推送完整持倉報告

🐦 <b>X (Twitter) 推文追蹤</b>
  • 每 10 分鐘自動檢查新推文
  • 發現新推文立即通知
  • 顯示完整原文 + 繁體翻譯 + 連結

💵 <b>Tether 鑄造監控</b>
  • 每 5 分鐘自動檢查
  • 發現鑄造立即通知

━━━━━━━━━━━━━━━━━━━━

使用 /start 查看所有可用指令
"""
        
        await update.message.reply_text(welcome_text, parse_mode='HTML')
    
    else:
        # 已訂閱用戶 - 顯示指令列表
        command_text = """
📋 <b>加密貨幣巨鯨追蹤機器人</b>
👷 <b>作者: Kaio601</b>
━━━━━━━━━━━━━━━━━━━━
🐋 <b>Hyperliquid 巨鯨追蹤:</b>
/list - 查看追蹤列表
/whalecheck - 查看指定巨鯨持倉
/allwhale - 查看所有巨鯨持倉
/history - 查看交易歷史

💵 <b>Tether 監控:</b>
/checktether - 查看 Tether 鑄造狀態
/tetherhistory - 查看 Tether 鑄造紀錄

🐦 <b>X (Twitter) 追蹤:</b>
/xlist - 查看追蹤的 X 帳號
/checkx - 查看 X 推文
"""
        
        await update.message.reply_text(command_text, parse_mode='HTML')
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
        await update.message.reply_text("❌ 驗證地址時發生錯誤，請稍後再試")
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
                f"⚡ 系統將每 15 分鐘自動檢查巨鯨動態\n"
                f"📢 發現交易變動時會立即通知您\n"
                f"🕐 每小時 00 分、30 分推送持倉報告（5分鐘窗口）",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text("❌ 新增失敗，請稍後再試")
        
        context.user_data.clear()
        return ConversationHandler.END
    except Exception as e:
        print(f"❌ addwhale_receive_name 錯誤: {e}")
        await update.message.reply_text("❌ 新增失敗，請稍後再試")
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
            short_addr = f"{addr[:6]}...{addr[-4:]}"
            text += f"{i}. <b>{name}</b>\n"
            text += f"   📍 {short_addr}\n\n"
        
        text += f"━━━━━━━━━━━━━━━━━━━━\n"
        text += f"📊 總計: {len(tracker.whales)} 個巨鯨\n"
        text += f"⚡ 監控頻率: 每 15 分鐘\n"
        text += f"🔔 定時推送: 每小時 00 分、30 分"
        
        await update.message.reply_text(text, parse_mode='HTML')
    except Exception as e:
        print(f"❌ list_whales 錯誤: {e}")

async def show_all_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """顯示所有 Hyperliquid 巨鯨持倉"""
    try:
        if not tracker.whales:
            await update.message.reply_text("📭 目前沒有追蹤任何 Hyperliquid 巨鯨")
            return
        
        await update.message.reply_text(f"🔍 正在獲取 {len(tracker.whales)} 個巨鯨的持倉...")
        
        taipei_time = datetime.now(timezone(timedelta(hours=8)))
        
        for address, name in tracker.whales.items():
            positions = await tracker.fetch_positions(address)
            
            if not positions:
                await update.message.reply_text(
                    f"🐋 <b>{name}</b>\n"
                    f"📭 目前沒有持倉",
                    parse_mode='HTML'
                )
                await asyncio.sleep(1)
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
        await update.message.reply_text(
            "🐋 <b>選擇要查看持倉的巨鯨:</b>",
            parse_mode='HTML',
            reply_markup=keyboard
        )
    except Exception as e:
        print(f"❌ whale_check 錯誤: {e}")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """選擇要查看歷史的 Hyperliquid 巨鯨"""
    try:
        if not tracker.whales:
            await update.message.reply_text("📭 目前沒有追蹤任何 Hyperliquid 巨鯨")
            return
        
        keyboard = get_whale_list_keyboard("history")
        await update.message.reply_text(
            "🐋 <b>選擇要查看交易歷史的巨鯨:</b>",
            parse_mode='HTML',
            reply_markup=keyboard
        )
    except Exception as e:
        print(f"❌ history_command 錯誤: {e}")

# Twitter 追蹤命令

async def addx_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """開始添加 X 帳號的流程"""
    try:
        await update.message.reply_text(
            "🐦 <b>新增 X (Twitter) 帳號追蹤</b>\n\n"
            "請輸入要追蹤的 X 帳號用戶名\n\n"
            "範例: <code>realDonaldTrump</code> 或 <code>@elonmusk</code>\n\n"
            "💡 推文將自動翻譯成繁體中文\n"
            "💡 顯示完整原文（無短連結）\n\n"
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
            await update.message.reply_text("❌ 用戶名無效，請重新輸入")
            return WAITING_FOR_TWITTER_USERNAME
        
        if username.lower() in twitter_monitor.accounts:
            await update.message.reply_text(
                f"⚠️ @{username} 已在追蹤列表中！"
            )
            return ConversationHandler.END
        
        context.user_data['twitter_username'] = username
        
        await update.message.reply_text(
            f"✅ 用戶名: <code>@{username}</code>\n\n"
            f"請輸入顯示名稱（可選）\n\n"
            f"範例: <code>川普</code> 或 <code>馬斯克</code>\n\n"
            f"直接按 /skip 跳過，使用用戶名作為顯示名稱",
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
                f"✅ <b>成功添加 X 帳號追蹤！</b>\n\n"
                f"🐦 用戶: @{username}\n"
                f"📝 顯示名稱: {display_name}\n\n"
                f"⚡ 系統將每 10 分鐘自動檢查新推文\n"
                f"📢 發現新推文時會立即通知您：\n"
                f"   • <b>完整原文內容</b>（無 t.co 短連結）\n"
                f"   • <b>繁體中文翻譯</b>\n"
                f"   • 發文時間\n"
                f"   • 原文連結\n\n"
                f"🔄 支援雙 API 自動切換，防止速率限制\n"
                f"🔤 支援多翻譯引擎自動切換",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text("❌ 添加失敗，請稍後再試")
        
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
                f"✅ <b>成功添加 X 帳號追蹤！</b>\n\n"
                f"🐦 用戶: @{username}\n"
                f"📝 顯示名稱: {username}\n\n"
                f"⚡ 系統將每 10 分鐘自動檢查新推文\n"
                f"📢 發現新推文時會立即通知您：\n"
                f"   • <b>完整原文內容</b>（無 t.co 短連結）\n"
                f"   • <b>繁體中文翻譯</b>\n"
                f"   • 發文時間\n"
                f"   • 原文連結\n\n"
                f"🔄 支援雙 API 自動切換，防止速率限制\n"
                f"🔤 支援多翻譯引擎自動切換",
                parse_mode='HTML'
            )
        else:
            await update.message.reply_text("❌ 添加失敗，請稍後再試")
        
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
            await update.message.reply_text(
                "📭 目前沒有追蹤任何 X 帳號\n\n"
                "使用 /addx 添加追蹤帳號"
            )
            return
        
        if not twitter_monitor.api_tokens:
            await update.message.reply_text(
                "❌ 未設置 Twitter Bearer Token\n\n"
                "請在 .env 文件中添加:\n"
                "TWITTER_BEARER_TOKEN_1=你的Token1\n"
                "TWITTER_BEARER_TOKEN_2=你的Token2"
            )
            return
        
        keyboard = get_twitter_list_keyboard("checkx_user")
        await update.message.reply_text(
            "🐦 <b>選擇要查看推文的用戶:</b>\n\n"
            "點擊下方按鈕查看該用戶的最新推文\n"
            "（包含完整原文和翻譯）",
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
            text += f"{i}. <b>@{username}</b> ({display_name})\n"
            if username in twitter_monitor.last_tweets:
                text += f"   最後檢查: ✅ 已檢查\n"
            else:
                text += f"   最後檢查: 🆕 尚未檢查\n"
            text += "\n"
        
        text += "━━━━━━━━━━━━━━━━━━━━\n"
        text += f"📊 總計: {len(twitter_monitor.accounts)} 個帳號\n"
        text += "⚡ 監控頻率: 每 10 分鐘\n"
        text += "📢 推文通知: 完整原文 + 繁體翻譯 + 連結\n"
        
        failed_apis = set(twitter_monitor.api_status.get('failed_apis', []))
        available_apis = len(twitter_monitor.api_tokens) - len(failed_apis)
        text += f"🔄 可用 API: {available_apis}/{len(twitter_monitor.api_tokens)}\n"
        
        failed_translators = set(twitter_monitor.translator.translator_status.get('failed_translators', []))
        available_translators = len(twitter_monitor.translator.translators) - len(failed_translators)
        text += f"🔤 可用翻譯器: {available_translators}/{len(twitter_monitor.translator.translators)}"
        
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
        await update.message.reply_text(
            "🐦 <b>選擇要移除的 X 帳號:</b>\n\n"
            "⚠️ 移除後將停止監控該帳號的推文",
            parse_mode='HTML',
            reply_markup=keyboard
        )
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
        
        text = f"💵 <b>Tether (USDT) 監控狀態</b>\n\n"
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
            "💵 <b>Tether 鑄造紀錄查詢</b>\n\n"
            "請選擇要查詢的筆數:",
            parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        print(f"❌ tether_history_command 錯誤: {e}")
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
            name = tracker.whales.get(address, "未知")
            success = tracker.remove_whale(address)
            if success:
                await query.edit_message_text(f"✅ 已移除 Hyperliquid 巨鯨追蹤: {name}")
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
        
        # Twitter 相關回調
        if data.startswith("checkx_user:"):
            username = data.split(":", 1)[1]
            
            await query.edit_message_text(f"🔍 正在獲取 @{username} 的推文...")
            
            tweets = await twitter_monitor.check_new_tweets(username, max_results=10)
            
            if not tweets:
                # 檢查是否所有 API 都失敗
                failed = set(twitter_monitor.api_status.get('failed_apis', []))
                if len(failed) == len(twitter_monitor.api_tokens):
                    await query.message.reply_text(
                        f"❌ 所有 Twitter API 額度已用完\n\n"
                        f"請使用 /apistatus 查看詳細狀態\n"
                        f"系統會在 24 小時後自動重置"
                    )
                else:
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
                f"請選擇要查看的筆數:\n"
                f"（包含完整原文和繁體翻譯）",
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
            
            await query.message.reply_text(f"🔍 正在處理 {len(tweets)} 條推文（含翻譯）...")
            
            for tweet in tweets:
                notification = await twitter_monitor.format_tweet_notification(username, tweet, show_full=True)
                await query.message.reply_text(notification, parse_mode='HTML')
                await asyncio.sleep(2)
            
            return
        
        if data.startswith("removex:"):
            username = data.split(":", 1)[1]
            display_name = twitter_monitor.accounts.get(username, username)
            success = twitter_monitor.remove_account(username)
            if success:
                await query.edit_message_text(f"✅ 已移除 X 帳號追蹤: @{username} ({display_name})")
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
            await query.answer("發生錯誤，請稍後再試")
        except:
            pass

# ========== 定時任務 ==========

async def auto_update(context: ContextTypes.DEFAULT_TYPE):
    """Hyperliquid 巨鯨持倉自動更新 - 每 15 分鐘執行"""
    global last_scheduled_push_time
    
    try:
        # 詳細調試日誌
        taipei_time = datetime.now(timezone(timedelta(hours=8)))
        print(f"\n{'='*60}")
        print(f"🔄 auto_update 執行時間: {taipei_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"追蹤巨鯨數: {len(tracker.whales)}")
        print(f"訂閱用戶數: {len(tracker.subscribed_chats)}")
        print(f"訂閱列表: {list(tracker.subscribed_chats)}")
        print(f"{'='*60}\n")
        
        if not tracker.whales:
            print(f"⚠️ 沒有追蹤的巨鯨，跳過更新")
            return
        
        if not tracker.subscribed_chats:
            print(f"⚠️ 沒有訂閱用戶，跳過推送")
        
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
        
        print(f"⏰ 當前分鐘: {current_minute}")
        print(f"📍 時間標記: {current_time_mark}")
        print(f"🔔 在推送窗口: {in_push_window}")
        print(f"📮 應該推送: {should_push}")
        print(f"🕐 上次推送標記: {last_scheduled_push_time}")
        
        if should_push:
            print(f"\n{'🔔'*30}")
            print(f"🕐 觸發定時推送: {taipei_time.strftime('%H:%M:%S')}")
            print(f"{'🔔'*30}\n")
            last_scheduled_push_time = current_time_mark
        
        # 遍歷所有巨鯨
        for address, name in tracker.whales.items():
            print(f"\n🔍 檢查巨鯨: {name} ({address[:10]}...)")
            
            positions = await tracker.fetch_positions(address)
            
            if not positions:
                print(f"📭 {name} 無持倉")
                continue
            
            print(f"📊 {name} 當前持倉: {len(positions)} 個")
            
            # 檢測變化
            notifications, changes = tracker.detect_position_changes(address, positions)
            
            # 即時通知 - 有變化時立即推送
            if notifications and tracker.subscribed_chats:
                print(f"⚡ 檢測到 {len(notifications)} 個變化，發送即時通知")
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
                            print(f"❌ 發送失敗 (chat_id: {chat_id}): {e}")
                    
                    await asyncio.sleep(1)
            
            # 定時推送 - 每半小時推送完整持倉
            if should_push and tracker.subscribed_chats:
                print(f"🔔 發送定時持倉報告: {name}")
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
                        print(f"❌ 發送失敗 (chat_id: {chat_id}): {e}")
                
                await asyncio.sleep(1)
        
        print(f"\n{'='*60}")
        print(f"✅ auto_update 執行完成")
        print(f"{'='*60}\n")
    
    except Exception as e:
        print(f"❌ auto_update 錯誤: {e}")
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
    """Twitter 即時更新 - 每 10 分鐘執行"""
    try:
        if not tracker.subscribed_chats or not twitter_monitor.api_tokens or not twitter_monitor.accounts:
            return
        
        print(f"\n🐦 Twitter 更新檢查開始...")
        
        for username in twitter_monitor.accounts.keys():
            print(f"🔍 檢查 @{username} 的新推文...")
            tweets = await twitter_monitor.check_new_tweets_auto(username)
            
            if tweets:
                tweet = tweets[0]
                print(f"✅ 發現 @{username} 的新推文，準備發送通知...")
                
                notification = await twitter_monitor.format_tweet_notification(username, tweet, show_full=True)
                
                for chat_id in tracker.subscribed_chats:
                    try:
                        print(f"📤 發送 Twitter 通知到 {chat_id}")
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=notification,
                            parse_mode='HTML'
                        )
                        print(f"✅ 成功發送到 {chat_id}")
                    except Exception as e:
                        print(f"❌ 發送 Twitter 通知錯誤: {e}")
                
                await asyncio.sleep(2)
        
        print(f"✅ Twitter 更新檢查完成\n")
        
    except Exception as e:
        print(f"❌ Twitter 更新錯誤: {e}")

async def daily_reset_task(context: ContextTypes.DEFAULT_TYPE):
    """每日重置任務 - 重置 API 狀態"""
    try:
        print("🔄 執行每日重置任務")
        
        # 重置 Twitter API 狀態
        twitter_monitor.check_and_reset_api_status()
        
        # 重置翻譯器狀態
        twitter_monitor.translator.reset_failed_translators()
        
        print("✅ 每日重置完成")
    except Exception as e:
        print(f"❌ 每日重置錯誤: {e}")

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
        
        # Hyperliquid 命令
        application.add_handler(CommandHandler("list", list_whales))
        application.add_handler(CommandHandler("whalecheck", whale_check))
        application.add_handler(CommandHandler("allwhale", show_all_positions))
        application.add_handler(CommandHandler("history", history_command))
        
        # Tether 命令
        application.add_handler(CommandHandler("checktether", check_tether))
        application.add_handler(CommandHandler("tetherhistory", tether_history_command))
        
        # Twitter 命令
        application.add_handler(CommandHandler("xlist", xlist_command))
        application.add_handler(CommandHandler("checkx", checkx_command))
        
        application.add_handler(CallbackQueryHandler(button_callback))
        
        application.add_error_handler(error_handler)
        
        # 設置定時任務（已修改間隔）
        job_queue = application.job_queue
        if job_queue:
            # Hyperliquid 巨鯨監控 - 每 15 分鐘檢查（900 秒）
            job_queue.run_repeating(auto_update, interval=900, first=10)
            
            # Tether 監控 - 每 5 分鐘（300 秒）
            job_queue.run_repeating(tether_update, interval=300, first=30)
            
            # Twitter 監控 - 每 10 分鐘（600 秒）
            job_queue.run_repeating(twitter_update, interval=600, first=60)
            
            # 每日重置任務 - 每天凌晨 3 點執行
            job_queue.run_daily(
                daily_reset_task,
                time=datetime.strptime("03:00", "%H:%M").time()
            )
            
            print("✅ 定時任務已設置:")
            print("   • Hyperliquid 巨鯨監控: 每 15 分鐘檢查一次")
            print("   • Hyperliquid 定時推送: 每小時 00 分、30 分 (5分鐘窗口)")
            print("   • Tether 監控: 每 5 分鐘")
            print("   • Twitter 監控: 每 10 分鐘")
            print("   • API 狀態重置: 每天凌晨 3:00")
        
        print("="*60)
        print("✅ Bot 啟動成功")
        print(f"📊 當前追蹤: {len(tracker.whales)} 個巨鯨")
        print(f"👥 當前訂閱: {len(tracker.subscribed_chats)} 個用戶")
        print(f"🐦 Twitter 追蹤: {len(twitter_monitor.accounts)} 個帳號")
        print(f"🔄 Twitter API: {len(twitter_monitor.api_tokens)} 個")
        print(f"🔤 翻譯引擎: {len(twitter_monitor.translator.translators)} 個")
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