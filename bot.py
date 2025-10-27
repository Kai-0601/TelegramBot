import os
import sys
import json
import asyncio
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

WHALES_FILE = os.path.join(os.path.dirname(__file__), 'whales.json')

if not TELEGRAM_TOKEN:
    raise ValueError("請在 .env 文件中設置 TELEGRAM_TOKEN")

ADD_ADDRESS, ADD_NAME = range(2)
BATCH_ADD_DATA = range(1)

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

tracker = WhaleTracker()

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

async def whale_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not tracker.whales:
        await update.message.reply_text("📭 目前沒有追蹤任何巨鯨")
        return
    
    keyboard = get_whale_list_keyboard("check")
    await update.message.reply_text("請選擇要查看的巨鯨:", reply_markup=keyboard)

async def batch_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 請輸入巨鯨資料，每行一個，格式:\n"
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

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"Update {update} caused error {context.error}")
    
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text(
                "❌ 發生錯誤，請稍後再試或聯繫管理員"
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
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test_api))
    application.add_handler(add_handler)
    application.add_handler(CommandHandler("remove", remove_whale))
    application.add_handler(batch_add_handler)
    application.add_handler(CommandHandler("batchremove", batch_remove))
    application.add_handler(CommandHandler("list", list_whales))
    application.add_handler(CommandHandler("whalecheck", whale_check))
    application.add_handler(CommandHandler("allwhale", show_all_positions))
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