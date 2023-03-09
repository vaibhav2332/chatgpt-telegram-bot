import os
import logging
import shelve
import datetime
import time
import openai
from telegram.ext import Updater, MessageHandler, Filters, CommandHandler
from telegram.error import RetryAfter, NetworkError, TimedOut

ADMIN_ID = 71863318
MODEL = "gpt-3.5-turbo"
def PROMPT():
    s = "You are ChatGPT Telegram bot. ChatGPT is a large language model trained by OpenAI. This Telegram bot is developed by zzh whose username is zzh1996. Answer as concisely as possible. Knowledge cutoff: Sep 2021. Current Beijing Time: {current_time}"
    return s.replace('{current_time}', (datetime.datetime.utcnow() + datetime.timedelta(hours=8)).strftime('%Y-%m-%d %H:%M'))

openai.api_key = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def retry(max_retry=30, interval=10):
    def decorator(func):
        def new_func(*args, **kwargs):
            for _ in range(max_retry - 1):
                try:
                    return func(*args, **kwargs)
                except (RetryAfter, NetworkError, TimedOut) as e:
                    logging.exception(e)
                    time.sleep(interval)
            return func(*args, **kwargs)
        return new_func
    return decorator

def is_whitelist(chat_id):
    whitelist = db['whitelist']
    return chat_id in whitelist

def add_whitelist(chat_id):
    whitelist = db['whitelist']
    whitelist.add(chat_id)
    db['whitelist'] = whitelist

def del_whitelist(chat_id):
    whitelist = db['whitelist']
    whitelist.discard(chat_id)
    db['whitelist'] = whitelist

def get_whitelist():
    return db['whitelist']

def only_admin(func):
    def new_func(update, context):
        if update.message.from_user.id != ADMIN_ID:
            update.message.reply_text('Only admin can use this command')
            return
        func(update, context)
    return new_func

def only_private(func):
    def new_func(update, context):
        if update.effective_chat.id != update.message.from_user.id:
            update.message.reply_text('This command only works in private chat')
            return
        func(update, context)
    return new_func

def only_whitelist(func):
    def new_func(update, context):
        if not is_whitelist(update.effective_chat.id):
            if update.effective_chat.id == update.message.from_user.id:
                update.message.reply_text('This chat is not in whitelist')
            return
        func(update, context)
    return new_func

def completion(chat_history): # chat_history = [user, ai, user, ai, ..., user]
    assert len(chat_history) % 2 == 1
    messages=[{"role": "system", "content": PROMPT()}]
    roles = ["user", "assistant"]
    role_id = 0
    for msg in chat_history:
        messages.append({"role": roles[role_id], "content": msg})
        role_id = 1 - role_id
    logging.info('Request: %s', messages)
    stream = openai.ChatCompletion.create(model=MODEL, messages=messages, stream=True)
    for response in stream:
        logging.info('Response: %s', response)
        obj = response['choices'][0]
        if obj['finish_reason'] is not None:
            assert not obj['delta']
            if obj['finish_reason'] == 'length':
                yield ' [!长度超限]'
            return
        if 'role' in obj['delta']:
            if obj['delta']['role'] != 'assistant':
                raise ValueError("Role error")
        if 'content' in obj['delta']:
            yield obj['delta']['content']

def construct_chat_history(chat_id, msg_id):
    messages = []
    should_be_bot = False
    while True:
        key = repr((chat_id, msg_id))
        if key not in db:
            logging.error('History message not found')
            return
        is_bot, text, reply_id = db[key]
        if is_bot != should_be_bot:
            logging.error('Role does not match')
            return
        messages.append(text)
        should_be_bot = not should_be_bot
        if reply_id is None:
            break
        msg_id = reply_id
    if len(messages) % 2 != 1:
        logging.error('First message not from user')
        return
    return messages[::-1]

@only_admin
def add_whitelist_handler(update, context):
    if is_whitelist(update.effective_chat.id):
        update.message.reply_text('Already in whitelist')
        return
    add_whitelist(update.effective_chat.id)
    update.message.reply_text('Whitelist added')

@only_admin
def del_whitelist_handler(update, context):
    if not is_whitelist(update.effective_chat.id):
        update.message.reply_text('Not in whitelist')
        return
    del_whitelist(update.effective_chat.id)
    update.message.reply_text('Whitelist deleted')

@only_admin
@only_private
def get_whitelist_handler(update, context):
    update.message.reply_text(str(get_whitelist()))

@retry()
def reply_or_edit(update, reply, reply_msg):
    chat_id = update.effective_chat.id
    sender_id = update.message.from_user.id
    is_edit = reply_msg is not None
    if not is_edit:
        reply_msg = update.message.reply_text(reply, disable_web_page_preview=True)
    else:
        reply_msg = reply_msg.edit_text(reply, disable_web_page_preview=True)
    logging.info('Reply message: chat=%r, sender=%r, id=%r, is_edit=%r, reply=%r', chat_id, sender_id, reply_msg.message_id, is_edit, reply)
    return reply_msg

@only_whitelist
def reply_handler(update, context):
    chat_id = update.effective_chat.id
    sender_id = update.message.from_user.id
    msg_id = update.message.message_id
    text = update.message.text
    logging.info('New message: chat=%r, sender=%r, id=%r, msg=%r', chat_id, sender_id, msg_id, text)
    reply_to_message = update.message.reply_to_message
    reply_to_id = None
    if reply_to_message is not None and update.message.reply_to_message.from_user.id == bot_id: # user reply to bot message
        reply_to_id = reply_to_message.message_id
    elif text.startswith('$'): # new message
        if text.startswith('$'):
            text = text[1:]
    else: # not reply or new message to bot
        if update.effective_chat.id == update.message.from_user.id: # if in private chat, send hint
            update.message.reply_text('Please start a new conversation with $ or reply to a bot message')
        return
    db[repr((chat_id, msg_id))] = (False, text, reply_to_id)

    chat_history = construct_chat_history(chat_id, msg_id)
    if chat_history is None:
        update.message.reply_text(f"[!] Error: Can't fetch this conversation, please start a new one.")
        return

    reply_msg = None
    try:
        cnt = 0
        while True:
            try:
                stream = completion(chat_history)
                break
            except openai.OpenAIError as e:
                if e.http_status != 500:
                    raise
                cnt += 1
                if cnt == 3:
                    raise
        last_time = time.time()
        reply = ''
        last_sent_reply = None
        for delta in stream:
            print('delta', delta)
            reply += delta
            if time.time() - last_time >= 4 and reply != delta and reply != last_sent_reply:
                last_time = time.time()
                reply_msg = reply_or_edit(update, reply + ' [!正在生成]', reply_msg)
                last_sent_reply = reply
        reply_msg = reply_or_edit(update, reply, reply_msg)
    except openai.OpenAIError as e:
        logging.exception('OpenAI Error: %s', e)
        reply_or_edit(update, f'[!] OpenAI Error: {e}', reply_msg)
        return

    db[repr((chat_id, reply_msg.message_id))] = (True, reply, msg_id)

def ping(update, context):
    update.message.reply_text(f'chat_id={update.effective_chat.id} user_id={update.message.from_user.id} is_whitelisted={is_whitelist(update.effective_chat.id)}')

if __name__ == '__main__':
    logFormatter = logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s")

    rootLogger = logging.getLogger()
    rootLogger.setLevel(logging.INFO)

    fileHandler = logging.FileHandler(__file__ + ".log")
    fileHandler.setFormatter(logFormatter)
    rootLogger.addHandler(fileHandler)

    consoleHandler = logging.StreamHandler()
    consoleHandler.setFormatter(logFormatter)
    rootLogger.addHandler(consoleHandler)

    with shelve.open('db') as db:
        # db[(chat_id, msg_id)] = (is_bot, text, reply_id)
        # db['whitelist'] = set(whitelist_chat_ids)
        if 'whitelist' not in db:
            db['whitelist'] = {ADMIN_ID}
        updater = Updater(token=TELEGRAM_BOT_TOKEN, use_context=True)
        bot_id = updater.bot.get_me().id
        logging.info("Bot ID: %s", bot_id)
        dispatcher = updater.dispatcher
        dispatcher.add_handler(MessageHandler(Filters.text & (~Filters.command), reply_handler))
        dispatcher.add_handler(CommandHandler('ping', ping))
        dispatcher.add_handler(CommandHandler('add_whitelist', add_whitelist_handler))
        dispatcher.add_handler(CommandHandler('del_whitelist', del_whitelist_handler))
        dispatcher.add_handler(CommandHandler('get_whitelist', get_whitelist_handler))
        updater.start_polling()
        updater.idle()
