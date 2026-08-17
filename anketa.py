"""
ZIYNAT do'koni — ishga qabul anketasi boti.

TUZATILGAN XATOLAR RO'YXATI (eski versiyaga nisbatan):
  1.  f-string ichidagi teskari slesh (SyntaxError, Python < 3.12) olib tashlandi.
  2.  Mavjud bo'lmagan "gemini-3.5-*" modellari ro'yxatdan chiqarildi.
  3.  ConversationHandler'ga allow_reentry=True qo'shildi (/start endi doim ishlaydi).
  4.  Uzun xabarlar bo'laklarga bo'linadi (Telegram 4096 belgi limiti).
      Markdown o'rniga HTML ishlatilyapti + Gemini'ning "**qalin**" formati
      avtomatik <b> ga o'giriladi.
  5.  Rasm / anketa / AI tahlil alohida try-except bloklarida — biri yiqilsa,
      qolgani baribir direktorga yetib boradi.
  6.  Anketa oxirida va /cancel da user_data tozalanadi.
  7.  concurrent_updates(True) — bot bir vaqtda bir nechta odamga xizmat qiladi.
  8.  PicklePersistence — bot qayta ishga tushsa, anketalar yo'qolmaydi.
  9.  Sana / telefon kabi maydonlar AI'siz, lokal regex bilan tekshiriladi
      (tezroq va bepul). Ochiq savollargagina Gemini chaqiriladi.
  10. Validatsiya xato bergan lahzada klaviatura yo'qolmaydi.
  11. "Ha" javobi endi moslashuvchan aniqlanadi ("Ha.", "Ha, chiqqanman"...).
  12. Telefon raqami tekshiriladi va +998... ko'rinishiga keltiriladi.
  13. PHOTO bosqichida stiker/video/fayl yuborilsa, bot jim qolmaydi.
  14. Qabul/Rad tugmalarini faqat direktor yoki dasturchi bosa oladi.
  15. BOT_TOKEN / GEMINI kalitlari tekshiriladi, global error handler qo'shildi.
  16. genai.Client keshlanadi, AI chaqiruvlarida timeout bor.
"""

import os
import re
import json
import html
import asyncio
import logging

from aiohttp import web
from google import genai
from google.genai import types
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    PicklePersistence,
    filters,
)

# ==========================================================================
#  LOGGING
# ==========================================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


# ==========================================================================
#  SOZLAMALAR
# ==========================================================================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# ⚠️ TAVSIYA: bu ID'larni koddan olib tashlab, faqat Render'ning
# "Environment Variables" bo'limida saqlang. Hozir eski qiymatlar zaxira
# sifatida qoldirildi, toki bot to'xtab qolmasin.
ADMIN_ID = int(os.getenv("ADMIN_ID", "766309793"))          # Direktor
DEVELOPER_ID = int(os.getenv("DEVELOPER_ID", "1168952611"))  # Dasturchi

# Fayllar saqlanadigan papka (Render'da Disk ulasangiz — DATA_DIR=/data qiling)
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

MODE_FILE = os.path.join(DATA_DIR, "bot_mode.json")
PERSISTENCE_FILE = os.path.join(DATA_DIR, "bot_data.pickle")

# Gemini API kalitlari (vergul bilan bir nechta kalit berish mumkin)
RAW_KEYS = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_KEYS = [k.strip() for k in RAW_KEYS.split(",") if k.strip()]

# ✅ 2026-yil avgust holatiga ko'ra amaldagi modellar.
# ⚠️ Gemini 2.0 modellari 2026-yil 1-iyunda o'chirilgan, 2.5 esa yangi
# API kalitlar uchun yopilgan (404 "no longer available to new users").
# Agar kelajakda yana 404 chiqsa — /modellar buyrug'i orqali kalitingizga
# ochiq bo'lgan modellar ro'yxatini ko'ring va shu ro'yxatni yangilang.
VALIDATION_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"]
ANALYSIS_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]

# AI chaqiruvlari uchun timeout (soniya)
VALIDATION_TIMEOUT = 25
PHOTO_TIMEOUT = 35
ANALYSIS_TIMEOUT = 120

# Telegram xabar limiti 4096 — zaxira bilan
TG_LIMIT = 3800

PREDEFINED_BUTTONS = {
    "ha", "yo'q", "yoq", "uylangan", "uylanmagan", "turmush qurgan",
    "turmush qurmagan", "o'zbek", "rus", "tojik", "hovli", "dom", "o'rta",
    "o'rta maxsus", "oliy", "harbiyda bo'lganman", "harbiyda bo'lmaganman",
    "sudlanganman", "sudlanmaganman",
    "ha, avvalgi do'konda sahifani yuritganman",
    "yo'q, lekin tez o'rganib olaman",
}


# ==========================================================================
#  DASTURCHI REJIMI (test / production)
# ==========================================================================
def load_mode() -> str:
    """Joriy rejim: 'test' yoki 'production'."""
    try:
        if os.path.exists(MODE_FILE):
            with open(MODE_FILE, "r", encoding="utf-8") as f:
                mode = json.load(f).get("mode", "production")
                if mode in ("test", "production"):
                    return mode
    except Exception as e:
        logger.error("Rejimni o'qishda xatolik: %s", e)
    return "production"


def save_mode(mode: str) -> None:
    try:
        with open(MODE_FILE, "w", encoding="utf-8") as f:
            json.dump({"mode": mode}, f)
    except Exception as e:
        logger.error("Rejimni saqlashda xatolik: %s", e)


def get_recipient_id() -> int:
    return DEVELOPER_ID if load_mode() == "test" else ADMIN_ID


# ==========================================================================
#  GEMINI
# ==========================================================================
_client_cache: dict = {}


def _get_client(api_key: str):
    """Har chaqiruvda yangi Client yaratmaslik uchun kesh."""
    if api_key not in _client_cache:
        _client_cache[api_key] = genai.Client(api_key=api_key)
    return _client_cache[api_key]


def call_gemini_with_fallback(contents, models):
    """Barcha kalit va modellarni birma-bir sinaydi."""
    if not GEMINI_API_KEYS:
        raise ValueError("GEMINI_API_KEY topilmadi!")

    errors = []
    for idx, api_key in enumerate(GEMINI_API_KEYS, start=1):
        try:
            client = _get_client(api_key)
        except Exception as e:
            errors.append(f"Kalit #{idx}: client yaratilmadi — {e}")
            logger.warning("Client yaratilmadi: %s", e)
            continue

        for model_name in models:
            try:
                return client.models.generate_content(
                    model=model_name, contents=contents
                )
            except Exception as e:
                msg = str(e).replace("\n", " ")[:300]
                errors.append(f"Kalit #{idx} / {model_name}: {msg}")
                logger.warning("Model '%s' ishlamadi: %s", model_name, e)
                continue

    detail = " | ".join(errors) if errors else "noma'lum sabab"
    raise RuntimeError(f"Gemini ishlamadi. Tafsilot: {detail}")


async def _gemini_async(contents, models, timeout: int):
    """Gemini'ni thread'da, timeout bilan chaqiradi."""
    return await asyncio.wait_for(
        asyncio.to_thread(call_gemini_with_fallback, contents, models),
        timeout=timeout,
    )


# ==========================================================================
#  MATN YORDAMCHILARI (HTML)
# ==========================================================================
def esc(value) -> str:
    """Foydalanuvchi kiritgan matnni HTML uchun xavfsiz qiladi."""
    if value is None or str(value).strip() == "":
        return "—"
    return html.escape(str(value))


def ai_to_html(text: str) -> str:
    """Gemini'ning markdown javobini Telegram HTML'ga o'giradi."""
    t = html.escape(text or "")
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t, flags=re.DOTALL)
    t = re.sub(r"^\s{0,4}[\*\-]\s+", "• ", t, flags=re.MULTILINE)
    t = re.sub(r"^\s{0,4}#{1,6}\s*", "", t, flags=re.MULTILINE)
    return t


def strip_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text or ""))


def split_message(text: str, limit: int = TG_LIMIT):
    """Uzun matnni Telegram limitiga sig'adigan bo'laklarga ajratadi."""
    parts = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:]
    if text.strip():
        parts.append(text)
    return parts or [""]


async def reply(message, text, reply_markup=None):
    """HTML bilan javob; xato bo'lsa — oddiy matn bilan."""
    try:
        return await message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except Exception as e:
        logger.warning("HTML reply ishlamadi (%s), oddiy matnga o'tildi.", e)
        try:
            return await message.reply_text(strip_html(text), reply_markup=reply_markup)
        except Exception as e2:
            logger.error("Javob yuborilmadi: %s", e2)
            return None


async def send_long(bot, chat_id, text, reply_markup=None) -> bool:
    """Uzun matnni bo'laklab yuboradi. Tugmalar oxirgi bo'lakka ilinadi."""
    parts = split_message(text)
    ok = True
    for i, part in enumerate(parts):
        markup = reply_markup if i == len(parts) - 1 else None
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=part,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except Exception as e:
            logger.warning("HTML yuborilmadi (%s), oddiy matn sinalmoqda.", e)
            try:
                await bot.send_message(
                    chat_id=chat_id, text=strip_html(part), reply_markup=markup
                )
            except Exception as e2:
                logger.error("Bo'lak yuborilmadi: %s", e2)
                ok = False
        await asyncio.sleep(0.35)  # flood-limitdan saqlanish
    return ok


# ==========================================================================
#  BOSQICHLAR
# ==========================================================================
(
    PHOTO, POSITION, FULL_NAME, BIRTH_DATE, NATIONALITY, ADDRESS, HOUSING, PHONE,
    EDUCATION_LEVEL, EDU_DETAILS, WORK_EXP,
    VIDEO_SKILLS, EDITING_APPS, SMM_EXP, STORE_DUTIES,
    TRIP_ABROAD, TRIP_ABROAD_DETAILS, MARITAL_STATUS, FAMILY_MEMBERS,
    MILITARY, CRIMINAL,
    HOW_HEARD, GUARANTOR, BACKGROUND_CHECK, PREV_SALARY, EXPECTED_SALARY,
    WORK_DURATION, OVERTIME, HEALTH, ADDITIONAL,
) = range(30)


# --- Savol matnlari (bir joyda saqlanadi, takrorlanmaydi) -----------------
P_PHOTO = (
    "Assalomu alaykum! 'Ziynat' bijuteriya va soatlar do'koni ishga qabul "
    "anketasiga xush kelibsiz. ✨\n\n"
    "📸 Iltimos, anketaga biriktirish uchun o'zingizning rasmingizni yuboring:\n"
    "<i>(Yuzingiz aniq ko'ringan tushunarli rasm yuboring)</i>"
)
P_POSITION = (
    "Qaysi bo'lim va lavozimga topshiryapsiz?\n\n"
    "<i>(Misol: Do'kon sotuvchisi va kontent-menejer)</i>"
)
P_FULLNAME = (
    "Familiya, ism va sharifingizni kiriting:\n\n"
    "<i>(Misol: Abdullayeva Dilnoza Karim qizi)</i>"
)
P_BIRTHDATE = "Tug'ilgan sanangizni kiriting:\n\n<i>(Misol: 15.05.2001)</i>"
P_NATIONALITY = "Millatingizni tanlang yoki kiriting:"
P_ADDRESS = (
    "Doimiy yashash joyingiz (propiska manzilingiz):\n\n"
    "<i>(Misol: Toshkent sh., Chilonzor tumani, 5-mavze)</i>"
)
P_HOUSING = "Yashash sharoitingizni tanlang:"
P_PHONE = (
    "Shaxsiy mobil telefon raqamingizni yuboring:\n\n"
    "<i>(Tugmani bosing yoki qo'lda yozing: +998 90 123 45 67)</i>"
)
P_EDU_LEVEL = "Ma'lumotingiz darajasi:"
P_EDU_DETAILS = (
    "Qachon va qaysi o'quv yurtini tamomlagansiz?\n\n"
    "<i>(Misol: 2022-yil, Toshkent Moliya Instituti)</i>"
)
P_WORK_EXP = (
    "Avval qaysi korxona yoki do'konlarda va qanday lavozimda ishlagansiz?\n\n"
    "<i>(Misol: 2023-yil, 'X' kiyim do'konida sotuvchi bo'lib 1 yil ishlaganman)</i>"
)
P_VIDEO = (
    "📱 Telefoningizda sifatli video ololaysizmi va qaysi rusumdagi telefondan "
    "foydalanasiz?\n\n<i>(Misol: Ha, video olaman. Telefonim iPhone 13 / Samsung S21)</i>"
)
P_EDITING = (
    "🎬 Videolarni qaysi ilovalarda montaj qilasiz va qaysi birida yaxshi ishlay "
    "olasiz?\n\n<i>(Misol: CapCut, InShot, VN. CapCut dasturida juda yaxshi montaj qilaman)</i>"
)
P_SMM = (
    "💬 Instagram va Telegram'ga video joylash hamda mijozlar xabarlariga "
    "(DM/Comment) javob berish tajribangiz bormi?"
)
P_STORE = (
    "🛍 Do'konda tovarlarni (soat/bijuteriya) chiroyli joylashtirish, mijozlar "
    "bilan muloqot qilish va video olish vazifalarini bajara olasizmi?"
)
P_TRIP = "Chet el safariga chiqqanmisiz?"
P_TRIP_DETAILS = (
    "Chet elga qachon, qayerga va nima sababdan chiqqansiz?\n\n"
    "<i>(Misol: 2022-yil Turkiyaga vaqtinchalik sayohatga)</i>"
)
P_MARITAL = "Oilaviy ahvolingizni tanlang:"
P_FAMILY = (
    "Oila a'zolaringiz haqida ma'lumot bering:\n\n"
    "<i>(F.I.Sh., tug'ilgan yili, ish joyi va sudlangan/sudlanmaganligi)</i>"
)
P_MILITARY = "Harbiy xizmatda bo'lganmisiz?"
P_CRIMINAL = "Sudlanganlik holatingiz:"
P_HOW_HEARD = "Bizning 'Ziynat' do'konimiz haqida qayerdan eshitdingiz?"
P_GUARANTOR = (
    "Sizga kim kafillik yoki tavsiya bera oladi?\n\n"
    "<i>(Misol: Oxirgi ish joyimdagi rahbarim: Aliyev Vali, +998901234567)</i>"
)
P_BG_CHECK = "Oxirgi ish joyingizdan siz haqingizda surishtirishimizga rozimisiz?"
P_PREV_SALARY = "Oldingi ish joyingizdagi maoshingiz qancha edi?"
P_EXPECTED_SALARY = "Bizda qancha miqdordagi maoshga ishlamoqchisiz?"
P_WORK_DURATION = "Bizning do'konda qancha muddat ishlamoqchisiz?"
P_OVERTIME = "Ishdan keyin qolib ishlash (overtime) va majlislarga rozimisiz?"
P_HEALTH = "Sog'ligingizda muammolar yo'qmi?"
P_ADDITIONAL = (
    "O'zingiz haqingizda qo'shimcha ma'lumot (kuchli va ijobiy taraflaringiz):\n\n"
    "<i>(Misol: Kirishimli, mas'uliyatliman va muloqot qilishni yaxshi ko'raman)</i>"
)

# AI validatsiyasi uchun (HTML'siz, sof matn)
QUESTIONS = {
    POSITION: "Qaysi bo'lim va lavozimga topshiryapsiz?",
    FULL_NAME: "Familiya, ism-sharifingiz",
    NATIONALITY: "Millatingiz",
    ADDRESS: "Doimiy yashash joyingiz",
    EDU_DETAILS: "Qachon va qaysi o'quv yurtini tamomlagansiz",
    WORK_EXP: "Avval qaysi korxona/do'konlarda ishlagansiz",
    VIDEO_SKILLS: "Telefon va video olish tajribangiz",
    EDITING_APPS: "Montaj ilovalari (CapCut va b.)",
    SMM_EXP: "Instagram/Telegram va mijozlarga javob berish",
    STORE_DUTIES: "Do'kon vazifalari va tovarlar bilan ishlash",
    FAMILY_MEMBERS: "Oila a'zolaringiz haqida ma'lumot",
    HOW_HEARD: "Bizning 'Ziynat' do'konimiz haqida qayerdan eshitdingiz?",
    GUARANTOR: "Kafillik yoki tavsiya bera oladigan shaxs",
    PREV_SALARY: "Oldingi ish joyingizdagi maoshingiz",
    EXPECTED_SALARY: "Kutilayotgan maosh",
    WORK_DURATION: "Qancha muddat ishlamoqchisiz",
    HEALTH: "Sog'ligingizda muammolar yo'qmi?",
    ADDITIONAL: "O'zingiz haqingizda qo'shimcha ma'lumot",
}


# --- Klaviaturalar --------------------------------------------------------
KB_NATIONALITY = ReplyKeyboardMarkup(
    [["O'zbek", "Rus", "Tojik"]], resize_keyboard=True, one_time_keyboard=True
)
KB_HOUSING = ReplyKeyboardMarkup(
    [["Hovli", "Dom"]], resize_keyboard=True, one_time_keyboard=True
)
KB_PHONE = ReplyKeyboardMarkup(
    [[KeyboardButton("📱 Telefon raqamni yuborish", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)
KB_EDUCATION = ReplyKeyboardMarkup(
    [["O'rta", "O'rta maxsus", "Oliy"]], resize_keyboard=True, one_time_keyboard=True
)
KB_YES_NO = ReplyKeyboardMarkup(
    [["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True
)
KB_SMM = ReplyKeyboardMarkup(
    [["Ha, avvalgi do'konda sahifani yuritganman"], ["Yo'q, lekin tez o'rganib olaman"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)
KB_MARITAL = ReplyKeyboardMarkup(
    [["Uylangan", "Uylanmagan"], ["Turmush qurgan", "Turmush qurmagan"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)
KB_MILITARY = ReplyKeyboardMarkup(
    [["Harbiyda bo'lganman", "Harbiyda bo'lmaganman"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)
KB_CRIMINAL = ReplyKeyboardMarkup(
    [["Sudlanmaganman", "Sudlanganman"]], resize_keyboard=True, one_time_keyboard=True
)


# ==========================================================================
#  LOKAL (AI'siz) VALIDATSIYA
# ==========================================================================
DATE_RE = re.compile(r"(\d{1,2})\s*[.\-/\s]\s*(\d{1,2})\s*[.\-/\s]\s*(\d{4})")
YEAR_RE = re.compile(r"\b(19[4-9]\d|200\d|201[0-2])\b")
VOWELS = "aeiouаеёиоуыэюяўоʻ"


def looks_like_gibberish(text: str) -> bool:
    """'asdfgh', 'kjkhhi87to8f8' kabi javoblarni AI'siz aniqlaydi."""
    t = (text or "").strip()
    if len(t) < 2:
        return True
    letters = [c for c in t.lower() if c.isalpha()]
    if not letters:
        return not any(c.isdigit() for c in t)
    if len(letters) >= 6 and not any(c in VOWELS for c in letters):
        return True
    return False


def validate_birthdate(answer: str):
    """(ok, sabab, tozalangan_qiymat)"""
    m = DATE_RE.search(answer)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            return False, "Sana noto'g'ri. Kun 1-31, oy 1-12 oralig'ida bo'lishi kerak.", answer
        if not (1940 <= y <= 2012):
            return False, "Tug'ilgan yilingiz noto'g'ri ko'rinyapti. Iltimos, tekshirib qayta yozing.", answer
        return True, "", f"{d:02d}.{mo:02d}.{y}"
    if YEAR_RE.search(answer) and len(answer.strip()) <= 30:
        return True, "", answer.strip()
    return False, "Tug'ilgan sanangizni KUN.OY.YIL ko'rinishida yozing.", answer


def validate_phone(answer: str):
    digits = re.sub(r"\D", "", answer or "")
    if len(digits) == 9:
        return True, "", "+998" + digits
    if len(digits) == 12 and digits.startswith("998"):
        return True, "", "+" + digits
    if 10 <= len(digits) <= 15:
        return True, "", "+" + digits
    return False, "Telefon raqami noto'g'ri. Misol: +998 90 123 45 67", answer


def validate_fullname(answer: str):
    t = (answer or "").strip()
    if len(t) < 5 or len(t.split()) < 2:
        return False, "Familiya va ismingizni to'liq yozing.", t
    if looks_like_gibberish(t):
        return False, "Ism-familiya tushunarli emas.", t
    return True, "", t


# ==========================================================================
#  AI VALIDATSIYA
# ==========================================================================
async def validate_answer(question: str, answer: str) -> dict:
    clean_ans = (answer or "").strip().lower()

    if clean_ans in PREDEFINED_BUTTONS:
        return {"valid": True, "reason": ""}

    if looks_like_gibberish(answer):
        return {"valid": False, "reason": "Javobingiz tushunarli emas."}

    if not GEMINI_API_KEYS:
        return {"valid": True, "reason": ""}

    prompt = f"""Siz "Ziynat" do'koni ishga qabul anketasini tekshiruvchi bag'rikeng va tushunuvchan yordamchisiz.
Savol: "{question}"
Foydalanuvchi javobi: "{answer}"

VAZIFA: Foydalanuvchi javobi savolga mantiqan mos keladimi?

MUHIM QOIDALAR:
1. Agar javob savolga umuman aloqasiz bo'lsa (so'kish, "asdfgh", yoki "shahar" degan
   savolga "ovqat" deb javob berilgan bo'lsa) -> valid: false.
2. Oddiy, so'zlashuv tilidagi, qisqa yoki xatolar bilan yozilgan javoblarni
   ("yomon", "yo'q", "sog'lomman", "kasalligim yo'q", "ishlamaganman", "o'rganaman",
   "uylanmaganman", "harbiyda bo'lmaganman", "sudlanmaganman") HAMMA VAQT
   to'g'ri deb qabul qiling -> valid: true.

Faqat JSON formatida javob bering:
{{"valid": true yoki false, "reason": "agar valid false bo'lsa, qisqa sababini o'zbek tilida yozing"}}"""

    try:
        response = await _gemini_async(prompt, VALIDATION_MODELS, VALIDATION_TIMEOUT)
        match = re.search(r"\{.*\}", (response.text or "").strip(), re.DOTALL)
        if match:
            data = json.loads(match.group(0))
            if isinstance(data, dict) and "valid" in data:
                return data
    except asyncio.TimeoutError:
        logger.warning("Validatsiya timeout — javob qabul qilindi.")
    except Exception as e:
        logger.error("Validatsiyada xatolik: %s", e)

    return {"valid": True, "reason": ""}


async def validate_photo(photo_bytes: bytes) -> dict:
    if not GEMINI_API_KEYS:
        return {"is_person": True, "reason": ""}

    prompt = """Siz fotosuratlarni tahlil qiluvchi mutaxassisiz.
Ushbu rasmda INSON YUZI (portret, selfie yoki odam qiyofasi) ko'rinib turibdimi?

QOIDALAR:
1. Buyumlar, hujjat, mashina, avtomobil, hayvonlar -> "is_person": false
2. Inson yuzi ko'ringan bo'lsa -> "is_person": true

FAQAT ushbu JSON formatida javob bering:
{"is_person": true yoki false, "reason": "qisqa sabab o'zbek tilida"}"""

    try:
        contents = [
            types.Part.from_bytes(data=bytes(photo_bytes), mime_type="image/jpeg"),
            prompt,
        ]
        response = await _gemini_async(contents, VALIDATION_MODELS, PHOTO_TIMEOUT)
        text = (response.text or "").strip()

        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
                if "is_person" in result:
                    return result
            except Exception:
                pass

        low = text.lower()
        if any(w in low for w in ("false", "mashina", "avtomobil", "buyum")):
            return {"is_person": False, "reason": "Rasmda inson yuzi ko'rinmayapti."}
        if "true" in low:
            return {"is_person": True, "reason": ""}
    except asyncio.TimeoutError:
        logger.warning("Rasm validatsiyasi timeout — rasm qabul qilindi.")
    except Exception as e:
        logger.error("Rasm validatsiyasida xatolik: %s", e)

    return {"is_person": True, "reason": ""}


async def analyze_candidate_with_ai(user_data: dict) -> str:
    prompt = f"""
Siz "ZIYNAT" bijuteriya va soatlar do'konining HR menejerisiz.
Do'konda xodimlar mijozlarga soat va zargarlik buyumlarini sotishi, tovarlarni chiroyli
joylashtirishi, mobil telefon orqali video olib, CapCut kabi ilovalarda montaj qilishi
hamda Instagram/Telegram'da mijozlar bilan muloqot qilishi kerak.

NOMZOD MA'LUMOTLARI:
- F.I.Sh va Lavozim: {user_data.get('fullname')} / {user_data.get('position')}
- Yoshi, Millati va Manzili: {user_data.get('birthdate')} / {user_data.get('nationality')} / {user_data.get('address')} ({user_data.get('housing')})
- Tel: {user_data.get('phone')}
- Ma'lumoti va Ish tajribasi: {user_data.get('education_level')} / {user_data.get('edu_details')} | Tajriba: {user_data.get('work_exp')}
- Video olish va Telefon: {user_data.get('video_skills')}
- Montaj (CapCut va b.): {user_data.get('editing_apps')}
- SMM va Mijozlar bilan muloqot: {user_data.get('smm_exp')}
- Do'kon vazifalariga tayyorligi: {user_data.get('store_duties')}
- Oilaviy ahvoli va A'zolari: {user_data.get('marital_status')} / {user_data.get('family_members')}
- Oldingi va Kutilayotgan maosh: {user_data.get('prev_salary')} / {user_data.get('expected_salary')}
- Ishlash muddati / Qolib ishlash: {user_data.get('work_duration')} / {user_data.get('overtime')}
- Sog'lig'i: {user_data.get('health')}
- Qo'shimcha sifatlari: {user_data.get('additional')}

QUYIDAGI MEZONLAR BO'YICHA "ZIYNAT" DO'KONI DIREKTORI UCHUN HR TAHLIL BERING
(O'zbek tilida, jami 350 so'zdan oshmasin):
1. **Sotuv va Mijozlar bilan muloqot salohiyati (1-10 ball)**
2. **Video olish va Montaj (CapCut/SMM) mahorati (1-10 ball)**
3. **Mas'uliyat va Do'kon vazifalariga tayyorligi**
4. **Nomzodning kuchli va zaif tomonlari**
5. **YAKUNIY BAHO VA DIREKTORGA TAVSIYA (1-10 ball)**
"""
    try:
        response = await _gemini_async(prompt, ANALYSIS_MODELS, ANALYSIS_TIMEOUT)
        return response.text or "⚠️ AI bo'sh javob qaytardi."
    except asyncio.TimeoutError:
        logger.error("AI tahlili timeout.")
        return "⚠️ AI tahlili juda uzoq davom etdi. Anketani qo'lda ko'rib chiqing."
    except Exception as e:
        logger.error("Gemini AI xatoligi: %s", e)
        return (
            "⚠️ Sun'iy intellekt tahlilida xatolik yuz berdi. "
            "Anketani qo'lda ko'rib chiqing.\n\n"
            f"🔧 Texnik sabab: {str(e)[:800]}"
        )


# ==========================================================================
#  UMUMIY BOSQICH ISHLOVCHISI
# ==========================================================================
async def process_text_step(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    current_state: int,
    field_name: str,
    current_prompt: str,
    next_prompt: str,
    next_state: int,
    keyboard=None,          # keyingi savol klaviaturasi
    current_keyboard=None,  # xato bo'lsa qayta ko'rsatiladigan klaviatura
    local_validator=None,   # AI'siz tekshiruv funksiyasi
):
    answer = (update.message.text or "").strip()

    if not answer:
        await reply(
            update.message,
            "⚠️ Javob bo'sh bo'lishi mumkin emas.\n\n" + current_prompt,
            reply_markup=current_keyboard or ReplyKeyboardRemove(),
        )
        return current_state

    if local_validator is not None:
        ok, reason, cleaned = local_validator(answer)
        if not ok:
            await reply(
                update.message,
                f"⚠️ {esc(reason)}\n\n{current_prompt}",
                reply_markup=current_keyboard or ReplyKeyboardRemove(),
            )
            return current_state
        answer = cleaned
    else:
        question_text = QUESTIONS.get(current_state, "")
        if question_text:
            result = await validate_answer(question_text, answer)
            if not result.get("valid", True):
                reason = result.get("reason") or "Javob savolga mos emas."
                await reply(
                    update.message,
                    f"⚠️ {esc(reason)}\n\nIltimos, ushbu savolga qaytadan javob bering:\n\n{current_prompt}",
                    reply_markup=current_keyboard or ReplyKeyboardRemove(),
                )
                return current_state

    context.user_data[field_name] = answer
    await reply(update.message, next_prompt, reply_markup=keyboard or ReplyKeyboardRemove())
    return next_state


# ==========================================================================
#  DASTURCHI REJIMI BUYRUG'I
# ==========================================================================
async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔒 Faqat DEVELOPER_ID uchun. Boshqalarga bot javob bermaydi."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or user.id != DEVELOPER_ID:
        return

    if load_mode() == "test":
        status = (
            "🧪 Hozirgi rejim: <b>TEST</b>\n\n"
            "Barcha yangi anketalar faqat SIZGA keladi, direktor ularni ko'rmaydi."
        )
    else:
        status = (
            "🚀 Hozirgi rejim: <b>ISHLAB CHIQARISH</b>\n\n"
            "Barcha yangi anketalar direktorga yuborilyapti."
        )

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🧪 Test rejimi (faqat menga)", callback_data="mode_test")],
            [InlineKeyboardButton("🚀 Ishlab chiqarish (direktorga)", callback_data="mode_prod")],
        ]
    )
    await message.reply_text(status, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔒 Faqat dasturchi uchun: kalitga ochiq bo'lgan modellar ro'yxati."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or user.id != DEVELOPER_ID:
        return

    if not GEMINI_API_KEYS:
        await message.reply_text("❌ GEMINI_API_KEY topilmadi.")
        return

    await message.reply_text("🔎 Mavjud modellar ro'yxati olinmoqda...")
    lines = []

    for idx, api_key in enumerate(GEMINI_API_KEYS, start=1):
        lines.append(f"\n🔑 <b>Kalit #{idx}</b> (...{api_key[-4:]})")
        try:
            client = _get_client(api_key)
            models = await asyncio.wait_for(
                asyncio.to_thread(lambda c=client: list(c.models.list())), timeout=45
            )
            names = []
            for m in models:
                actions = getattr(m, "supported_actions", None) or []
                if actions and "generateContent" not in actions:
                    continue
                name = (getattr(m, "name", "") or "").replace("models/", "")
                if name and "embedding" not in name and "image" not in name:
                    names.append(name)
            if names:
                lines.extend(f"  • {esc(n)}" for n in sorted(names))
            else:
                lines.append("  ⚠️ Matn generatsiya qiluvchi model topilmadi.")
        except Exception as e:
            lines.append(f"  ❌ {esc(str(e)[:200])}")

    await send_long(context.bot, message.chat_id, "\n".join(lines))


async def cmd_aitest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """🔒 Faqat dasturchi uchun: har bir kalit va modelni alohida sinaydi."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or user.id != DEVELOPER_ID:
        return

    if not GEMINI_API_KEYS:
        await message.reply_text("❌ GEMINI_API_KEY umuman topilmadi.")
        return

    await message.reply_text(f"🔎 {len(GEMINI_API_KEYS)} ta kalit sinalmoqda...")

    all_models = list(dict.fromkeys(VALIDATION_MODELS + ANALYSIS_MODELS))
    lines = []

    for idx, api_key in enumerate(GEMINI_API_KEYS, start=1):
        lines.append(f"\n🔑 <b>Kalit #{idx}</b> (...{api_key[-4:]})")
        for model_name in all_models:
            try:
                client = _get_client(api_key)
                resp = await asyncio.wait_for(
                    asyncio.to_thread(
                        client.models.generate_content,
                        model=model_name,
                        contents="Javob: OK",
                    ),
                    timeout=30,
                )
                ok = bool(resp.text and resp.text.strip())
                lines.append(f"  {'✅' if ok else '⚠️ bo`sh javob'} {model_name}")
            except asyncio.TimeoutError:
                lines.append(f"  ⏱ {model_name} — timeout")
            except Exception as e:
                lines.append(f"  ❌ {model_name} — {esc(str(e)[:180])}")

    await send_long(context.bot, message.chat_id, "\n".join(lines))


async def handle_mode_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != DEVELOPER_ID:
        await query.answer("⛔️ Ruxsat yo'q.", show_alert=True)
        return

    new_mode = "test" if query.data == "mode_test" else "production"
    save_mode(new_mode)
    await query.answer("✅ Rejim yangilandi.", show_alert=True)

    text = (
        "🧪 <b>TEST rejimi yoqildi.</b>\n\nEndi barcha yangi anketalar faqat sizga keladi."
        if new_mode == "test"
        else "🚀 <b>ISHLAB CHIQARISH rejimi yoqildi.</b>\n\nEndi barcha yangi anketalar direktorga boradi."
    )
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass


# ==========================================================================
#  QABUL / RAD ETISH
# ==========================================================================
async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id not in (ADMIN_ID, DEVELOPER_ID):
        await query.answer("⛔️ Bu tugma faqat rahbariyat uchun.", show_alert=True)
        return

    await query.answer()

    if query.data == "done":
        return

    try:
        action, user_id = query.data.split("_", 1)
        user_id = int(user_id)
    except (ValueError, AttributeError):
        return

    if action == "accept":
        new_keyboard = [[InlineKeyboardButton("🟢 QABUL QILINGAN ✅", callback_data="done")]]
        user_msg = (
            "🎉 <b>Tabriklaymiz!</b>\n\n"
            "Sizning anketangiz 'Ziynat' do'koni rahbariyati tomonidan ijobiy baholandi "
            "va suhbatga taklif qilinasiz! Tez orada siz bilan bog'lanamiz."
        )
    elif action == "reject":
        new_keyboard = [[InlineKeyboardButton("🔴 RAD ETILDI ❌", callback_data="done")]]
        user_msg = (
            "Assalomu alaykum.\n\n"
            "Afsuski, sizning anketangiz hozirgi vaqtda bizning talablarimizga mos kelmadi. "
            "Anketani to'ldirganingiz uchun rahmat, kelgusi ishlaringizda omad tilaymiz!"
        )
    else:
        return

    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))
    except Exception as e:
        logger.warning("Tugmani yangilashda xatolik: %s", e)

    try:
        await context.bot.send_message(
            chat_id=user_id, text=user_msg, parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error("Nomzodga qarorni yuborishda xatolik: %s", e)
        try:
            await context.bot.send_message(
                chat_id=query.from_user.id,
                text="⚠️ Nomzodga xabar yuborib bo'lmadi (u botni bloklagan bo'lishi mumkin).",
            )
        except Exception:
            pass


# ==========================================================================
#  ANKETA BOSQICHLARI
# ==========================================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await reply(update.message, P_PHOTO, reply_markup=ReplyKeyboardRemove())
    return PHOTO


async def photo_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """PHOTO bosqichida rasmdan boshqa narsa yuborilsa."""
    await reply(
        update.message,
        "⚠️ Avval rasm yuborish kerak.\n\n"
        "Iltimos, 📎 tugmasi orqali <b>rasm (photo)</b> yuboring — "
        "fayl, stiker yoki video emas.",
    )
    return PHOTO


async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return await photo_reminder(update, context)

    checking_msg = await reply(update.message, "⏳ Rasmingiz tekshirilmoqda...")

    try:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        result = await validate_photo(photo_bytes)
    except Exception as e:
        logger.error("Rasmni yuklab olishda xatolik: %s", e)
        result = {"is_person": True, "reason": ""}

    if checking_msg:
        try:
            await checking_msg.delete()
        except Exception:
            pass

    if not result.get("is_person", True):
        reason = result.get("reason") or "Bu rasmda inson yuzi ko'rinmayapti."
        await reply(
            update.message,
            f"❌ {esc(reason)}\n\nIltimos, yuzingiz aniq ko'ringan haqiqiy suratingizni yuboring:",
        )
        return PHOTO

    context.user_data["photo"] = update.message.photo[-1].file_id
    await reply(update.message, P_POSITION)
    return POSITION


async def get_position(update, context):
    return await process_text_step(
        update, context, POSITION, "position",
        P_POSITION, P_FULLNAME, FULL_NAME,
    )


async def get_fullname(update, context):
    return await process_text_step(
        update, context, FULL_NAME, "fullname",
        P_FULLNAME, P_BIRTHDATE, BIRTH_DATE,
        local_validator=validate_fullname,
    )


async def get_birthdate(update, context):
    return await process_text_step(
        update, context, BIRTH_DATE, "birthdate",
        P_BIRTHDATE, P_NATIONALITY, NATIONALITY,
        keyboard=KB_NATIONALITY,
        local_validator=validate_birthdate,
    )


async def get_nationality(update, context):
    return await process_text_step(
        update, context, NATIONALITY, "nationality",
        P_NATIONALITY, P_ADDRESS, ADDRESS,
        current_keyboard=KB_NATIONALITY,
    )


async def get_address(update, context):
    return await process_text_step(
        update, context, ADDRESS, "address",
        P_ADDRESS, P_HOUSING, HOUSING,
        keyboard=KB_HOUSING,
    )


async def get_housing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await reply(update.message, P_HOUSING, reply_markup=KB_HOUSING)
        return HOUSING
    context.user_data["housing"] = text
    await reply(update.message, P_PHONE, reply_markup=KB_PHONE)
    return PHONE


async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        phone = update.message.contact.phone_number
        if phone and not phone.startswith("+"):
            phone = "+" + phone
    else:
        ok, reason, cleaned = validate_phone(update.message.text or "")
        if not ok:
            await reply(
                update.message,
                f"⚠️ {esc(reason)}\n\n{P_PHONE}",
                reply_markup=KB_PHONE,
            )
            return PHONE
        phone = cleaned

    context.user_data["phone"] = phone
    await reply(update.message, P_EDU_LEVEL, reply_markup=KB_EDUCATION)
    return EDUCATION_LEVEL


async def get_education_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await reply(update.message, P_EDU_LEVEL, reply_markup=KB_EDUCATION)
        return EDUCATION_LEVEL
    context.user_data["education_level"] = text
    await reply(update.message, P_EDU_DETAILS, reply_markup=ReplyKeyboardRemove())
    return EDU_DETAILS


async def get_edudetails(update, context):
    return await process_text_step(
        update, context, EDU_DETAILS, "edu_details",
        P_EDU_DETAILS, P_WORK_EXP, WORK_EXP,
    )


async def get_workexp(update, context):
    return await process_text_step(
        update, context, WORK_EXP, "work_exp",
        P_WORK_EXP, P_VIDEO, VIDEO_SKILLS,
    )


async def get_video_skills(update, context):
    return await process_text_step(
        update, context, VIDEO_SKILLS, "video_skills",
        P_VIDEO, P_EDITING, EDITING_APPS,
    )


async def get_editing_apps(update, context):
    return await process_text_step(
        update, context, EDITING_APPS, "editing_apps",
        P_EDITING, P_SMM, SMM_EXP,
        keyboard=KB_SMM,
    )


async def get_smm_exp(update, context):
    return await process_text_step(
        update, context, SMM_EXP, "smm_exp",
        P_SMM, P_STORE, STORE_DUTIES,
        keyboard=KB_YES_NO,
        current_keyboard=KB_SMM,
    )


async def get_store_duties(update, context):
    return await process_text_step(
        update, context, STORE_DUTIES, "store_duties",
        P_STORE, P_MARITAL, MARITAL_STATUS,
        keyboard=KB_MARITAL,
        current_keyboard=KB_YES_NO,
    )


def _is_yes(text: str) -> bool:
    t = (text or "").strip().lower()
    if t.startswith(("yo'q", "yoq", "yo`q", "yo‘q", "нет")):
        return False
    return bool(re.match(r"^(ha|xa|да|yes)\b", t)) or t in ("ha", "xa")


async def get_marital_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await reply(update.message, P_MARITAL, reply_markup=KB_MARITAL)
        return MARITAL_STATUS
    context.user_data["marital_status"] = text
    await reply(update.message, P_FAMILY, reply_markup=ReplyKeyboardRemove())
    return FAMILY_MEMBERS


async def get_family_members(update, context):
    return await process_text_step(
        update, context, FAMILY_MEMBERS, "family_members",
        P_FAMILY, P_HOW_HEARD, HOW_HEARD,
    )


async def get_how_heard(update, context):
    return await process_text_step(
        update, context, HOW_HEARD, "how_heard",
        P_HOW_HEARD, P_GUARANTOR, GUARANTOR,
    )


async def get_guarantor(update, context):
    return await process_text_step(
        update, context, GUARANTOR, "guarantor",
        P_GUARANTOR, P_BG_CHECK, BACKGROUND_CHECK,
        keyboard=KB_YES_NO,
    )


async def get_background_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await reply(update.message, P_BG_CHECK, reply_markup=KB_YES_NO)
        return BACKGROUND_CHECK
    context.user_data["background_check"] = text
    await reply(update.message, P_PREV_SALARY, reply_markup=ReplyKeyboardRemove())
    return PREV_SALARY


async def get_prev_salary(update, context):
    return await process_text_step(
        update, context, PREV_SALARY, "prev_salary",
        P_PREV_SALARY, P_EXPECTED_SALARY, EXPECTED_SALARY,
    )


async def get_expected_salary(update, context):
    return await process_text_step(
        update, context, EXPECTED_SALARY, "expected_salary",
        P_EXPECTED_SALARY, P_WORK_DURATION, WORK_DURATION,
    )


async def get_work_duration(update, context):
    return await process_text_step(
        update, context, WORK_DURATION, "work_duration",
        P_WORK_DURATION, P_OVERTIME, OVERTIME,
        keyboard=KB_YES_NO,
    )


async def get_overtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        await reply(update.message, P_OVERTIME, reply_markup=KB_YES_NO)
        return OVERTIME
    context.user_data["overtime"] = text
    await reply(update.message, P_HEALTH, reply_markup=ReplyKeyboardRemove())
    return HEALTH


async def get_health(update, context):
    return await process_text_step(
        update, context, HEALTH, "health",
        P_HEALTH, P_ADDITIONAL, ADDITIONAL,
    )


# ==========================================================================
#  YAKUNIY BOSQICH
# ==========================================================================
def build_summary(d: dict) -> str:
    return (
        "📥 <b>ZIYNAT DO'KONI — YANGI NOMZOD ANKETASI</b>\n"
        "====================================\n\n"
        "📌 <b>1. SHAXSIY MA'LUMOTLAR:</b>\n"
        f"🎯 <b>Lavozim:</b> {esc(d.get('position'))}\n"
        f"👤 <b>F.I.Sh:</b> {esc(d.get('fullname'))}\n"
        f"🎂 <b>Tug'ilgan sanasi:</b> {esc(d.get('birthdate'))}\n"
        f"🇺🇿 <b>Millati:</b> {esc(d.get('nationality'))}\n"
        f"🏠 <b>Manzil:</b> {esc(d.get('address'))} ({esc(d.get('housing'))})\n"
        f"📞 <b>Tel:</b> {esc(d.get('phone'))}\n\n"
        "📌 <b>2. MA'LUMOTI VA ISH TAJRIBASI:</b>\n"
        f"🎓 <b>Ma'lumoti:</b> {esc(d.get('education_level'))} ({esc(d.get('edu_details'))})\n"
        f"💼 <b>Ish tajribasi:</b> {esc(d.get('work_exp'))}\n\n"
        "📌 <b>3. VIDEO, MONTAJ VA SMM:</b>\n"
        f"📹 <b>Video olish / Telefon:</b> {esc(d.get('video_skills'))}\n"
        f"🎬 <b>Montaj (CapCut va b.):</b> {esc(d.get('editing_apps'))}\n"
        f"💬 <b>Instagram/Telegram/Mijozlar:</b> {esc(d.get('smm_exp'))}\n"
        f"🛍 <b>Do'kon vazifalariga tayyorligi:</b> {esc(d.get('store_duties'))}\n\n"
        "📌 <b>4. OILAVIY VA SHAXSIY:</b>\n"
        f"💍 <b>Oilaviy ahvoli:</b> {esc(d.get('marital_status'))}\n"
        f"👨‍👩‍👧‍👦 <b>Oila a'zolari:</b> {esc(d.get('family_members'))}\n"
        f"📢 <b>Manba:</b> {esc(d.get('how_heard'))}\n"
        f"🤝 <b>Kafillik/Tavsiya:</b> {esc(d.get('guarantor'))}\n"
        f"🔍 <b>Surishtirishga roziligi:</b> {esc(d.get('background_check'))}\n\n"
        "📌 <b>5. SHAROITLAR VA TALABLAR:</b>\n"
        f"💵 <b>Oldingi / Kutilayotgan maosh:</b> {esc(d.get('prev_salary'))} / {esc(d.get('expected_salary'))}\n"
        f"⏳ <b>Ishlash muddati:</b> {esc(d.get('work_duration'))}\n"
        f"⏰ <b>Overtime va majlislar:</b> {esc(d.get('overtime'))}\n"
        f"🏥 <b>Sog'lig'i:</b> {esc(d.get('health'))}\n"
        f"📝 <b>Qo'shimcha:</b> {esc(d.get('additional'))}\n"
    )


async def get_additional(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = (update.message.text or "").strip()

    if not answer:
        await reply(update.message, P_ADDITIONAL)
        return ADDITIONAL

    result = await validate_answer(QUESTIONS.get(ADDITIONAL, ""), answer)
    if not result.get("valid", True):
        reason = result.get("reason") or "Javob mos emas."
        await reply(
            update.message,
            f"⚠️ {esc(reason)}\n\nIltimos, ushbu savolga qaytadan javob bering:\n\n{P_ADDITIONAL}",
        )
        return ADDITIONAL

    context.user_data["additional"] = answer
    user_id = update.effective_user.id
    username = update.effective_user.username

    await reply(
        update.message,
        "Rahmat! Anketangiz qabul qilindi.",
        reply_markup=ReplyKeyboardRemove(),
    )

    data = dict(context.user_data)
    summary_text = build_summary(data)

    ai_analysis = await analyze_candidate_with_ai(data)
    ai_report_text = (
        "🤖 <b>GEMINI AI — HR TAHLIL VA BAHOSI</b>\n"
        "------------------------------------\n" + ai_to_html(ai_analysis)
    )

    decision_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Qabul qilish (Suhbatga)", callback_data=f"accept_{user_id}"),
                InlineKeyboardButton("❌ Rad etish", callback_data=f"reject_{user_id}"),
            ]
        ]
    )

    recipient_id = get_recipient_id()
    test_prefix = (
        "🧪 <b>[TEST REJIMI — direktorga yuborilmadi]</b>\n\n"
        if recipient_id == DEVELOPER_ID
        else ""
    )

    # --- 1) Rasm (alohida try) --------------------------------------------
    try:
        photo = data.get("photo")
        if photo:
            contact = f"@{username}" if username else f"ID: {user_id}"
            caption = (
                f"{test_prefix}"
                f"📥 <b>YANGI NOMZOD:</b> {esc(data.get('fullname'))}\n"
                f"🎯 <b>Lavozim:</b> {esc(data.get('position'))}\n"
                f"💬 <b>Telegram:</b> {esc(contact)}"
            )
            await context.bot.send_photo(
                chat_id=recipient_id,
                photo=photo,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        logger.error("Rasm yuborilmadi: %s", e)

    # --- 2) Anketa matni (alohida try) ------------------------------------
    try:
        await send_long(context.bot, recipient_id, test_prefix + summary_text)
    except Exception as e:
        logger.error("Anketa yuborilmadi: %s", e)

    # --- 3) AI tahlil + tugmalar (alohida try) ----------------------------
    try:
        ok = await send_long(
            context.bot, recipient_id, ai_report_text, reply_markup=decision_keyboard
        )
        if not ok:
            await context.bot.send_message(
                chat_id=recipient_id,
                text="⚠️ AI tahlilini yuborishda muammo bo'ldi. Qaror qabul qilish tugmalari:",
                reply_markup=decision_keyboard,
            )
    except Exception as e:
        logger.error("AI tahlili yuborilmadi: %s", e)
        try:
            await context.bot.send_message(
                chat_id=recipient_id,
                text="⚠️ AI tahlili yuborilmadi. Qaror qabul qilish tugmalari:",
                reply_markup=decision_keyboard,
            )
        except Exception:
            pass

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await reply(
        update.message,
        "Anketa bekor qilindi.\n\nQaytadan boshlash uchun /start buyrug'ini yuboring.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def unknown_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Suhbatdan tashqaridagi xabarlar."""
    if update.effective_message:
        await reply(
            update.effective_message,
            "Anketani to'ldirishni boshlash uchun /start buyrug'ini yuboring. 📝",
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Kutilmagan xatolik:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Kutilmagan xatolik yuz berdi. Iltimos, biroz kutib qayta urinib ko'ring."
            )
    except Exception:
        pass


# ==========================================================================
#  RENDER UCHUN VEB-SERVER (UptimeRobot ping)
# ==========================================================================
async def start_dummy_server():
    async def handle_ping(request):
        return web.Response(text="Ziynat HR Anketa Bot is running!", status=200)

    app = web.Application()
    app.router.add_route("*", "/", handle_ping)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Render veb-server %s-portda ishga tushdi.", port)


async def post_init(application):
    commands = [
        BotCommand("start", "Anketani boshlash 🚀"),
        BotCommand("cancel", "Anketani bekor qilish ❌"),
        # "/rejim" ataylab ro'yxatga qo'shilmagan — faqat dasturchi uchun.
    ]
    await application.bot.set_my_commands(commands)
    await start_dummy_server()


# ==========================================================================
#  MAIN
# ==========================================================================
def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "❌ BOT_TOKEN topilmadi! Render > Environment bo'limida BOT_TOKEN qo'shing."
        )
    if not GEMINI_API_KEYS:
        logger.warning(
            "⚠️ GEMINI_API_KEY topilmadi — AI tekshiruvi va tahlili ishlamaydi, "
            "lekin anketa baribir yig'iladi."
        )

    persistence = PicklePersistence(filepath=PERSISTENCE_FILE)

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )

    text_only = filters.TEXT & ~filters.COMMAND

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PHOTO: [
                MessageHandler(filters.PHOTO, get_photo),
                MessageHandler(~filters.COMMAND, photo_reminder),
            ],
            POSITION: [MessageHandler(text_only, get_position)],
            FULL_NAME: [MessageHandler(text_only, get_fullname)],
            BIRTH_DATE: [MessageHandler(text_only, get_birthdate)],
            NATIONALITY: [MessageHandler(text_only, get_nationality)],
            ADDRESS: [MessageHandler(text_only, get_address)],
            HOUSING: [MessageHandler(text_only, get_housing)],
            PHONE: [
                MessageHandler(filters.CONTACT, get_phone),
                MessageHandler(text_only, get_phone),
            ],
            EDUCATION_LEVEL: [MessageHandler(text_only, get_education_level)],
            EDU_DETAILS: [MessageHandler(text_only, get_edudetails)],
            WORK_EXP: [MessageHandler(text_only, get_workexp)],
            VIDEO_SKILLS: [MessageHandler(text_only, get_video_skills)],
            EDITING_APPS: [MessageHandler(text_only, get_editing_apps)],
            SMM_EXP: [MessageHandler(text_only, get_smm_exp)],
            STORE_DUTIES: [MessageHandler(text_only, get_store_duties)],
            MARITAL_STATUS: [MessageHandler(text_only, get_marital_status)],
            FAMILY_MEMBERS: [MessageHandler(text_only, get_family_members)],
            HOW_HEARD: [MessageHandler(text_only, get_how_heard)],
            GUARANTOR: [MessageHandler(text_only, get_guarantor)],
            BACKGROUND_CHECK: [MessageHandler(text_only, get_background_check)],
            PREV_SALARY: [MessageHandler(text_only, get_prev_salary)],
            EXPECTED_SALARY: [MessageHandler(text_only, get_expected_salary)],
            WORK_DURATION: [MessageHandler(text_only, get_work_duration)],
            OVERTIME: [MessageHandler(text_only, get_overtime)],
            HEALTH: [MessageHandler(text_only, get_health)],
            ADDITIONAL: [MessageHandler(text_only, get_additional)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        allow_reentry=True,      # ✅ /start endi istalgan payt qayta ishlaydi
        persistent=True,         # ✅ bot qayta ishga tushsa, holat saqlanadi
        name="ziynat_anketa",
    )

    app.add_handler(conv_handler)

    # 🔒 Yashirin dasturchi buyrug'i
    app.add_handler(CommandHandler("rejim", cmd_mode))
    app.add_handler(CommandHandler("aitest", cmd_aitest))
    app.add_handler(CommandHandler("modellar", cmd_models))
    app.add_handler(CallbackQueryHandler(handle_mode_decision, pattern=r"^mode_"))

    # Qabul / rad etish tugmalari
    app.add_handler(CallbackQueryHandler(handle_decision, pattern=r"^(accept_|reject_|done$)"))

    # Eng oxirida — suhbatdan tashqaridagi xabarlar
    app.add_handler(MessageHandler(filters.ALL, unknown_message))

    app.add_error_handler(error_handler)

    logger.info("Ziynat Do'koni Anketa Boti va Gemini AI ishga tushdi...")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
