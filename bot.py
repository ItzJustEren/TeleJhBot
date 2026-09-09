import asyncio
import random
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# ---------- توکن خود را وارد کنید ----------
TOKEN = "YOUR_BOT_TOKEN_HERE"

bot = Bot(token=TOKEN)
storage = MemoryStorage()  # برای ۱ گیگ رم کافیه
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

# ---------- دیکشنری مرکزی ذخیره‌سازی داده‌های بازی ----------
# ساختار دقیق:
# games[chat_id] = {
#     'players': [user_id, ...],
#     'starter': user_id,
#     'join_timer': job_object,
#     'status': 'idle' | 'waiting' | 'playing',
#     'list_numbers': [],  # لیست اعداد و سوالات
#     'current_round': {
#         'question_number': None,
#         'question_text': None,
#         'asker_id': None,  # کسی که سوال می‌پرسه
#         'answerer_id': None,  # کسی که باید جواب بده
#         'players_answered': [],  # لیست user_idهایی که ریپلای زدن
#         'answer_timer': None,
#         'punishment_timer': None,
#         'winner_id': None,  # برنده رای‌گیری
#         'punisher_id': None,  # کسی که چالش رو تعیین میکنه
#         'punishment_text': None,
#     },
#     'votes': {},  # {user_id: count}
#     'voted_users': [],  # لیست user_idهایی که رای دادن
#     'game_over': False,
# }
games: Dict[int, dict] = {}

# ---------- تعریف Stateهای مختلف (FSM) ----------
class GameStates(StatesGroup):
    waiting_for_list = State()          # منتظر لیست از استارت‌کننده
    waiting_for_question_number = State()  # منتظر عدد سوال از پرسنده
    waiting_for_answers = State()       # منتظر پاسخ‌ها با ریپلای (تایمر ۶۰ ثانیه)
    waiting_for_vote = State()          # در حال رای‌گیری
    waiting_for_punishment = State()    # منتظر چالش از برنده
    waiting_for_punishment_confirm = State()  # منتظر تایید انجام مجازات

# ---------- توابع کمکی مدیریت بازی ----------

def get_game(chat_id: int) -> dict:
    """دریافت دیکشنری بازی برای چت، اگر نبود ایجاد کن"""
    if chat_id not in games:
        games[chat_id] = {
            'players': [],
            'starter': None,
            'join_timer': None,
            'status': 'idle',
            'list_numbers': [],
            'current_round': {},
            'votes': {},
            'voted_users': [],
            'game_over': False,
        }
    return games[chat_id]

def cancel_join_timer(chat_id: int):
    """لغو تایمر انتظار برای شروع بازی"""
    game = get_game(chat_id)
    if game['join_timer']:
        try:
            game['join_timer'].remove()
        except:
            pass
        game['join_timer'] = None

def cancel_answer_timer(chat_id: int):
    """لغو تایمر ۶۰ ثانیه‌ی پاسخ‌دهی"""
    game = get_game(chat_id)
    if game['current_round'].get('answer_timer'):
        try:
            game['current_round']['answer_timer'].remove()
        except:
            pass
        game['current_round']['answer_timer'] = None

def cancel_punishment_timer(chat_id: int):
    """لغو تایمر ۶ دقیقه‌ای انجام مجازات"""
    game = get_game(chat_id)
    if game['current_round'].get('punishment_timer'):
        try:
            game['current_round']['punishment_timer'].remove()
        except:
            pass
        game['current_round']['punishment_timer'] = None

async def start_voting(chat_id: int, loser_id: int):
    """شروع رای‌گیری برای تعیین مجازات‌دهنده"""
    game = get_game(chat_id)
    players = game['players']
    # حذف خود بازنده از لیست رای‌دهندگان بالقوه
    voters = [p for p in players if p != loser_id]
    
    if not voters:
        await bot.send_message(chat_id, "فقط خودت موندی دیگه! بازی تموم شد.")
        game['status'] = 'idle'
        return
    
    # ساخت دکمه‌های شیشه‌ای برای هر بازیکن (به جز خودش)
    buttons = []
    for voter in voters:
        try:
            user = await bot.get_chat(voter)
            name = user.first_name or "کاربر"
            buttons.append([InlineKeyboardButton(text=name, callback_data=f"vote_{voter}")])
        except:
            continue
    
    if not buttons:
        await bot.send_message(chat_id, "هیچکس برای رای‌گیری نیست!")
        game['status'] = 'idle'
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    # ارسال پیام رای‌گیری
    msg = await bot.send_message(
        chat_id,
        f"خب کاربر @{loser_id} تو نفرستادی جوابتو وقتشه مجازات شی\nبچها رای بدین کی مجازاتشو انتخاب کنه بیشترین رای انتخاب میشه",
        reply_markup=keyboard
    )
    
    # ذخیره اطلاعات رای‌گیری
    game['current_round']['vote_message_id'] = msg.message_id
    game['current_round']['loser_id'] = loser_id
    game['votes'] = {voter: 0 for voter in voters}
    game['voted_users'] = []
    game['status'] = 'voting'

    # تنظیم تایمر ۳۰ ثانیه برای پایان رای‌گیری (اختیاری)
    # (در صورت تمایل فعال کنید)
    # scheduler.add_job(
    #     finalize_voting,
    #     trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=30)),
    #     args=[chat_id]
    # )

async def finalize_voting(chat_id: int):
    """پایان رای‌گیری و اعلام برنده"""
    game = get_game(chat_id)
    if game['status'] != 'voting':
        return
    
    votes = game['votes']
    if not votes:
        await bot.send_message(chat_id, "هیچ رایی ثبت نشد! بازی ادامه پیدا میکنه.")
        game['status'] = 'playing'
        return
    
    # پیدا کردن بیشترین رای
    max_votes = max(votes.values())
    winners = [uid for uid, count in votes.items() if count == max_votes]
    
    if len(winners) > 1:
        winner_id = random.choice(winners)
    else:
        winner_id = winners[0]
    
    loser_id = game['current_round']['loser_id']
    game['current_round']['winner_id'] = winner_id
    game['current_round']['punisher_id'] = winner_id
    
    # ارسال پیام برنده
    await bot.send_message(
        chat_id,
        f"یوهو مثل اینکه انتخاب شد داداشمون @{winner_id} به این یارو @{loser_id} چالش درون تلگرامی میده و باید انجام بده"
    )
    
    # تغییر وضعیت به انتظار چالش از برنده
    game['status'] = 'waiting_for_punishment'
    # Set FSM state for the winner (we'll handle via callback or message)
    # Since we use states per user, we need to set state for winner
    # But easier: we check in message handler if user is the winner and replied to the correct message

async def start_new_round(chat_id: int):
    """شروع دور جدید: انتخاب تصادفی پرسنده و سوال"""
    game = get_game(chat_id)
    players = game['players']
    if len(players) < 3:
        await bot.send_message(chat_id, "تعداد بازیکن‌ها کمتر از ۳ نفر شد! بازی متوقف شد.")
        game['status'] = 'idle'
        return
    
    # انتخاب تصادفی پرسنده و پاسخ‌دهنده (متفاوت)
    asker_id = random.choice(players)
    answerer_id = random.choice([p for p in players if p != asker_id])
    
    # انتخاب تصادفی یک سوال از لیست
    if not game['list_numbers']:
        await bot.send_message(chat_id, "لیست سوالات خالی‌ست! بازی ادامه نمی‌یابد.")
        game['status'] = 'idle'
        return
    
    number, question = random.choice(game['list_numbers'])
    
    # ذخیره در دور جاری
    game['current_round']['asker_id'] = asker_id
    game['current_round']['answerer_id'] = answerer_id
    game['current_round']['question_number'] = number
    game['current_round']['question_text'] = question
    game['current_round']['players_answered'] = []
    game['current_round']['winner_id'] = None
    game['current_round']['punisher_id'] = None
    game['current_round']['punishment_text'] = None
    game['status'] = 'playing'
    
    # ارسال پیام به پرسنده
    await bot.send_message(
        chat_id,
        f"اوکی پسر وقتشه سوال بپرسی بدو بپرس\n@{asker_id}\nتو فقط باید عدد سوالت رو بفرستی"
    )
    
    # تنظیم state برای پرسنده
    # ما از FSM استفاده میکنیم، ولی اینجا state رو برای کاربر خاصی تنظیم نمیکنیم
    # چون پیام می‌تونه از هر کسی بیاد، توی هندلر چک میکنیم که فرستنده = asker_id باشه

async def handle_answer_timeout(chat_id: int):
    """زمان پاسخ‌دهی ۶۰ ثانیه تمام شد"""
    game = get_game(chat_id)
    if game['status'] != 'playing' or not game['current_round'].get('answerer_id'):
        return
    
    answerer_id = game['current_round']['answerer_id']
    await bot.send_message(chat_id, f"Times up buddy LoL")
    await bot.send_message(
        chat_id,
        f"خب کاربر @{answerer_id} تو نفرستادی جوابتو وقتشه مجازات شی"
    )
    
    # شروع رای‌گیری
    await start_voting(chat_id, answerer_id)

async def handle_punishment_timeout(chat_id: int):
    """۶ دقیقه برای انجام مجازات تمام شد"""
    game = get_game(chat_id)
    if game['status'] != 'waiting_for_punishment_confirm':
        return
    
    game['game_over'] = True
    game['status'] = 'idle'
    await bot.send_message(
        chat_id,
        "این یارو بی‌وجود بود انجام نداد بازی تموم شد حالا می‌تونید بازی جدید شروع کنید"
    )

# ---------- هندلرهای اصلی ----------

@dp.message(Command("startJHGame"))
async def cmd_start_game(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    game = get_game(chat_id)
    
    # اگر بازی در حال انجامه، اجازه نده دوباره شروع کنه
    if game['status'] != 'idle' and not game['game_over']:
        await message.reply("بازی در حال انجامه! صبر کن تموم بشه.")
        return
    
    # ریست بازی
    games[chat_id] = {
        'players': [user_id],
        'starter': user_id,
        'join_timer': None,
        'status': 'waiting',
        'list_numbers': [],
        'current_round': {},
        'votes': {},
        'voted_users': [],
        'game_over': False,
    }
    game = games[chat_id]
    
    # دکمه پیوستن
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="پیوستن به بازی 🎮", callback_data="join_game")]
        ]
    )
    
    await message.reply(
        "بازی جرئت یا حقیقت شروع شد!\nبرای پیوستن دکمه زیر رو بزن.",
        reply_markup=keyboard
    )
    
    # تنظیم تایمر ۶۰ ثانیه برای شروع خودکار (با ریست در صورت پیوستن افراد جدید)
    schedule_game_start(chat_id)

def schedule_game_start(chat_id: int):
    """تنظیم یا ریست تایمر ۶۰ ثانیه برای شروع بازی"""
    cancel_join_timer(chat_id)
    game = get_game(chat_id)
    
    # تابعی که بعد از ۶۰ ثانیه اجرا میشه
    async def start_game():
        game = get_game(chat_id)
        if game['status'] != 'waiting':
            return
        if len(game['players']) < 3:
            await bot.send_message(chat_id, "تعداد بازیکن‌ها کمتر از ۳ نفر هست. بازی شروع نشد.")
            game['status'] = 'idle'
            return
        
        # شروع بازی
        game['status'] = 'playing'
        await bot.send_message(chat_id, "بازی شروع شد!")
        
        # از استارت‌کننده لیست بخواه
        starter_id = game['starter']
        game['status'] = 'waiting_for_list'
        await bot.send_message(
            chat_id,
            f"اونر و شروع کننده‌ی بازی رو همین پیام ریپلای کن لیستو بفرست\n@{starter_id}"
        )
    
    # اضافه کردن job به scheduler
    run_time = datetime.now() + timedelta(seconds=60)
    job = scheduler.add_job(
        start_game,
        trigger=DateTrigger(run_date=run_time),
        id=f"start_game_{chat_id}",
        replace_existing=True
    )
    game['join_timer'] = job

@dp.callback_query(lambda c: c.data == "join_game")
async def join_game_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    
    game = get_game(chat_id)
    
    if game['status'] != 'waiting':
        await callback.answer("بازی در حال انجامه یا شروع شده!")
        return
    
    if user_id in game['players']:
        await callback.answer("قبلاً پیوستی!")
        return
    
    # اضافه کردن بازیکن
    game['players'].append(user_id)
    
    # ارسال پیام خوش‌آمدگویی در پیوی
    try:
        await bot.send_message(user_id, "به بازی خوش اومدی جیگر")
    except:
        pass
    
    await callback.answer("به بازی پیوستی!")
    
    # ریست تایمر ۶۰ ثانیه
    schedule_game_start(chat_id)
    
    # آپدیت پیام (اختیاری - می‌تونیم تعداد بازیکن‌ها رو نشون بدیم)
    await callback.message.edit_text(
        f"بازی جرئت یا حقیقت شروع شد!\nتعداد بازیکن‌ها: {len(game['players'])} نفر\nبرای پیوستن دکمه زیر رو بزن.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="پیوستن به بازی 🎮", callback_data="join_game")]
            ]
        )
    )

@dp.message(StateFilter(GameStates.waiting_for_list))
async def receive_list(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    user_id = message.from_user.id
    game = get_game(chat_id)
    
    # فقط استارت‌کننده می‌تونه لیست بده
    if user_id != game['starter']:
        await message.reply("فقط شروع‌کننده بازی می‌تونه لیست بفرسته.")
        return
    
    # بررسی اینکه ریپلای به پیام درخواست لیست هست
    if not message.reply_to_message or "اونر و شروع کننده‌ی بازی رو همین پیام ریپلای کن" not in message.reply_to_message.text:
        await message.reply("روی پیام درخواست لیست ریپلای کن!")
        return
    
    # پردازش لیست (فرمت: عدد-متن سوال)
    lines = message.text.strip().split('\n')
    numbers_list = []
    for line in lines:
        match = re.match(r'^(\d+)\s*[-–]\s*(.+)$', line.strip())
        if match:
            num = int(match.group(1))
            text = match.group(2).strip()
            numbers_list.append((num, text))
    
    if len(numbers_list) < 3:
        await message.reply("حداقل ۳ سوال با فرمت 'عدد-متن' وارد کن. مثال:\n1-چی؟\n2-کجا؟\n3-کی؟")
        return
    
    game['list_numbers'] = numbers_list
    await state.clear()
    await message.reply(f"لیست با {len(numbers_list)} سوال ذخیره شد!")
    
    # شروع دور اول
    await start_new_round(chat_id)

@dp.message(StateFilter(GameStates.waiting_for_question_number))
async def receive_question_number(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    user_id = message.from_user.id
    game = get_game(chat_id)
    
    # چک کن که فرستنده همان پرسنده هست
    if game['current_round'].get('asker_id') != user_id:
        await message.reply("نوبت تو نیست!")
        return
    
    # بررسی عدد
    try:
        number = int(message.text.strip())
    except:
        await message.reply("فقط عدد بفرست!")
        return
    
    # پیدا کردن سوال مربوط به عدد
    question_text = None
    for num, text in game['list_numbers']:
        if num == number:
            question_text = text
            break
    
    if not question_text:
        await message.reply(f"سوال با عدد {number} پیدا نشد! از لیست موجود استفاده کن.")
        return
    
    # ذخیره سوال انتخاب‌شده
    game['current_round']['question_number'] = number
    game['current_round']['question_text'] = question_text
    
    # اعلام سوال به همه
    await bot.send_message(
        chat_id,
        f"سوال انتخاب شد: {question_text}\n\nدر انتظار پاسخ شرکت کننده ها"
    )
    
    # شروع شمارش پاسخ‌ها (۶۰ ثانیه)
    game['current_round']['players_answered'] = []
    game['status'] = 'waiting_for_answers'
    await state.set_state(GameStates.waiting_for_answers)
    
    # تنظیم تایمر ۶۰ ثانیه
    cancel_answer_timer(chat_id)
    run_time = datetime.now() + timedelta(seconds=60)
    job = scheduler.add_job(
        handle_answer_timeout,
        trigger=DateTrigger(run_date=run_time),
        args=[chat_id],
        id=f"answer_timeout_{chat_id}",
        replace_existing=True
    )
    game['current_round']['answer_timer'] = job

@dp.message(StateFilter(GameStates.waiting_for_answers))
async def receive_answer(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    user_id = message.from_user.id
    game = get_game(chat_id)
    
    # فقط بازیکنانی که در بازی هستند می‌تونن پاسخ بدن
    if user_id not in game['players']:
        await message.reply("تو در این بازی نیستی!")
        return
    
    # باید ریپلای به پیام سوال باشه
    if not message.reply_to_message or "سوال انتخاب شد" not in message.reply_to_message.text:
        await message.reply("روی پیام سوال ریپلای کن!")
        return
    
    # بررسی اینکه قبلاً پاسخ نداده
    if user_id in game['current_round']['players_answered']:
        await message.reply("قبلاً پاسخ دادی!")
        return
    
    # ثبت پاسخ
    game['current_round']['players_answered'].append(user_id)
    
    # نمایش تعداد پاسخ‌دهندگان
    total_players = len(game['players'])
    answered = len(game['current_round']['players_answered'])
    await message.reply(f"{answered}/{total_players} نفر پاسخ دادن.")
    
    # اگر همه پاسخ دادن، تایمر رو لغو کن و برو مرحله بعد
    if answered >= total_players:
        cancel_answer_timer(chat_id)
        await state.clear()
        await bot.send_message(chat_id, "همه پاسخ دادن! دور بعد.")
        # شروع دور جدید (می‌تونیم از استارت‌کننده دوباره لیست بخواهیم یا ادامه بدیم)
        # طبق سناریو، هر دست لیست جدید می‌خواد، پس دوباره از استارت‌کننده لیست می‌خوایم
        game['status'] = 'waiting_for_list'
        await bot.send_message(
            chat_id,
            f"اونر و شروع کننده‌ی بازی رو همین پیام ریپلای کن لیستو بفرست\n@{game['starter']}"
        )

# ---------- هندلرهای رای‌گیری ----------

@dp.callback_query(lambda c: c.data and c.data.startswith("vote_"))
async def vote_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    voted_for_id = int(callback.data.split("_")[1])
    
    game = get_game(chat_id)
    
    if game['status'] != 'voting':
        await callback.answer("رای‌گیری تموم شده!")
        return
    
    # چک کن که خودش به خودش رای نده
    if user_id == voted_for_id:
        await callback.answer("مشتی تو نمیتونی به خودت رای بدی زجه نزن L")
        return
    
    # چک کن که قبلاً رای نداده
    if user_id in game['voted_users']:
        await callback.answer("قبلاً رای دادی!")
        return
    
    # ثبت رای
    game['votes'][voted_for_id] = game['votes'].get(voted_for_id, 0) + 1
    game['voted_users'].append(user_id)
    
    await callback.answer(f"به {voted_for_id} رای دادی!")
    
    # اگر همه رای دادن، رای‌گیری رو نهایی کن
    voters = [p for p in game['players'] if p != game['current_round']['loser_id']]
    if len(game['voted_users']) >= len(voters):
        await finalize_voting(chat_id)

# ---------- هندلر دریافت مجازات (چالش) از برنده ----------

@dp.message(StateFilter(GameStates.waiting_for_punishment))
async def receive_punishment(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    user_id = message.from_user.id
    game = get_game(chat_id)
    
    # فقط برنده (مجازات‌گر) می‌تونه چالش بفرسته
    if user_id != game['current_round']['winner_id']:
        await message.reply("سیکتیر تو قرار نیست مشخص کنی مجازاتو")
        return
    
    # باید ریپلای به پیام اعلام برنده باشه
    if not message.reply_to_message or "یوهو مثل اینکه انتخاب شد" not in message.reply_to_message.text:
        await message.reply("روی پیام اعلام برنده ریپلای کن!")
        return
    
    punishment_text = message.text.strip()
    if not punishment_text:
        await message.reply("چالش رو بنویس!")
        return
    
    # ذخیره چالش
    game['current_round']['punishment_text'] = punishment_text
    loser_id = game['current_round']['loser_id']
    
    # ارسال پیام نهایی با چالش و دکمه تایید
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="انجامش دادم ✅", callback_data="punishment_done")]
        ]
    )
    
    await bot.send_message(
        chat_id,
        f"چالش انتخابی: {punishment_text}\n\n@{loser_id} باید این چالش رو انجام بده.\n۶ دقیقه وقت داری!",
        reply_markup=keyboard
    )
    
    # تغییر وضعیت و تنظیم تایمر ۶ دقیقه
    game['status'] = 'waiting_for_punishment_confirm'
    await state.set_state(GameStates.waiting_for_punishment_confirm)
    
    cancel_punishment_timer(chat_id)
    run_time = datetime.now() + timedelta(minutes=6)
    job = scheduler.add_job(
        handle_punishment_timeout,
        trigger=DateTrigger(run_date=run_time),
        args=[chat_id],
        id=f"punishment_timeout_{chat_id}",
        replace_existing=True
    )
    game['current_round']['punishment_timer'] = job

@dp.callback_query(lambda c: c.data == "punishment_done")
async def punishment_done_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game = get_game(chat_id)
    
    if game['status'] != 'waiting_for_punishment_confirm':
        await callback.answer("این مرحله تموم شده!")
        return
    
    # فقط شخص مجازات‌شونده می‌تونه تایید کنه
    if user_id != game['current_round']['loser_id']:
        await callback.answer("فقط خود شخص مجازات‌شونده می‌تونه تایید کنه!")
        return
    
    # لغو تایمر
    cancel_punishment_timer(chat_id)
    game['status'] = 'playing'
    await callback.message.edit_text("✅ مجازات انجام شد! دور بعد.")
    await callback.answer("آفرین!")
    
    # شروع دور جدید (دوباره لیست از استارت‌کننده)
    game['status'] = 'waiting_for_list'
    await bot.send_message(
        chat_id,
        f"اونر و شروع کننده‌ی بازی رو همین پیام ریپلای کن لیستو بفرست\n@{game['starter']}"
    )

# ---------- هندلر پیش‌فرض برای خطاها ----------

@dp.message()
async def fallback_handler(message: types.Message, state: FSMContext):
    chat_id = message.chat.id
    game = get_game(chat_id)
    
    # اگر بازی در وضعیت waiting_for_list باشه و کاربر استارت‌کننده باشه
    if game['status'] == 'waiting_for_list' and message.from_user.id == game['starter']:
        # ریپلای به پیام درخواست لیست
        if message.reply_to_message and "اونر و شروع کننده" in message.reply_to_message.text:
            await state.set_state(GameStates.waiting_for_list)
            await receive_list(message, state)
            return
    
    # اگر بازی در وضعیت waiting_for_question_number باشه
    if game['status'] == 'playing' and game['current_round'].get('asker_id') == message.from_user.id:
        await state.set_state(GameStates.waiting_for_question_number)
        await receive_question_number(message, state)
        return
    
    # اگر بازی در وضعیت waiting_for_punishment باشه
    if game['status'] == 'waiting_for_punishment' and game['current_round'].get('winner_id') == message.from_user.id:
        await state.set_state(GameStates.waiting_for_punishment)
        await receive_punishment(message, state)
        return
    
    await message.reply("دستور نامعتبر. برای شروع بازی /startJHGame رو بزن.")

# ---------- اجرای ربات ----------

async def main():
    # حذف webhook (مطمئن میشیم که pollinگ کار کنه)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # شروع scheduler
    scheduler.start()
    
    # شروع polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
