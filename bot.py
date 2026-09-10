import asyncio
import os
import random
import re
from datetime import datetime, timedelta
from typing import Dict, List

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# ---------- تنظیمات ----------
TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not TOKEN:
    raise ValueError("BOT_TOKEN تنظیم نشده! برو توی Railway Variables و مقدارش رو ست کن.")

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
scheduler = AsyncIOScheduler()

games: Dict[int, dict] = {}


class GameStates(StatesGroup):
    waiting_for_list = State()
    waiting_for_question = State()
    waiting_for_answers = State()
    voting = State()
    waiting_for_punishment = State()
    waiting_for_confirm = State()


# ---------- توابع کمکی ----------

def get_game(chat_id: int) -> dict:
    if chat_id not in games:
        games[chat_id] = {
            'players': [],
            'starter': None,
            'join_timer': None,
            'answer_timer': None,
            'punish_timer': None,
            'status': 'idle',
            'list_numbers': [],
            'round': {},
            'votes': {},
            'voted_users': [],
            'msg_id_list_request': None,
            'msg_id_question': None,
            'msg_id_winner': None,
            'msg_id_punish': None,
            'vote_msg_id': None,
        }
    return games[chat_id]


def cancel_job(job):
    if job:
        try:
            job.remove()
        except Exception:
            pass


def schedule(chat_id: int, key: str, seconds: int, func, *args):
    game = get_game(chat_id)
    cancel_job(game.get(key))
    job = scheduler.add_job(
        func,
        trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=seconds)),
        args=args,
        id=f"{key}_{chat_id}_{random.randint(1000, 9999)}",
        replace_existing=False
    )
    game[key] = job
    return job


# ---------- شروع بازی ----------

@dp.message(Command("startJHGame"))
async def cmd_start_game(message: types.Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    game = get_game(chat_id)
    if game['status'] not in ('idle', 'finished'):
        await message.reply("بازی در حال انجامه! صبر کن تموم بشه.")
        return

    cancel_job(game.get('join_timer'))
    cancel_job(game.get('answer_timer'))
    cancel_job(game.get('punish_timer'))

    games[chat_id] = {
        'players': [user_id],
        'starter': user_id,
        'join_timer': None,
        'answer_timer': None,
        'punish_timer': None,
        'status': 'waiting',
        'list_numbers': [],
        'round': {},
        'votes': {},
        'voted_users': [],
        'msg_id_list_request': None,
        'msg_id_question': None,
        'msg_id_winner': None,
        'msg_id_punish': None,
        'vote_msg_id': None,
    }

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="پیوستن", callback_data="join_game")]]
    )
    await message.reply(
        f"بازی شروع شد!\nتعداد بازیکن ها: 1\nحداقل 3 نفر لازمه.",
        reply_markup=keyboard
    )
    schedule(chat_id, 'join_timer', 60, start_game_after_timer, chat_id)


@dp.callback_query(F.data == "join_game")
async def join_game(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game = get_game(chat_id)

    if game['status'] != 'waiting':
        await callback.answer("بازی شروع شده یا تموم شده!")
        return

    if user_id in game['players']:
        await callback.answer("قبلا پیوستی!")
        return

    game['players'].append(user_id)

    try:
        await bot.send_message(user_id, "به بازی خوش اومدی جیگر")
    except Exception:
        pass

    await callback.answer("پیوستی!")

    schedule(chat_id, 'join_timer', 60, start_game_after_timer, chat_id)

    try:
        await callback.message.edit_text(
            f"بازی شروع شد!\nتعداد بازیکن ها: {len(game['players'])}\nحداقل 3 نفر لازمه.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="پیوستن", callback_data="join_game")]]
            )
        )
    except Exception:
        pass


async def start_game_after_timer(chat_id: int):
    game = get_game(chat_id)
    if game['status'] != 'waiting':
        return
    if len(game['players']) < 3:
        await bot.send_message(chat_id, "تعداد بازیکن ها کمتر از 3 نفر شد. بازی شروع نشد.")
        game['status'] = 'idle'
        return

    game['status'] = 'waiting_for_list'
    starter = game['starter']
    msg = await bot.send_message(
        chat_id,
        f"اونر و شروع کننده ی بازی رو همین پیام ریپلای کن لیستو بفرست\n@{starter}"
    )
    game['msg_id_list_request'] = msg.message_id


# ---------- دریافت لیست ----------

async def _handle_list(message: types.Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    game = get_game(chat_id)

    if game['status'] != 'waiting_for_list':
        return
    if user_id != game['starter']:
        return
    if not message.reply_to_message:
        return
    if message.reply_to_message.message_id != game.get('msg_id_list_request'):
        return

    lines = message.text.strip().split('\n')
    parsed = []
    for line in lines:
        m = re.match(r'^\s*(\d+)\s*[-–:.]\s*(.+)$', line.strip())
        if m:
            parsed.append((int(m.group(1)), m.group(2).strip()))

    if len(parsed) < 3:
        await message.reply("حداقل 3 سوال با فرمت 'عدد-متن' بفرست. مثال:\n1-چی؟\n2-کجا؟\n3-کی؟")
        return

    game['list_numbers'] = parsed
    game['status'] = 'playing'
    await message.reply(f"لیست با {len(parsed)} سوال ثبت شد!")
    await start_new_round(chat_id)


async def start_new_round(chat_id: int):
    game = get_game(chat_id)
    players = game['players']

    if len(players) < 3:
        await bot.send_message(chat_id, "تعداد بازیکن ها کمه. بازی متوقف شد.")
        game['status'] = 'idle'
        return

    if not game['list_numbers']:
        await bot.send_message(chat_id, "لیست سوالات خالیه!")
        game['status'] = 'idle'
        return

    asker_id = random.choice(players)
    answerer_id = random.choice([p for p in players if p != asker_id])

    game['round'] = {
        'asker_id': asker_id,
        'answerer_id': answerer_id,
        'answered': [],
        'question_text': None,
    }
    game['status'] = 'waiting_for_question'
    game['msg_id_question'] = None
    game['msg_id_winner'] = None
    game['msg_id_punish'] = None

    await bot.send_message(
        chat_id,
        f"اوکی پسر وقتشه سوال بپرسی بدو بپرس\n@{asker_id}\nتو فقط باید عدد سوالت رو بفرستی"
    )


# ---------- عدد سوال ----------

async def _handle_number(message: types.Message):
    chat_id = message.chat.id
    game = get_game(chat_id)

    if game['status'] != 'waiting_for_question':
        return
    if message.from_user.id != game['round'].get('asker_id'):
        return

    try:
        number = int(message.text.strip())
    except Exception:
        return

    question = None
    for num, text in game['list_numbers']:
        if num == number:
            question = text
            break

    if not question:
        await message.reply(f"سوال با عدد {number} پیدا نشد! از لیست موجود انتخاب کن.")
        return

    game['round']['question_text'] = question
    game['status'] = 'waiting_for_answers'

    msg = await bot.send_message(
        chat_id,
        f"سوال انتخاب شد: {question}\n\nدر انتظار پاسخ شرکت کننده ها"
    )
    game['msg_id_question'] = msg.message_id

    schedule(chat_id, 'answer_timer', 60, answer_timeout, chat_id)


# ---------- پاسخ با ریپلای ----------

async def _handle_answer(message: types.Message):
    chat_id = message.chat.id
    game = get_game(chat_id)
    user_id = message.from_user.id

    if game['status'] != 'waiting_for_answers':
        return
    if user_id not in game['players']:
        return
    if user_id in game['round'].get('answered', []):
        return

    game['round']['answered'].append(user_id)
    total = len(game['players'])
    got = len(game['round']['answered'])

    await message.reply(f"{got}/{total} نفر پاسخ دادن.")

    if got >= total:
        cancel_job(game.get('answer_timer'))
        await finish_round(chat_id)


async def finish_round(chat_id: int):
    game = get_game(chat_id)
    game['status'] = 'waiting_for_list'
    game['list_numbers'] = []
    game['round'] = {}

    starter = game['starter']
    msg = await bot.send_message(
        chat_id,
        f"اونر و شروع کننده ی بازی رو همین پیام ریپلای کن لیستو بفرست\n@{starter}"
    )
    game['msg_id_list_request'] = msg.message_id


# ---------- تایم اوت پاسخ ----------

async def answer_timeout(chat_id: int):
    game = get_game(chat_id)
    if game['status'] != 'waiting_for_answers':
        return

    loser_id = game['round']['answerer_id']
    await bot.send_message(chat_id, "Times up buddy LoL")
    await bot.send_message(
        chat_id,
        f"خب کاربر @{loser_id} تو نفرستادی جوابتو وقتشه مجازات شی\nبچها رای بدین کی مجازاتشو انتخاب کنه بیشترین رای انتخاب میشه"
    )

    game['status'] = 'voting'
    game['round']['loser_id'] = loser_id
    game['votes'] = {}
    game['voted_users'] = []

    await send_voting_buttons(chat_id, loser_id)


async def send_voting_buttons(chat_id: int, loser_id: int):
    game = get_game(chat_id)
    candidates = [p for p in game['players'] if p != loser_id]

    if not candidates:
        await bot.send_message(chat_id, "کسی برای رای دادن نمونده!")
        await end_game(chat_id)
        return

    buttons = []
    for c in candidates:
        buttons.append([InlineKeyboardButton(text=f"@{c}", callback_data=f"vote_{c}")])

    game['votes'] = {c: 0 for c in candidates}

    msg = await bot.send_message(
        chat_id,
        "برای انتخاب مجازات گر رای بدین:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    game['vote_msg_id'] = msg.message_id


@dp.callback_query(F.data.startswith("vote_"))
async def vote_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    try:
        voted_for = int(callback.data.split("_")[1])
    except Exception:
        return

    game = get_game(chat_id)

    if game['status'] != 'voting':
        await callback.answer("رای گیری تموم شده!")
        return

    loser_id = game['round'].get('loser_id')

    if user_id == voted_for:
        await callback.answer("مشتی تو نمیتونی به خودت رای بدی زجه نزن L", show_alert=True)
        return

    if user_id == loser_id:
        await callback.answer("تو مجازات شدی! حق رای نداری.", show_alert=True)
        return

    if user_id in game['voted_users']:
        await callback.answer("قبلا رای دادی!")
        return

    game['votes'][voted_for] = game['votes'].get(voted_for, 0) + 1
    game['voted_users'].append(user_id)

    await callback.answer("رای ثبت شد!")

    voters = [p for p in game['players'] if p != loser_id]
    if len(game['voted_users']) >= len(voters):
        await finalize_voting(chat_id)


async def finalize_voting(chat_id: int):
    game = get_game(chat_id)
    if game['status'] != 'voting':
        return

    votes = game['votes']
    if not votes:
        await bot.send_message(chat_id, "رایی ثبت نشد!")
        await end_game(chat_id)
        return

    max_votes = max(votes.values())
    winners = [uid for uid, c in votes.items() if c == max_votes]
    winner = random.choice(winners) if len(winners) > 1 else winners[0]

    loser_id = game['round']['loser_id']
    game['round']['winner_id'] = winner
    game['status'] = 'waiting_for_punishment'

    results_text = "\n".join([f"@{uid}: {c} رای" for uid, c in sorted(votes.items(), key=lambda x: -x[1])])
    await bot.send_message(chat_id, f"نتایج رای گیری:\n{results_text}")

    msg = await bot.send_message(
        chat_id,
        f"یوهو مثل اینکه انتخاب شد داداشمون @{winner} به این یارو @{loser_id} چالش درون تلگرامی میده و باید انجام بده"
    )
    game['msg_id_winner'] = msg.message_id


# ---------- ثبت چالش ----------

async def _handle_punishment(message: types.Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    game = get_game(chat_id)

    if game['status'] != 'waiting_for_punishment':
        return
    if user_id != game['round'].get('winner_id'):
        await message.reply("سیکتیر تو قرار نیست مشخص کنی مجازاتو")
        return
    if not message.reply_to_message:
        return
    if message.reply_to_message.message_id != game.get('msg_id_winner'):
        return

    challenge = message.text.strip()
    if not challenge:
        return

    game['round']['punishment_text'] = challenge
    game['status'] = 'waiting_for_confirm'
    loser_id = game['round']['loser_id']

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="انجامش دادم", callback_data="done_punish")]]
    )

    msg = await bot.send_message(
        chat_id,
        f"چالش انتخابی: {challenge}\n\n@{loser_id} باید این چالش رو انجام بده.\n6 دقیقه وقت داری!",
        reply_markup=keyboard
    )
    game['msg_id_punish'] = msg.message_id

    schedule(chat_id, 'punish_timer', 360, punishment_timeout, chat_id)


@dp.callback_query(F.data == "done_punish")
async def done_punish(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    user_id = callback.from_user.id
    game = get_game(chat_id)

    if game['status'] != 'waiting_for_confirm':
        await callback.answer("این مرحله تموم شده!")
        return

    if user_id != game['round'].get('loser_id'):
        await callback.answer("فقط خود شخص مجازات شونده میتونه تایید کنه!", show_alert=True)
        return

    cancel_job(game.get('punish_timer'))
    await callback.message.edit_text("مجازات انجام شد! دور بعد شروع میشه.")
    await callback.answer("آفرین!")

    await finish_round(chat_id)


async def punishment_timeout(chat_id: int):
    game = get_game(chat_id)
    if game['status'] != 'waiting_for_confirm':
        return
    await bot.send_message(
        chat_id,
        "این یارو بی وجود بود انجام نداد بازی تموم شد حالا میتونید بازی جدید شروع کنید"
    )
    await end_game(chat_id)


async def end_game(chat_id: int):
    game = get_game(chat_id)
    game['status'] = 'finished'
    cancel_job(game.get('join_timer'))
    cancel_job(game.get('answer_timer'))
    cancel_job(game.get('punish_timer'))


# ---------- هندلرهای عمومی ----------

@dp.message(F.reply_to_message)
async def reply_router(message: types.Message):
    chat_id = message.chat.id
    game = get_game(chat_id)
    replied_id = message.reply_to_message.message_id

    if replied_id == game.get('msg_id_list_request'):
        await _handle_list(message)
        return
    if replied_id == game.get('msg_id_question'):
        await _handle_answer(message)
        return
    if replied_id == game.get('msg_id_winner'):
        await _handle_punishment(message)
        return


@dp.message(F.text.regexp(r'^\d+$'))
async def number_router(message: types.Message):
    await _handle_number(message)


# ---------- اجرا ----------

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    scheduler.start()
    print("ربات روشن شد...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
