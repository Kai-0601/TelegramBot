import os
import sys
import json
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
HYPERLIQUID_API = os.getenv('HYPERLIQUID_API', 'https://api.hyperliquid.xyz')

WHALES_FILE = os.path.join(os.path.dirname(__file__), 'whales.json')

if not TELEGRAM_TOKEN:
    raise ValueError("請在 .env 文件中設置 TELEGRAM_TOKEN")

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
                print(f"Error fetching positions for {address}: {e}")
        return []
    
    async def fetch_user_fills(self, address: str) -> List[Dict]:
        """獲取用戶的交易歷史"""
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
                print(f"Error fetching fills for {address}: {e}")
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
        """檢測持倉變化並返回通知訊息"""
        notifications = []
        changes = {}
        
        # 建立新的持倉字典
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
        
        # 如果是第一次檢測,只記錄不通知
        if address not in self.last_positions:
            self.last_positions[address] = new_pos_dict
            return [], {}
        
        old_pos_dict = self.last_positions[address]
        
        # 檢測新開倉
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
        
        # 檢測平倉
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
        
        # 檢測加倉/減倉
        for coin in set(new_pos_dict.keys()) & set(old_pos_dict.keys()):
            old_margin = old_pos_dict[coin]['margin']
            new_margin = new_pos_dict[coin]['margin']
            margin_diff = new_margin - old_margin
            
            # 保證金變化超過10%
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
        
        # 更新記錄
        self.last_positions[address] = new_pos_dict
        
        return notifications, changes

tracker = WhaleTracker()

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
        BotCommand("test", "🔧 測試API連接"),
    ]
    await application.bot.set_my_commands(commands)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tracker.subscribed_chats.add(chat_id)
    
    await update.message.reply_text(
        "🤖 <b>Hyperliquid 巨鯨追蹤機器人</b>\n"
        "🧑  <b>作者：Kai0601</b>\n\n"
        "🐋 <b>巨鯨追蹤:</b>\n"
        "/list - 查看追蹤列表\n"
        "/whalecheck - 查看特定巨鯨\n"
        "/allwhale - 查看所有巨鯨持倉\n"
        "/history - 查看巨鯨歷史紀錄\n\n"
        "🔧 <b>系統功能:</b>\n"
        "/test - 測試API連接",
        parse_mode='HTML'
    )

async def test_api(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 正在測試API連接...")
    
    results = []
    results.append(f"📝 TELEGRAM_TOKEN: {'✅ 已設置' if TELEGRAM_TOKEN else '❌ 未設置'}")
    results.append(f"🌐 HYPERLIQUID_API: {'✅ 已設置' if HYPERLIQUID_API else '❌ 未設置'}")
    
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
    
    result_text = "📊 <b>API 測試結果:</b>\n\n" + "\n".join(results)
    
    issues = [r for r in results if '❌' in r]
    if issues:
        result_text += "\n\n⚠️ <b>發現問題:</b>\n" + "\n".join(issues)
    else:
        result_text += "\n\n✅ 所有API運作正常！"
    
    await update.message.reply_text(result_text, parse_mode='HTML')

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
        
        # 顯示歷史查詢選項
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
        
        # 根據篩選條件處理
        if filter_type == "buy":
            filtered_fills = [f for f in fills if f.get('side') == 'B']
            title = "買入紀錄"
        elif filter_type == "sell":
            filtered_fills = [f for f in fills if f.get('side') == 'A']
            title = "賣出紀錄"
        else:
            # 數量篩選
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
            
            # 計算 USDT 金額
            usdt_amount = px * sz
            
            # 轉換時間戳
            dt = datetime.fromtimestamp(time / 1000, timezone(timedelta(hours=8)))
            time_str = dt.strftime('%m-%d %H:%M')
            
            side_emoji = "🟢" if side == "B" else "🔴"
            side_text = "買入" if side == "B" else "賣出"
            
            text += f"{i}. {side_emoji} <b>{coin}</b> {side_text}\n"
            text += f"   價格: ${px:,.4f}\n"
            text += f"   數量: ${usdt_amount:,.2f} USDT\n"
            text += f"   時間: {time_str}\n\n"
            
            # 防止訊息過長
            if len(text) > 2550:
                text += f" 還有 {len(filtered_fills) - i} 筆紀錄,剩餘紀錄需自行查找"
                break
        
        await query.message.reply_text(text, parse_mode='HTML')
        await query.edit_message_text("✅ 已顯示歷史紀錄")
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
        
        # 檢測持倉變化
        notifications, changes = tracker.detect_position_changes(address, positions)
        
        # 如果有即時變化,立即發送通知
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
                        print(f"Error sending notification: {e}")
                
                await asyncio.sleep(1)
        
        # 每30分鐘的定時通知
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
                    print(f"Error sending message: {e}")
            
            await asyncio.sleep(1)

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Update {update} caused error {context.error}")
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ 發生錯誤,請稍後再試或聯繫管理員"
            )
    except Exception as e:
        print(f"Error sending error message: {e}")

async def health_check(request):
    return web.Response(text="✅ Telegram Bot is running!")

async def start_health_server():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port = int(os.environ.get('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"✅ HTTP health server started on port {port}")
    
    return site

async def post_init(application: Application):
    print("📋 Setting up bot commands...")
    await setup_commands(application)
    print("✅ Bot commands setup complete")

def main():
    print("🤖 啟動中...")
    print(f"Token: {TELEGRAM_TOKEN[:10]}...")
    
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
    application.add_handler(CallbackQueryHandler(button_callback))
    
    application.add_error_handler(error_handler)
    
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(auto_update, interval=60, first=10)
        print("✅ 定時任務已設置")
    else:
        print("⚠️ Job queue 未啟用")
    
    print("✅ 已啟動")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()