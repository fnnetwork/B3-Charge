import aiohttp
import asyncio
import re
import random
import string
import os
import json
import logging
from flask import Flask, render_template
from datetime import datetime
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)
from colorama import Fore, init
from pymongo import MongoClient
from dateutil.relativedelta import relativedelta
from gate import PaymentGateway
import dateutil.parser
import dotenv

dotenv.load_dotenv()
init()
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class AdvancedCardChecker:
    def __init__(self):
        self.mongo_client = MongoClient(os.getenv('MONGODB_URI'))
        self.db = self.mongo_client['stripe_checker']
        self.users_col = self.db['users']
        self.keys_col = self.db['keys']
        self.admin_id = int(os.getenv('ADMIN_ID'))
        self.admin_username = os.getenv('ADMIN_USERNAME')
        self.bot_username = None
        self.active_tasks = {}
        self.user_stats = {}
        self.proxy_pool = []
        self.load_proxies()
        self.request_timeout = aiohttp.ClientTimeout(total=70)
        self.max_concurrent = 3
        self.payment_gateway = PaymentGateway()
        self.bin_cache = {}

    def create_banner(self):
        return f"""
{Fore.CYAN}
╔══════════════════════════════════════════════════════════════╗
║ 🔥 Cc CHECKER BOT                                            ║
╠══════════════════════════════════════════════════════════════╣
║ ➤ Admin ID: {self.admin_id:<15}                             ║
║ ➤ Bot Username: @{self.bot_username or 'Initializing...':<20}║
║ ➤ Admin Contact: https://t.me/{self.admin_username:<15}      ║
╚══════════════════════════════════════════════════════════════╝
{Fore.YELLOW}
✅ System Ready
{Fore.RESET}
"""

    async def post_init(self, application: Application):
        self.bot_username = application.bot.username
        print(self.create_banner())

    def load_proxies(self):
        if os.path.exists('proxies.txt'):
            with open('proxies.txt', 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(':')
                    if len(parts) == 4:
                        host, port, user, password = parts
                        proxy_url = f"http://{user}:{password}@{host}:{port}"
                        self.proxy_pool.append(proxy_url)
                    elif len(parts) == 2:
                        host, port = parts
                        proxy_url = f"http://{host}:{port}"
                        self.proxy_pool.append(proxy_url)
                    else:
                        logger.warning(f"Invalid proxy format: {line}")

    async def is_user_allowed(self, user_id):
        user = self.users_col.find_one({'user_id': str(user_id)})
        return bool(user and user.get('expires_at', datetime.now()) > datetime.now() or user_id == self.admin_id)

    async def check_subscription(self, func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            if not await self.is_user_allowed(user_id):
                await update.message.reply_text("⛔ Subscription expired!\nContact: https://t.me/{self.admin_username}")
                return
            return await func(update, context)
        return wrapper

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        keyboard = [
            [InlineKeyboardButton("📁 Upload Combo", callback_data='upload'),
             InlineKeyboardButton("🛑 Cancel Check", callback_data='cancel')],
            [InlineKeyboardButton("📊 Live Stats", callback_data='stats'),
             InlineKeyboardButton("❓ Help", callback_data='help')]
        ]
        await update.message.reply_text(
            "🔥 Welcome to FN Checker!\nUse /chk to check single CC:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def handle_admin_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id != self.admin_id:
            await update.message.reply_text("⛔ Command restricted to admin only!")
            return

        command = update.message.text.split()
        if len(command) < 2:
            await update.message.reply_text("❌ Usage: /allow <user_id> or /deny <user_id>")
            return

        action = command[0][1:]
        target_user = command[1]

        if action == 'allow':
            self.users_col.update_one(
                {'user_id': target_user},
                {'$set': {'expires_at': datetime.now() + relativedelta(days=30)}},
                upsert=True
            )
            await update.message.reply_text(f"✅ User {target_user} approved!")
        elif action == 'deny':
            self.users_col.delete_one({'user_id': target_user})
            await update.message.reply_text(f"❌ User {target_user} removed!")

    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        
        if query.data.startswith('allow_'):
            user_id = query.data.split('_')[1]
            self.users_col.update_one(
                {'user_id': user_id},
                {'$set': {'expires_at': datetime.now() + relativedelta(days=30)}},
                upsert=True
            )
            await query.edit_message_text(f"✅ User {user_id} approved!")
            await self.application.bot.send_message(
                chat_id=int(user_id),
                text="🎉 Your access has been approved!\nUse /start to begin checking cards."
            )
            
        elif query.data.startswith('deny_'):
            user_id = query.data.split('_')[1]
            self.users_col.delete_one({'user_id': user_id})
            await query.edit_message_text(f"❌ User {user_id} denied!")
            
        elif query.data == 'upload':
            if await self.is_user_allowed(query.from_user.id):
                await query.message.reply_text("📤 Please upload your combo file (.txt)")
            else:
                await query.message.reply_text("⛔ You are not authorized!")
                
        elif query.data == 'stats':
            await self.show_stats(update, context)
        elif query.data == 'help':
            await self.show_help(update, context)
        elif query.data == 'cancel':
            await self.stop_command(update, context)

    async def broadcast_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id != self.admin_id:
            await update.message.reply_text("⛔ Admin only command!")
            return
        
        message = ' '.join(context.args)
        if not message:
            await update.message.reply_text("Usage: /broadcast Your message here")
            return
        
        users = self.users_col.find()
        success = 0
        failed = 0
        for user in users:
            try:
                await self.application.bot.send_message(
                    chat_id=int(user['user_id']),
                    text=f"📢 Admin Broadcast:\n\n{message}"
                )
                success += 1
            except:
                failed += 1
        await update.message.reply_text(f"Broadcast complete:\n✅ Success: {success}\n❌ Failed: {failed}")

    async def genkey_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user.id != self.admin_id:
            await update.message.reply_text("⛔ Admin only command!")
            return
        
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /genkey <duration>\nDurations: 1d, 7d, 1m")
            return
        
        duration = context.args[0].lower()
        key_id = ''.join(random.choices(string.ascii_uppercase + string.digits, k=3))
        key_code = ''.join(random.choices(string.digits, k=2))
        key = f"FN-CHECKER-{key_id}-{key_code}"
        
        delta = self.parse_duration(duration)
        if not delta:
            await update.message.reply_text("Invalid duration! Use 1d, 7d, or 1m")
            return
        
        self.keys_col.insert_one({
            'key': key,
            'duration_days': delta.days,
            'used': False,
            'created_at': datetime.now()
        })
        
        await update.message.reply_text(f"🔑 New key generated:\n`{key}`\nDuration: {delta.days} days")

    def parse_duration(self, duration):
        if duration.endswith('d'):
            days = int(duration[:-1])
            return relativedelta(days=days)
        if duration.endswith('m'):
            months = int(duration[:-1])
            return relativedelta(months=months)
        return None

    async def redeem_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if not context.args:
            await update.message.reply_text("Usage: /redeem <key>")
            return
        
        key = context.args[0].upper()
        key_data = self.keys_col.find_one({'key': key, 'used': False})
        
        if not key_data:
            await update.message.reply_text("❌ Invalid or expired key!")
            return
        
        expires_at = datetime.now() + relativedelta(days=key_data['duration_days'])
        self.users_col.update_one(
            {'user_id': str(user.id)},
            {'$set': {
                'user_id': str(user.id),
                'username': user.username,
                'full_name': user.full_name,
                'expires_at': expires_at
            }},
            upsert=True
        )
        
        self.keys_col.update_one({'key': key}, {'$set': {'used': True}})
        await update.message.reply_text(
            f"🎉 Subscription activated until {expires_at.strftime('%Y-%m-%d')}!"
        )

    async def show_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        help_text = (
            "📜 <b>Bot Commands:</b>\n\n"
            "/start - Start the bot and show the main menu\n"
            "/chk <card> - Check a single card (e.g., /chk 4111111111111111|12|2025|123)\n"
            "/stop - Stop the current checking process\n"
            "/stats - Show your checking statistics\n"
            "/help - Show this help message\n\n"
            "📁 <b>How to Use:</b>\n"
            "1. Upload a combo file (.txt) or use /chk to check a single card.\n"
            "2. View live stats and progress during the check.\n"
            "3. Use /stop to cancel the process anytime."
        )
        await self.send_message(update, help_text)

    async def initialize_user_stats(self, user_id):
        if user_id not in self.user_stats:
            self.user_stats[user_id] = {
                'total': 0,
                'approved': 0,
                'declined': 0,
                'checked': 0,
                'start_time': datetime.now()
            }

    async def handle_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await self.is_user_allowed(user_id):
            await update.message.reply_text("⛔ Authorization required!")
            return

        if user_id in self.active_tasks:
            await update.message.reply_text("⚠️ Existing process found! Use /stop to cancel")
            return

        file = await update.message.document.get_file()
        filename = f"combos_{user_id}_{datetime.now().timestamp()}.txt"
        
        try:
            await file.download_to_drive(filename)
            with open(filename, 'r') as f:
                line_count = sum(1 for line in f if line.strip())
            
            if line_count > 4000:
                await update.message.reply_text("❌ Maximum 4000 cards allowed!")
                os.remove(filename)
                return
            
            await self.initialize_user_stats(user_id)
            self.active_tasks[user_id] = asyncio.create_task(
                self.process_combos(user_id, filename, update)
            )
            await update.message.reply_text(
                "✅ File received! Starting checking...\n"
                "⚡ Progress will be updated every 50 cards\n"
                "📈 Use /progress for live updates"
            )
        except Exception as e:
            logger.error(f"File error: {str(e)}")
            await update.message.reply_text("❌ File processing failed!")

    async def chk_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not await self.is_user_allowed(user_id):
            await update.message.reply_text("⛔ Authorization required!")
            return

        await self.initialize_user_stats(user_id)

        if not context.args:
            await update.message.reply_text("❌ Format: /chk 4111111111111111|12|2025|123")
            return

        combo = context.args[0]
        if len(combo.split("|")) != 4:
            await update.message.reply_text("❌ Invalid format! Use: 4111111111111111|MM|YYYY|CVV")
            return

        await update.message.reply_text("🔍 Checking card...")
        try:
            result = await self.process_line(user_id, combo, asyncio.Semaphore(1), update)
            if result:
                await update.message.reply_text(f"FN CHECKER")
            else:
                await update.message.reply_text(f"""
Declined ❌

Card: {combo}
Gateway: Stripe
Response: Card Declined

Proxy: [ LIVE ✅ ]
DEV: @FNxELECTRA
Bot: @FN_CHECKERR_BOT
""")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Check failed: {str(e)}")

    async def process_combos(self, user_id, filename, update):
        try:
            with open(filename, 'r') as f:
                combos = [line.strip() for line in f if line.strip()]
                self.user_stats[user_id] = {
                    'total': len(combos),
                    'approved': 0,
                    'declined': 0,
                    'checked': 0,
                    'start_time': datetime.now(),
                    'approved_ccs': []
                }
                
                semaphore = asyncio.Semaphore(self.max_concurrent)
                tasks = [self.process_line(user_id, combo, semaphore, update) for combo in combos]
                
                for future in asyncio.as_completed(tasks):
                    result = await future
                    self.user_stats[user_id]['checked'] += 1
                    if result:
                        self.user_stats[user_id]['approved'] += 1
                        self.user_stats[user_id]['approved_ccs'].append(result)
                    else:
                        self.user_stats[user_id]['declined'] += 1
                    
                    if self.user_stats[user_id]['checked'] % 50 == 0:
                        await self.send_progress_update(user_id, update)

                await self.send_report(user_id, update)
        except Exception as e:
            logger.error(f"Processing error: {str(e)}")
            await self.send_message(update, f"❌ Processing failed: {str(e)}")
        finally:
            if os.path.exists(filename):
                os.remove(filename)
            if user_id in self.active_tasks:
                del self.active_tasks[user_id]

    async def fetch_nonce(self, session, url, pattern, proxy=None):
        try:
            async with session.get(url, proxy=proxy) as response:
                html = await response.text()
                return re.search(pattern, html).group(1)
        except Exception as e:
            logger.error(f"Nonce fetch error: {str(e)}")
            return None

    async def fetch_bin_info(self, bin_number):
        try:
            if bin_number in self.bin_cache:
                return self.bin_cache[bin_number]
            
            async with aiohttp.ClientSession() as session:
                async with session.get(f'https://lookup.binlist.net/{bin_number}') as response:
                    if response.status == 200:
                        data = await response.json()
                        self.bin_cache[bin_number] = {
                            'scheme': data.get('scheme', 'N/A'),
                            'type': data.get('type', 'N/A'),
                            'brand': data.get('brand', 'N/A'),
                            'prepaid': data.get('prepaid', 'N/A'),
                            'country': data.get('country', {}).get('name', 'N/A'),
                            'bank': data.get('bank', {}).get('name', 'N/A')
                        }
                        return self.bin_cache[bin_number]
        except Exception as e:
            logger.error(f"BIN lookup error: {str(e)}")
        return None

    async def format_approval_message(self, combo, bin_info, check_time, user):
        bin_info = bin_info or {}
        return f"""
<b>Authorized✅</b>

[ϟ]CARD -» <code>{combo}</code>
[ϟ]STATUS -» Charged 1$
[ϟ]GATEWAY -» <code>Stripe</code>
<b>[ϟ]RESPONSE -»: <code>Charged Successfully</code></b>

━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━

[ϟ]BIN -» <code>{bin_info.get('scheme', 'N/A')} {bin_info.get('type', '')}</code>
[ϟ]BANK -» <code>{bin_info.get('bank', 'N/A')}</code>
<b>[ϟ]COUNTRY -» <code>{bin_info.get('country', 'N/A')}</code></b>

━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━

[⌬]TIME -» <code>{check_time:.2f}s</code>
<b>[⌬]PROXY -» [ LIVE ✅ ]</b>

━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━ ━

[⌬]CHECKED BY -» @{user.username if user.username else user.full_name}
[⌬]DEV -» https://t.me/{self.admin_username}
[み]Bot -» @FN_CHECKERR_BOT
"""

    async def process_line(self, user_id, combo, semaphore, update):
        async with semaphore:
            try:
                result = await self.payment_gateway.process_payment(
                    combo, 
                    self.proxy_pool
                )
                
                if result['status'] == 'approved':
                    user = update.effective_user
                    await self.send_approval(
                        update,
                        combo,
                        result['bin_info'],
                        result['check_time'],
                        user
                    )
                    return combo
                return None
            except Exception as e:
                logger.error(f"Processing error: {str(e)}")
                return None

    async def send_approval(self, update, combo, bin_info, check_time, user):
        message = await self.format_approval_message(combo, bin_info, check_time, user)
        try:
            await update.message.reply_text(
                message, 
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📊 View Stats", callback_data='stats'),
                     InlineKeyboardButton("🛑 Stop Check", callback_data='cancel')]
                ])
            )
        except Exception as e:
            logger.error(f"Failed to send approval: {str(e)}")

    async def send_progress_update(self, user_id, update):
        stats = self.user_stats[user_id]
        elapsed = datetime.now() - stats['start_time']
        progress = f"""
━━━━━━━━━━━━━━━━━━━━━━
[⌬] FN CHECKER LIVE PROGRESS
━━━━━━━━━━━━━━━━━━━━━━
[✪] Approved: {stats['approved']}
[✪] Declined: {stats['declined']}
[✪] Checked: {stats['checked']}/{stats['total']}
[✪] Total: {stats['total']}
[✪] Duration: {elapsed.seconds // 60}m {elapsed.seconds % 60}s
[✪] Avg Speed: {stats['total']/elapsed.seconds if elapsed.seconds else 0:.1f} c/s
[✪] Success Rate: {(stats['approved']/stats['total'])*100:.2f}%
━━━━━━━━━━━━━━━━━━━━━━
[み] DEV: @FNxELECTRA
━━━━━━━━━━━━━━━━━━━━━━"""
        await self.send_message(update, progress)

    async def generate_hits_file(self, approved_ccs, total_ccs):
        random_number = random.randint(0, 9999)
        filename = f"hits_FnChecker_{random_number:04d}.txt"
        
        header = f"""━━━━━━━━━━━━━━━━━━━━━━
[⌬] FN CHECKER HITS
━━━━━━━━━━━━━━━━━━━━━━
[✪] Approved: {len(approved_ccs)}
[✪] Total: {total_ccs}
━━━━━━━━━━━━━━━━━━━━━━
[み] DEV: @FNxELECTRA
━━━━━━━━━━━━━━━━━━━━━━

━━━━━━━━━━━━━━━━━━━━━━
FN CHECKER HITS
━━━━━━━━━━━━━━━━━━━━━━
"""
        
        cc_entries = "\n".join([f"Approved ✅ {cc}" for cc in approved_ccs])
        full_content = header + cc_entries
        
        with open(filename, "w", encoding="utf-8") as f:
            f.write(full_content)
        
        return filename

    async def send_report(self, user_id, update):
        stats = self.user_stats[user_id]
        elapsed = datetime.now() - stats['start_time']
        report = f"""
━━━━━━━━━━━━━━━━━━━━━━
[⌬] FN CHECKER HITS
━━━━━━━━━━━━━━━━━━━━━━
[✪] Approved: {stats['approved']}
[❌] Declined: {stats['declined']}
[✪] Total: {stats['total']}
[✪] Duration: {elapsed.seconds // 60}m {elapsed.seconds % 60}s
[✪] Avg Speed: {stats['total']/elapsed.seconds if elapsed.seconds else 0:.1f} c/s
[✪] Success Rate: {(stats['approved']/stats['total'])*100:.2f}%
━━━━━━━━━━━━━━━━━━━━━━
[み] DEV: @FNxELECTRA
━━━━━━━━━━━━━━━━━━━━━━"""
        
        try:
            hits_file = await self.generate_hits_file(stats['approved_ccs'], stats['total'])
            await update.message.reply_document(
                document=open(hits_file, 'rb'),
                caption="FN Checker Results Attached"
            )
            os.remove(hits_file)
        except Exception as e:
            logger.error(f"Failed to send hits file: {str(e)}")
        
        await self.send_message(update, report)
        del self.user_stats[user_id]

    async def show_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id not in self.user_stats:
            await self.send_message(update, "📊 No statistics available")
            return
            
        stats = self.user_stats[user_id]
        elapsed = datetime.now() - stats['start_time']
        message = f"""
━━━━━━━━━━━━━━━━━━━━━━
[⌬] FN CHECKER STATS
━━━━━━━━━━━━━━━━━━━━━━
[✪] Approved: {stats['approved']}
[❌] Declined: {stats['declined']}
[✪] Total: {stats['total']}
[✪] Duration: {elapsed.seconds // 60}m {elapsed.seconds % 60}s
[✪] Avg Speed: {stats['total']/elapsed.seconds if elapsed.seconds else 0:.1f} c/s
[✪] Success Rate: {(stats['approved']/stats['total'])*100:.2f}%
━━━━━━━━━━━━━━━━━━━━━━
[み] DEV: @FNxELECTRA
━━━━━━━━━━━━━━━━━━━━━━"""
        await self.send_message(update, message)

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if user_id in self.active_tasks:
            self.active_tasks[user_id].cancel()
            del self.active_tasks[user_id]
            await self.send_message(update, "⏹️ Process cancelled")
            if user_id in self.user_stats:
                del self.user_stats[user_id]
        else:
            await self.send_message(update, "⚠️ No active process")

    async def send_message(self, update, text):
        try:
            await update.message.reply_text(text, parse_mode='HTML')
        except:
            try:
                await update.callback_query.message.reply_text(text, parse_mode='HTML')
            except:
                logger.error("Failed to send message")

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logger.error(msg="Exception:", exc_info=context.error)
        await self.send_message(update, f"⚠️ System Error: {str(context.error)}")

def main():
    checker = AdvancedCardChecker()
    application = Application.builder().token(os.getenv('TELEGRAM_TOKEN')).post_init(checker.post_init).build()
    checker.application = application
    
    handlers = [
        CommandHandler('start', checker.start),
        CommandHandler('allow', checker.handle_admin_command),
        CommandHandler('deny', checker.handle_admin_command),
        CommandHandler('stop', checker.stop_command),
        CommandHandler('stats', checker.show_stats),
        CommandHandler('help', checker.show_help),
        CommandHandler('chk', checker.chk_command),
        CommandHandler('broadcast', checker.broadcast_command),
        CommandHandler('genkey', checker.genkey_command),
        CommandHandler('redeem', checker.redeem_command),
        MessageHandler(filters.Document.TXT, checker.handle_file),
        CallbackQueryHandler(checker.button_handler)
    ]
    
    for handler in handlers:
        application.add_handler(handler)

    application.add_error_handler(checker.error_handler)
    application.run_polling()

if __name__ == "__main__":
    main()