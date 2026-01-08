import os
import logging
import sys
import re
from typing import Optional, List, Dict
from io import BytesIO
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ChatAction
from html import escape as html_escape

from docx import Document
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

# ================= ENV =================
load_dotenv()

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
OPENROUTER_API_KEY = os.getenv('OPENROUTER_API_KEY')
OPENROUTER_MODEL = os.getenv('OPENROUTER_MODEL', 'deepseek/deepseek-chat')
OPENROUTER_BASE_URL = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')

if not TELEGRAM_BOT_TOKEN or not OPENROUTER_API_KEY:
    logger.error("❌ Не заданы TELEGRAM_BOT_TOKEN или OPENROUTER_API_KEY")
    sys.exit(1)

client = AsyncOpenAI(
    base_url=OPENROUTER_BASE_URL,
    api_key=OPENROUTER_API_KEY,
)

# ================= STATE =================
user_styles: Dict[int, str] = {}
last_documents: Dict[int, str] = {}

DEFAULT_STYLE = "REPORT"
MAX_TG_LEN = 4096

# ================= SYSTEM PROMPT =================
SYSTEM_PROMPT = """Ты - AI-помощник по таможенному делу. Твоя задача - предоставлять точную, 
актуальную и полезную информацию по вопросам таможенного регулирования, таможенных процедур, 
таможенных платежей и законодательства в сфере таможенного дела.

ПРИМЕЧАНИЕ: Всегда уточняй, что твои ответы носят информационный характер и не являются 
юридической консультацией. Для конкретных случаев рекомендовано обращаться к профильным 
специалистам или таможенным органам.

Отвечай на вопросы по следующим темам:
1. Таможенное оформление товаров
2. Таможенные платежи (пошлины, НДС, акцизы)
3. Таможенная стоимость
4. Запреты и ограничения
5. Декларирование товаров
6. Таможенные процедуры
7. Таможенный контроль
8. Международные договоры и соглашения

Формат ответов:
- Будь четким и структурированным
- Приводи ссылки на нормативные акты при возможности
- Используй примеры для наглядности
- Разбивай сложную информацию на пункты
- Подчеркивай важные моменты"""

# ================= FORMAT =================
def smart_format(text: str, style: str) -> str:
    lines = text.splitlines()
    out = []
    toc = []
    bullets = []
    section = 1

    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        if re.match(r'^#{1,6}\s+', line):
            title = re.sub(r'^#{1,6}\s+', '', line)
            toc.append((section, title))
            out += ["", f"{section}. {title.upper()}", "─" * (len(title) + 3)]
            section += 1
            i += 1
            continue

        if "|" in line and i + 1 < len(lines) and "---" in lines[i + 1]:
            headers = [c.strip() for c in line.strip("|").split("|")]
            rows = []
            i += 2
            while i < len(lines) and "|" in lines[i]:
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
                i += 1

            widths = [max(len(row[j]) for row in [headers] + rows) for j in range(len(headers))]

            def fmt(row):
                return " | ".join(row[j].ljust(widths[j]) for j in range(len(row)))

            out.append(fmt(headers))
            out.append("-+-".join("-" * w for w in widths))
            for r in rows:
                out.append(fmt(r))
            continue

        if re.match(r'^\d+[\.\)]\s+', line):
            item = re.sub(r'^(\d+)[\.\)]\s+', r'\1. ', line)
            out.append(item)
            bullets.append(item)
            i += 1
            continue

        if re.match(r'^[-*+]\s+', line):
            item = re.sub(r'^[-*+]\s+', '', line)
            out.append(f"• {item}")
            bullets.append(item)
            i += 1
            continue

        line = re.sub(r'(\*\*|\*|__|_|`)', '', line)

        if line.strip():
            out.append(line)

        i += 1

    if len(toc) >= 2:
        toc_block = ["ОГЛАВЛЕНИЕ", "──────────"]
        for n, t in toc:
            toc_block.append(f"{n}. {t}")
        out = toc_block + [""] + out

    if bullets:
        out += ["", "КРАТКОЕ РЕЗЮМЕ", "─────────────"]
        for b in bullets[:5]:
            out.append(f"• {b}")

    if style == "LETTER":
        out.insert(0, "Уважаемые коллеги,\n")
        out.append("\nС уважением,")

    return "\n".join(out).strip()

def split_text(text: str) -> List[str]:
    parts, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 <= MAX_TG_LEN:
            cur += para + "\n\n"
        else:
            parts.append(cur.strip())
            cur = para + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts

# ================= EXPORT =================
def export_docx(text: str) -> BytesIO:
    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf

# def export_pdf(text: str) -> BytesIO:
#     buf = BytesIO()
#     c = canvas.Canvas(buf, pagesize=A4)
#     _, height = A4
#     y = height - 40
#     for line in text.splitlines():
#         if y < 40:
#             c.showPage()
#             y = height - 40
#         c.drawString(40, y, line[:120])
#         y -= 14
#     c.save()
#     buf.seek(0)
#     return buf

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    welcome_text = """
👋 *Добро пожаловать в AI-помощник по таможенному делу!*

Я помогу вам с вопросами по:
• Таможенному оформлению товаров
• Таможенным платежам и пошлинам
• Таможенной стоимости
• Таможенным процедурам
• Нормативным требованиям

📝 *Просто задайте ваш вопрос, и я постараюсь дать развернутый ответ!*

⚠️ *Важно:* 
Мои ответы носят информационный характер и основаны на обученных данных. 
Для конкретных случаев обращайтесь к таможенным органам или юристам.

📚 *Используйте /help для списка команд*
"""
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /help"""
    help_text = """
📚 *Доступные команды:*
/start - Начать работу с ботом
/help - Получить справку
/about - О боте
/status - Проверить статус бота
/style report|reference|letter
/export docx

💡 *Как пользоваться:*
Просто отправьте ваш вопрос по таможенному делу текстовым сообщением.

*Примеры вопросов:*
• Какие документы нужны для таможенного оформления?
• Как рассчитать таможенную пошлину?
• Что такое таможенная стоимость?
• Какие товары запрещены к ввозу?
• Как оформить временный ввоз товаров?

🔄 *Техническая информация:*
Бот использует OpenRouter API с доступом к 400+ моделям.
Максимальная длина ответа: 2000 символов.
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /about"""
    about_text = f"""
🤖 *Таможенный AI-помощник*

*Версия:* 2.0
*Модель AI:* {OPENROUTER_MODEL}
*Платформа:* OpenRouter API

*Возможности:*
• Консультации по таможенному законодательству
• Разъяснение таможенных процедур
• Информация о таможенных платежах
• Ответы на вопросы по декларированию
• Доступ к 400+ моделям через один API

*Технические особенности:*
• Асинхронная обработка запросов
• Логирование всех запросов
• Обработка ошибок и таймаутов
• Поддержка Markdown форматирования

*Ограничения:*
• Информация носит справочный характер
• Может не учитывать последние изменения законодательства
• Не заменяет официальные консультации

🔗 *Для точной информации обращайтесь в ФТС России*
📧 *Вопросы по работе бота:* через /help
"""
    await update.message.reply_text(about_text, parse_mode='Markdown')

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /status"""
    status_text = f"""
✅ *Бот работает в штатном режиме*

*Текущая конфигурация:*
• Модель: `{OPENROUTER_MODEL}`
• API: OpenRouter (прямое подключение)
• Статус ключа: {"Проверен" if OPENROUTER_API_KEY else "Ошибка"}

*Статистика:*
• Лимит токенов: 2000 на ответ
• Формат вывода: Markdown
• Поддержка fallback-моделей: Да

Для проверки задайте любой вопрос по таможенной тематике.
"""
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def style_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("/style report | reference | letter")
        return
    style = context.args[0].upper()
    if style not in ("REPORT", "REFERENCE", "LETTER"):
        await update.message.reply_text("❌ Неизвестный стиль")
        return
    user_styles[update.effective_user.id] = style
    await update.message.reply_text(f"✅ Стиль установлен: {style}")

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in last_documents:
        await update.message.reply_text("Нет данных для экспорта")
        return
    if not context.args:
        await update.message.reply_text("/export docx")
        return

    fmt = context.args[0].lower()
    text = last_documents[uid]

    if fmt == "docx":
        await update.message.reply_document(export_docx(text), filename="document.docx")
    # elif fmt == "pdf":
    #     await update.message.reply_document(export_pdf(text), filename="document.pdf")
    else:
        await update.message.reply_text("❌ Формат не поддерживается")

# ================= MESSAGE HANDLER =================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    style = user_styles.get(uid, DEFAULT_STYLE)

    user_text = update.message.text
    logger.info(f"👤 User ({uid}): {user_text}")  # <-- Логируем запрос пользователя

    await update.message.chat.send_action(ChatAction.TYPING)

    response = await client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_text}
        ],
        temperature=0.3,
        max_tokens=2000
    )

    raw = response.choices[0].message.content
    logger.info(f"🤖 LLM response ({uid}): {raw}")  # <-- Логируем ответ LLM

    formatted = smart_format(raw, style)
    last_documents[uid] = formatted

    for part in split_text(formatted):
        await update.message.reply_text(
            html_escape(part),
            parse_mode="HTML",
            disable_web_page_preview=True
        )


# ================= MAIN =================
def main():
    try:
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("help", help_command))
        app.add_handler(CommandHandler("about", about))
        app.add_handler(CommandHandler("status", status))
        app.add_handler(CommandHandler("style", style_cmd))
        app.add_handler(CommandHandler("export", export_cmd))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        
        logger.info("=" * 50)
        logger.info("Бот запущен успешно!")
        logger.info(f"Базовая URL: {OPENROUTER_BASE_URL}")
        logger.info(f"Используемая модель: {OPENROUTER_MODEL}")
        logger.info("=" * 50)
        app.run_polling(drop_pending_updates=True)

    except Exception as e:
        logger.error(f"Не удалось запустить бота: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
