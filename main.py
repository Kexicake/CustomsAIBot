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
OPENROUTER_MODEL = os.getenv('OPENROUTER_MODEL', 'nex-agi/deepseek-v3.1-nex-n1:free')
OPENROUTER_BASE_URL = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1/chat/completions')

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
SYSTEM_PROMPT = "Допускается Markdown."

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


def export_pdf(text: str) -> BytesIO:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    _, height = A4
    y = height - 40
    for line in text.splitlines():
        if y < 40:
            c.showPage()
            y = height - 40
        c.drawString(40, y, line[:120])
        y -= 14
    c.save()
    buf.seek(0)
    return buf

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Добро пожаловать!\n\n"
        "Я AI-помощник по таможенному делу.\n"
        "Задайте вопрос текстом."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — начать\n"
        "/help — помощь\n"
        "/about — о боте\n"
        "/status — статус\n\n"
        "/style report|reference|letter\n"
        "/export pdf|docx"
    )


async def about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🤖 Таможенный AI-бот\n\n"
        f"Модель: {OPENROUTER_MODEL}\n"
        "Ответы носят справочный характер."
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"✅ Бот работает\n"
        f"Модель: {OPENROUTER_MODEL}"
    )


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
        await update.message.reply_text("/export pdf | docx")
        return

    fmt = context.args[0].lower()
    text = last_documents[uid]

    if fmt == "pdf":
        await update.message.reply_document(export_pdf(text), filename="document.pdf")
    elif fmt == "docx":
        await update.message.reply_document(export_docx(text), filename="document.docx")
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
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("style", style_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 Бот запущен (ALL COMMANDS RESTORED)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
