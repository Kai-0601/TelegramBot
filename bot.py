import os
import sys
import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
HYPERLIQUID_API = os.getenv('HYPERLIQUID_API', 'https://api.hyperliquid.xyz')
ETHERSCAN_API_KEY = os.getenv('ETHERSCAN_API_KEY')

WHALES_FILE = os.path.join(os.path.dirname(__file__), 'whales.json')
TETHER_LAST_FILE = os.path.join(os.path.dirname(__file__), 'tether_last.json')

TETHER_CONTRACT = '0xdAC17F958D2ee523a2206206994597C13D831ec7'
TETHER_MULTISIG = '0xC6CDE7C39eB2f0F0095F41570af89eFC2C1Ea828'
TETHER_TREASURY = '0x5754284f345afc66a98fbB0a0Afe71e0F007B949'

# 使用 Etherscan V2 API
ETHERSCAN_API = 'https://api.etherscan.io/v2/api'

if not TELEGRAM_TOKEN:
    raise ValueError("請在 .env 文件中設置 TELEGRAM_TOKEN")

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
        """獲取最新區塊號 - 使用 V2 API"""
        if not ETHERSCAN_API_KEY:
            print("❌ Etherscan API Key 未設置")
            return None
        
        async with aiohttp.ClientSession() as session:
            try:
                params = {
                    'chainid': '1',
                    'module': 'proxy',
                    'action': 'eth_blockNumber',
                    'apikey': ETHERSCAN_API_KEY
                }
                
                print(f"🔍 正在獲取最新區塊 (使用 V2 API)...")
                async with session.get(ETHERSCAN_API, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    print(f"📡 HTTP 狀態碼: {resp.status}")
                    
                    if resp.status == 200:
                        data = await resp.json()
                        print(f"📄 API 回應: {data}")
                        
                        result = data.get('result')
                        
                        if result:
                            if isinstance(result, str):
                                if result.startswith('0x'):
                                    block_num = int(result, 16)
                                    print(f"✅ 成功獲取區塊: {block_num:,}")
                                    return block_num
                                else:
                                    try:
                                        block_num = int(result)
                                        print(f"✅ 成功獲取區塊: {block_num:,}")
                                        return block_num
                                    except:
                                        pass
                        
                        print(f"❌ 無法解析區塊號: {result}")
                    else:
                        print(f"❌ HTTP 錯誤: {resp.status}")
                        error_text = await resp.text()
                        print(f"錯誤內容: {error_text[:300]}")
            except Exception as e:
                print(f"❌ 獲取最新區塊錯誤: {e}")
                import traceback
                traceback.print_exc()
        
        return None
    
    async def check_tether_mints(self) -> List[Dict]:
        """監控 Tether 鑄造(從 Multisig 到 Treasury 的轉帳) - 使用 V2 API"""
        if not ETHERSCAN_API_KEY:
            print("❌ Etherscan API key 未設置")
            return []
        
        latest_block = await self.get_latest_block()
        if not latest_block:
            print("❌ 無法獲取最新區塊")
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
                
                print(f"🔍 檢查 Tether 轉帳從區塊 {self.last_block_checked:,} 到 {latest_block:,}")
                
                async with session.get(ETHERSCAN_API, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        print(f"📡 API 狀態: {data.get('status')}, 訊息: {data.get('message')}")
                        
                        if data.get('status') == '1' and data.get('result'):
                            result = data['result']
                            print(f"📊 獲取到 {len(result)} 筆交易到 Treasury")
                            
                            mints = []
                            for tx in result:
                                from_addr = tx.get('from', '').lower()
                                to_addr = tx.get('to', '').lower()
                                
                                if (from_addr == TETHER_MULTISIG.lower() and 
                                    to_addr == TETHER_TREASURY.lower()):
                                    mints.append(tx)
                                    value = int(tx.get('value', '0'))
                                    usdt_amount = value / 1_000_000
                                    print(f"✅ 發現鑄造: {tx.get('hash', '')[:16]}... 數量: {usdt_amount:,.0f} USDT")
                            
                            if mints:
                                print(f"🎯 總共發現 {len(mints)} 筆 Tether 鑄造")
                            else:
                                print(f"ℹ️ 在此區塊範圍內未發現鑄造")
                            
                            self.last_block_checked = latest_block
                            self.save_last_block(latest_block)
                            
                            return mints
                        else:
                            print(f"ℹ️ API 回應: {data}")
                            self.last_block_checked = latest_block
                            self.save_last_block(latest_block)
                    else:
                        print(f"❌ HTTP 錯誤: {resp.status}")
                        error_text = await resp.text()
                        print(f"錯誤內容: {error_text[:300]}")
            except Exception as e:
                print(f"❌ 檢查 Tether 鑄造錯誤: {e}")
                import traceback
                traceback.print_exc()
        
        return []
    
    async def get_recent_mints(self, limit: int = 10) -> List[Dict]:
        """獲取最近的 Tether 鑄造記錄 - 使用 V2 API"""
        if not ETHERSCAN_API_KEY:
            print("❌ Etherscan API key 未設置")
            return []
        
        async with aiohttp.ClientSession() as session:
            try:
                # 使用 V2 API 並增加 chainid 參數
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
                
                print(f"🔍 正在獲取最近的 USDT 轉帳到 Treasury 地址 (使用 V2 API)...")
                print(f"📍 Treasury: {TETHER_TREASURY}")
                print(f"📍 Multisig: {TETHER_MULTISIG}")
                
                async with session.get(ETHERSCAN_API, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    print(f"📡 HTTP 狀態: {resp.status}")
                    
                    if resp.status == 200:
                        data = await resp.json()
                        
                        print(f"📊 API 狀態: {data.get('status')}, 訊息: {data.get('message')}")
                        
                        if data.get('status') == '1' and data.get('result'):
                            result = data['result']
                            print(f"📦 獲取到 {len(result)} 筆總交易")
                            
                            mints = []
                            for tx in result:
                                from_addr = tx.get('from', '').lower()
                                to_addr = tx.get('to', '').lower()
                                
                                # 調試信息 - 只打印前 3 筆
                                if len(mints) < 3:
                                    print(f"🔎 檢查交易: From={from_addr[:10]}... To={to_addr[:10]}...")
                                
                                if (from_addr == TETHER_MULTISIG.lower() and 
                                    to_addr == TETHER_TREASURY.lower()):
                                    value = int(tx.get('value', '0'))
                                    usdt_amount = value / 1_000_000
                                    timestamp = int(tx.get('timeStamp', '0'))
                                    dt = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8)))
                                    time_str = dt.strftime('%Y-%m-%d %H:%M')
                                    print(f"✅ 找到鑄造 #{len(mints)+1}: {usdt_amount:,.0f} USDT at {time_str}")
                                    mints.append(tx)
                                    
                                    if len(mints) >= limit:
                                        break
                            
                            print(f"🎯 總共找到 {len(mints)} 筆從 Multisig 到 Treasury 的鑄造")
                            
                            if not mints:
                                print("⚠️ 未找到任何鑄造記錄")
                                print("📋 檢查前幾筆交易的來源地址:")
                                for i, tx in enumerate(result[:5], 1):
                                    from_addr = tx.get('from', '')
                                    to_addr = tx.get('to', '')
                                    print(f"   {i}. From: {from_addr[:16]}... To: {to_addr[:16]}...")
                            
                            return mints
                        elif data.get('status') == '0':
                            print(f"⚠️ API 返回狀態 0: {data.get('message')}")
                            result_text = data.get('result', '')
                            if 'rate limit' in str(result_text).lower() or 'rate limit' in str(data.get('message', '')).lower():
                                print("⚠️ 可能遇到 API 速率限制，請稍後再試")
                        else:
                            print(f"❌ API 回應異常: {data}")
                    else:
                        print(f"❌ HTTP 錯誤: {resp.status}")
                        error_text = await resp.text()
                        print(f"錯誤內容: {error_text[:300]}")
            except Exception as e:
                print(f"❌ 獲取最近鑄造錯誤: {e}")
                import traceback
                traceback.print_exc()
        
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

async def setup_commands(application: Application):
    commands = [
        BotCommand("start", "🤖 啟動機器人"),
        BotCommand("list", "🐋 查看追蹤列表"),
        BotCommand("whalecheck", "🐋 查看特定巨鯨"),
        BotCommand("allwhale", "🐋 查看所有巨鯨持倉"),
        BotCommand("history", "📜 查看巨鯨歷史紀錄"),
        BotCommand("checktether", "💵 查看 Tether 鑄造狀態"),
        BotCommand("tetherhistory", "📋 查看 Tether 轉帳紀錄"),
        BotCommand("test", "🔧 測試API連接"),
    ]
    await application.bot.set_my_commands(commands)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        "🔧 <b>系統功能:</b>\n"
        "/test - 測試API連接\n\n"
        "📢 <b>自動通知:</b>\n"
        "• 巨鯨開倉/平倉/加減倉\n"
        "• Tether 鑄造事件\n"
        "• 每30分鐘定時更新",
        parse_mode='HTML'
    )

async def test_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 正在測試API連接...")
    
    results = []
    results.append(f"📝 TELEGRAM_TOKEN: {'✅ 已設置' if TELEGRAM_TOKEN else '❌ 未設置'}")
    results.append(f"🌐 HYPERLIQUID_API: {'✅ 已設置' if HYPERLIQUID_API else '❌ 未設置'}")
    results.append(f"🔑 ETHERSCAN_API_KEY: {'✅ 已設置' if ETHERSCAN_API_KEY else '❌ 未設置'}")
    
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
            block_num = await tether_monitor.get_latest_block()
            if block_num:
                etherscan_test = f"✅ 連接成功 (V2 API, 區塊: {block_num:,})"
            else:
                etherscan_test = "❌ 無法獲取區塊號"
        except Exception as e:
            etherscan_test = f"❌ {str(e)[:30]}"
    else:
        etherscan_test = "❌ 未設置 API Key"
    
    results.append(f"🔗 Etherscan API: {etherscan_test}")
    
    result_text = "📊 <b>API 測試結果:</b>\n\n" + "\n".join(results)
    
    issues = [r for r in results if '❌' in r]
    if issues:
        result_text += "\n\n⚠️ <b>發現問題:</b>\n" + "\n".join(issues)
    else:
        result_text += "\n\n✅ 所有API運作正常!"
    
    await update.message.reply_text(result_text, parse_mode='HTML')

async def check_tether(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def tether_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tether 轉帳歷史查詢"""
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

async def whale_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tracker.whales:
        await update.message.reply_text("📭 目前沒有追蹤任何巨鯨")
        return
    
    keyboard = get_whale_list_keyboard("check")
    await update.message.reply_text("請選擇要查看的巨鯨:", reply_markup=keyboard)

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tracker.whales:
        await update.message.reply_text("📭 目前沒有追蹤任何巨鯨")
        return
    
    keyboard = get_whale_list_keyboard("history")
    await update.message.reply_text("請選擇要查看歷史的巨鯨:", reply_markup=keyboard)

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

async def auto_update(context: ContextTypes.DEFAULT_TYPE):
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

async def tether_update(context: ContextTypes.DEFAULT_TYPE):
    if not tracker.subscribed_chats or not ETHERSCAN_API_KEY:
        return
    
    try:
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
        print(f"Tether 更新錯誤: {e}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"更新 {update} 導致錯誤 {context.error}")
    
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
    print("📋 設置機器人命令...")
    await setup_commands(application)
    print("✅ 機器人命令設置完成")

def main():
    print("🤖 啟動中...")
    print(f"Token: {TELEGRAM_TOKEN[:10]}...")
    print(f"📡 使用 Etherscan V2 API: {ETHERSCAN_API}")
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_health_server())
    
    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test_api))
    application.add_handler(CommandHandler("list", list_whales))
    application.add_handler(CommandHandler("whalecheck", whale_check))
    application.add_handler(CommandHandler("allwhale", show_all_positions))
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("checktether", check_tether))
    application.add_handler(CommandHandler("tetherhistory", tether_history_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    
    application.add_error_handler(error_handler)
    
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(auto_update, interval=60, first=10)
        job_queue.run_repeating(tether_update, interval=300, first=30)
        print("✅ 定時任務已設置")
        print("   - 巨鯨監控: 每 60 秒")
        print("   - Tether 監控: 每 300 秒 (5 分鐘)")
    else:
        print("⚠️ Job queue 未啟用")
    
    print("✅ 已啟動")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()