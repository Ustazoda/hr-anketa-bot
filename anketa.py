import os
import logging
import json
import re
from aiohttp import web
from google import genai
from google.genai import types
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# === SOZLAMALAR ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1168952611"))

# Gemini API Keylarini olish
RAW_KEYS = os.getenv("GEMINI_API_KEY", "")
GEMINI_API_KEYS = [k.strip() for k in RAW_KEYS.split(",") if k.strip()]

# Rasmiy va faol Google Gemini Modellari
VALIDATION_MODELS = ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-2.0-flash-exp']
ANALYSIS_MODELS = ['gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-2.0-flash-exp']

# Tayyor tugma javoblari (AI tekshiruvisiz darhol o'tkaziladi)
PREDEFINED_BUTTONS = [
    "ha", "yo'q", "uylangan", "uylanmagan", "turmush qurgan", "turmush qurmagan",
    "o'zbek", "rus", "tojik", "hovli", "dom", "o'rta", "o'rta maxsus", "oliy",
    "harbiyda bo'lganman", "harbiyda bo'lmaganman", "sudlanganman", "sudlanmaganman",
    "ha, avvalgi do'konda sahifani yuritganman", "yo'q, lekin tez o'rganib olaman"
]


def call_gemini_with_fallback(contents, models):
    """Mavjud barcha API Kalitlar va Modellarni birma-bir sinab ko'radi."""
    last_error = None
    
    if not GEMINI_API_KEYS:
        raise ValueError("GEMINI_API_KEY environment variable topilmadi!")

    for api_key in GEMINI_API_KEYS:
        try:
            client = genai.Client(api_key=api_key)
            for model_name in models:
                try:
                    return client.models.generate_content(model=model_name, contents=contents)
                except Exception as e:
                    last_error = e
                    logging.warning(f"Model '{model_name}' ishlamadi ({e}). Keyingisiga o'tilmoqda...")
                    continue
        except Exception as e:
            last_error = e
            logging.warning(f"API Key ishlamadi ({e}). Zaxiradagi kalitga o'tilmoqda...")
            continue
            
    raise last_error


# === RENDER PORTI VA UPTIMEROBOT UCHUN DUMMY SERVER ===
async def start_dummy_server():
    async def handle_ping(request):
        return web.Response(text="Ziynat HR Anketa Bot is running on Render!", status=200)

    app = web.Application()
    app.router.add_route("*", "/", handle_ping)
    
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Render Veb-server {port}-portda ishga tushdi.")


# === TELEGRAM MENU TUGMASINI SOZLASH ===
async def post_init(application):
    commands = [
        BotCommand("start", "Anketani boshlash 🚀"),
        BotCommand("cancel", "Anketani bekor qilish ❌")
    ]
    await application.bot.set_my_commands(commands)
    await start_dummy_server()


# === BOSQICHLAR ===
(
    PHOTO, POSITION, FULL_NAME, BIRTH_DATE, NATIONALITY, ADDRESS, HOUSING, PHONE,
    EDUCATION_LEVEL, EDU_DETAILS, WORK_EXP,
    VIDEO_SKILLS, EDITING_APPS, SMM_EXP, STORE_DUTIES,
    TRIP_ABROAD, TRIP_ABROAD_DETAILS, MARITAL_STATUS, FAMILY_MEMBERS, 
    MILITARY, CRIMINAL,
    LANGUAGES, HOW_HEARD, GUARANTOR, BACKGROUND_CHECK, PREV_SALARY, EXPECTED_SALARY,
    WORK_DURATION, OVERTIME, HEALTH, ADDITIONAL
) = range(31)

QUESTIONS = {
    POSITION: "Qaysi bo'lim va lavozimga topshiryapsiz?",
    FULL_NAME: "Familiya, ism-sharifingiz",
    BIRTH_DATE: "Tug'ilgan sanangiz",
    NATIONALITY: "Millatingiz",
    ADDRESS: "Doimiy yashash joyingiz",
    EDU_DETAILS: "Qachon va qaysi o'quv yurtini tamomlagansiz",
    WORK_EXP: "Avval qaysi korxona/do'konlarda ishlagansiz",
    VIDEO_SKILLS: "Telefon va video olish tajribangiz",
    EDITING_APPS: "Montaj ilovalari (CapCut va b.)",
    SMM_EXP: "Instagram/Telegram va mijozlarga javob berish",
    STORE_DUTIES: "Do'kon vazifalari va tovarlar bilan ishlash",
    TRIP_ABROAD_DETAILS: "Chet el safarlari tafsiloti",
    FAMILY_MEMBERS: "Oila a'zolaringiz haqida ma'lumot",
    MILITARY: "Harbiy xizmatda bo'lganmisiz",
    CRIMINAL: "Sudlanganlik holatingiz",
    LANGUAGES: "Xorijiy tillarni bilish darajangiz",
    HOW_HEARD: "Bizning 'Ziynat' do'konimiz haqida qayerdan eshitdingiz?",
    GUARANTOR: "Kafillik yoki tavsiya bera oladigan shaxs",
    PREV_SALARY: "Oldingi ish joyingizdagi maoshingiz",
    EXPECTED_SALARY: "Kutilayotgan maosh",
    WORK_DURATION: "Qancha muddat ishlamoqchisiz",
    HEALTH: "Sog'ligingizda muammolar yo'qmi?",
    ADDITIONAL: "O'zingiz haqingizda qo'shimcha ma'lumot",
}


# ==================== YUMSHATILGAN AI VALIDATSIYA ====================

async def validate_answer(question: str, answer: str) -> dict:
    clean_ans = answer.strip().lower()
    
    # Agar javob tayyor tugmalardan biri bo'lsa, AI'siz darhol o'tkazadi!
    if clean_ans in PREDEFINED_BUTTONS:
        return {"valid": True, "reason": ""}

    prompt = f"""Siz "Ziynat" do'koni ishga qabul anketasini tekshiruvchi bag'rikeng va tushunuvchan yordamchisiz.
Savol: "{question}"
Foydalanuvchi javobi: "{answer}"

VAZIFA:
Foydalanuvchi javobi savolga mantiqan mos keladimi?

MUHIM QOIDALAR:
1. Agar javob savolga umuman aloqasiz bo'lsa (masalan: so'kish, "asdfgh", "kjkhhi87to8f8ot60", yoki "shahar" degan savolga "ovqat" deb javob berilgan bo'lsa) -> valid: false.
2. Oddiy, so'zlashuv tilidagi, qisqa yoki xatolar bilan yozilgan javoblarni ("yomon", "yo'q", "sog'lomman", "sog'lig'im joyida", "kasalligim yo'q", "ishlamaganman", "o'rganaman", "uylanmaganman", "uylangan", "harbiyda bo'lmaganman", "sudlanmaganman", "sudlanganman") HAMMA VAQT to'g'ri deb qabul qiling -> valid: true.

Faqat JSON formatida javob bering:
{{"valid": true yoki false, "reason": "agar valid false bo'lsa, qisqa sababini o'zbek tilida yozing"}}"""

    try:
        response = call_gemini_with_fallback(prompt, VALIDATION_MODELS)
        text = response.text.strip()
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        return {"valid": True, "reason": ""}
    except Exception as e:
        logging.error(f"Validatsiyada xatolik: {e}")
        return {"valid": True, "reason": ""}


async def validate_photo(photo_bytes: bytes) -> dict:
    prompt = """Siz fotosuratlarni tahlil qiluvchi mutaxassisiz.
Ushbu rasmda INSON YUZI (portret, selfie yoki odam qiyofasi) ko'rinib turibdimi?

QOIDALAR:
1. Buyumlar, hujjat, mashina, avtomobil, hayvonlar -> "is_person": false
2. Inson yuzi ko'ringan bo'lsa -> "is_person": true

FAQAT ushbu JSON formatida javob bering:
{"is_person": true yoki false, "reason": "qisqa sabab o'zbek tilida"}"""

    try:
        contents = [
            types.Part.from_bytes(data=bytes(photo_bytes), mime_type='image/jpeg'),
            prompt,
        ]
        response = call_gemini_with_fallback(contents, VALIDATION_MODELS)
        text = response.text.strip().lower()

        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                result = json.loads(match.group(0))
                if "is_person" in result:
                    return result
            except Exception:
                pass

        if "false" in text or "yo'q" in text or "mashina" in text or "avtomobil" in text or "buyum" in text:
            return {"is_person": False, "reason": "Rasmda inson yuzi ko'rinmayapti."}
        elif "true" in text or "ha" in text:
            return {"is_person": True, "reason": "Rasmda inson ko'rinib turibdi."}

        return {"is_person": True, "reason": ""}

    except Exception as e:
        logging.error(f"Rasm validatsiyasida xatolik: {e}")
        return {"is_person": True, "reason": ""}


async def analyze_candidate_with_ai(user_data: dict) -> str:
    prompt = f"""
Siz "ZIYNAT" bijuteriya va soatlar do'konining HR menejerisiz.
Do'konda xodimlar mijozlarga soat va zargarlik buyumlarini sotishi, tovarlarni chiroyli joylashtirishi, mobil telefon orqali video olib, CapCut kabi ilovalarda montaj qilishi hamda Instagram/Telegram'da mijozlar bilan muloqot qilishi kerak.

NOMZOD MA'LUMOTLARI:
- F.I.Sh va Lavozim: {user_data.get('fullname')} / {user_data.get('position')}
- Yoshi, Millati va Manzili: {user_data.get('birthdate')} / {user_data.get('nationality')} / {user_data.get('address')} ({user_data.get('housing')})
- Tel: {user_data.get('phone')}
- Ma'lumoti va Ish tajribasi: {user_data.get('education_level')} / {user_data.get('edu_details')} | Tajriba: {user_data.get('work_exp')}
- 📹 Video olish va Telefon: {user_data.get('video_skills')}
- 🎬 Montaj (CapCut va b.): {user_data.get('editing_apps')}
- 📱 SMM va Mijozlar bilan muloqot: {user_data.get('smm_exp')}
- 🛍 Do'kon vazifalariga tayyorligi: {user_data.get('store_duties')}
- Oilaviy ahvoli va A'zolari: {user_data.get('marital_status')} / {user_data.get('family_members')}
- Harbiy xizmat / Sudlanganlik: {user_data.get('military')} / {user_data.get('criminal')}
- Tillar: {user_data.get('languages')}
- Oldingi va Kutilayotgan maosh: {user_data.get('prev_salary')} / {user_data.get('expected_salary')}
- Ishlash muddati / Qolib ishlash: {user_data.get('work_duration')} / {user_data.get('overtime')}
- Sog'lig'i: {user_data.get('health')}
- Qo'shimcha sifatlari: {user_data.get('additional')}

QUYIDAGI MEZONLAR BO'YICHA "ZIYNAT" DO'KONI DIREKTORI UCHUN HR TAHLIL BERING (O'zbek tilida):
1. **Sotuv va Mijozlar bilan muloqot salohiyati (1-10 ball)**
2. **Video olish va Montaj (CapCut/SMM) mahorati (1-10 ball)**
3. **Mas'uliyat va Do'kon vazifalariga tayyorligi**
4. **Nomzodning kuchli va zaif tomonlari**
5. **YAKUNIY BAHO VA DIREKTORGA TAVSIYA (1-10 ball)**
"""
    try:
        response = call_gemini_with_fallback(prompt, ANALYSIS_MODELS)
        return response.text
    except Exception as e:
        logging.error(f"Gemini AI xatoligi: {e}")
        return "⚠️ Sun'iy intellekt tahlilida xatolik yuz berdi."


async def safe_reply_text(message, text, reply_markup=None, parse_mode="Markdown"):
    """Telegram Markdown xatolarida bot qotmasligi uchun xavfsiz yuboruvchi funksiya."""
    try:
        return await message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception:
        return await message.reply_text(text, reply_markup=reply_markup)


async def process_text_step(
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE, 
    current_state: int, 
    field_name: str, 
    current_question_prompt: str,
    next_question_prompt: str, 
    next_state: int, 
    keyboard=None
):
    answer = update.message.text
    question_text = QUESTIONS.get(current_state, "")

    if question_text:
        result = await validate_answer(question_text, answer)
        if not result.get("valid", True):
            await safe_reply_text(
                update.message,
                f"⚠️ {result.get('reason', 'Javob savolga mos emas.')}\n\nIltimos, ushbu savolga qaytadan javob bering:\n{current_question_prompt}"
            )
            return current_state

    context.user_data[field_name] = answer
    reply_markup = keyboard if keyboard else ReplyKeyboardRemove()
    await safe_reply_text(update.message, next_question_prompt, reply_markup=reply_markup)
    return next_state


async def safe_send_message(bot, chat_id, text, parse_mode="Markdown", reply_markup=None):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)


# ==================== TUGMALAR VA QAROR QABUL QILISH ====================

async def handle_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "done":
        return

    try:
        action, user_id = data.split("_")
    except ValueError:
        return

    if action == "accept":
        new_keyboard = [[InlineKeyboardButton("🟢 QABUL QILINGAN ✅", callback_data="done")]]
        user_msg = "🎉 *Tabriklaymiz!*\n\nSizning anketangiz 'Ziynat' do'koni rahbariyat tomonidan ijobiy baholandi va suhbatga taklif qilinasiz! Tez orada siz bilan bog'lanamiz."
    elif action == "reject":
        new_keyboard = [[InlineKeyboardButton("🔴 RAD ETILDI ❌", callback_data="done")]]
        user_msg = "Assalomu alaykum.\n\nAfsuski, sizning anketangiz hozirgi vaqtda bizning talablarimizga mos kelmadi. Anketani to'ldirganingiz uchun rahmat, kelgusi ishlaringizda omad tilaymiz!"
    else:
        return

    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_keyboard))

    try:
        await context.bot.send_message(chat_id=int(user_id), text=user_msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Nomzodga qarorni yuborishda xatolik: {e}")


# ==================== HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(
        update.message,
        "Assalomu alaykum! 'Ziynat' bijuteriya va soatlar do'koni ishga qabul anketasiga xush kelibsiz. ✨\n\n"
        "📸 Iltimos, anketaga biriktirish uchun o'zingizning rasmingizni yuboring:\n"
        "*(Yuzingiz aniq ko'ringan tushunarli rasm yuboring)*"
    )
    return PHOTO


async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await safe_reply_text(update.message, "⚠️ Rasm yuborish majburiy! Iltimos, faqat o'zingizning rasmingizni yuboring.")
        return PHOTO

    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()

    checking_msg = await safe_reply_text(update.message, "⏳ Rasmingiz tekshirilmoqda...")
    result = await validate_photo(photo_bytes)

    try:
        await checking_msg.delete()
    except Exception:
        pass

    if not result.get("is_person", True):
        await safe_reply_text(
            update.message,
            f"❌ {result.get('reason', 'Bu rasmda inson yuzi ko\'rinmayapti.')}\n\n"
            "Iltimos, yuzingiz aniq ko'ringan haqiqiy suratingizni yuboring:"
        )
        return PHOTO

    context.user_data['photo'] = update.message.photo[-1].file_id

    await safe_reply_text(
        update.message,
        "Qaysi bo'lim va lavozimga topshiryapsiz?\n\n*(Misol: Do'kon sotuvchisi va kontent-menejer)*"
    )
    return POSITION


async def get_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, POSITION, 'position',
        "Qaysi bo'lim va lavozimga topshiryapsiz?\n\n*(Misol: Do'kon sotuvchisi va kontent-menejer)*",
        "Familiya, ism va sharifingizni kiriting:\n\n*(Misol: Abdullayeva Dilnoza Karim qizi)*",
        FULL_NAME
    )

async def get_fullname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, FULL_NAME, 'fullname',
        "Familiya, ism va sharifingizni kiriting:\n\n*(Misol: Abdullayeva Dilnoza Karim qizi)*",
        "Tug'ilgan sanangizni kiriting:\n\n*(Misol: 15.05.2001)*",
        BIRTH_DATE
    )

async def get_birthdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["O'zbek", "Rus", "Tojik"]], resize_keyboard=True, one_time_keyboard=True)
    return await process_text_step(
        update, context, BIRTH_DATE, 'birthdate',
        "Tug'ilgan sanangizni kiriting:\n\n*(Misol: 15.05.2001)*",
        "Millatingizni tanlang yoki kiriting:",
        NATIONALITY,
        keyboard=keyboard
    )

async def get_nationality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, NATIONALITY, 'nationality',
        "Millatingizni tanlang yoki kiriting:",
        "Doimiy yashash joyingiz (propiska manzilingiz):\n\n*(Misol: Toshkent sh., Chilonzor tumani, 5-mavze)*",
        ADDRESS
    )

async def get_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Hovli", "Dom"]], resize_keyboard=True, one_time_keyboard=True)
    context.user_data['address'] = update.message.text
    await safe_reply_text(update.message, "Yashash sharoitingizni tanlang:", reply_markup=keyboard)
    return HOUSING

async def get_housing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['housing'] = update.message.text
    reply_keyboard = [[KeyboardButton("📱 Telefon raqamni yuborish", request_contact=True)]]
    await safe_reply_text(update.message, "Shaxsiy mobil telefon raqamingizni yuboring:", reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True, one_time_keyboard=True))
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.contact.phone_number if update.message.contact else update.message.text
    context.user_data['phone'] = phone
    keyboard = ReplyKeyboardMarkup([["O'rta", "O'rta maxsus", "Oliy"]], resize_keyboard=True, one_time_keyboard=True)
    await safe_reply_text(update.message, "Ma'lumotingiz darajasi:", reply_markup=keyboard)
    return EDUCATION_LEVEL

async def get_education_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['education_level'] = update.message.text
    await safe_reply_text(update.message, "Qachon va qaysi o'quv yurtini tamomlagansiz?\n\n*(Misol: 2022-yil, Toshkent Moliya Instituti)*", reply_markup=ReplyKeyboardRemove())
    return EDU_DETAILS

async def get_edudetails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, EDU_DETAILS, 'edu_details',
        "Qachon va qaysi o'quv yurtini tamomlagansiz?\n\n*(Misol: 2022-yil, Toshkent Moliya Instituti)*",
        "Avval qaysi korxona yoki do'konlarda va qanday lavozimda ishlagansiz?\n\n*(Misol: 2023-yil, 'X' kiyim do'konida sotuvchi bo'lib 1 yil ishlaganman)*",
        WORK_EXP
    )

async def get_workexp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, WORK_EXP, 'work_exp',
        "Avval qaysi korxona yoki do'konlarda va qanday lavozimda ishlagansiz?\n\n*(Misol: 2023-yil, 'X' kiyim do'konida sotuvchi bo'lib 1 yil ishlaganman)*",
        "📱 Telefoningizda sifatli video ololaysizmi va qaysi rusumdagi telefondan foydalanasiz?\n\n*(Misol: Ha, video olaman. Telefonim iPhone 13 / Samsung S21)*",
        VIDEO_SKILLS
    )

async def get_video_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, VIDEO_SKILLS, 'video_skills',
        "📱 Telefoningizda sifatli video ololaysizmi va qaysi rusumdagi telefondan foydalanasiz?\n\n*(Misol: Ha, video olaman. Telefonim iPhone 13 / Samsung S21)*",
        "🎬 Videolarni qaysi ilovalarda montaj qilasiz va qaysi birida yaxshi ishlay olasiz?\n\n*(Misol: CapCut, InShot, VN. CapCut dasturida juda yaxshi montaj qilaman)*",
        EDITING_APPS
    )

async def get_editing_apps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([
        ["Ha, avvalgi do'konda sahifani yuritganman"],
        ["Yo'q, lekin tez o'rganib olaman"]
    ], resize_keyboard=True, one_time_keyboard=True)

    return await process_text_step(
        update, context, EDITING_APPS, 'editing_apps',
        "🎬 Videolarni qaysi ilovalarda montaj qilasiz va qaysi birida yaxshi ishlay olasiz?\n\n*(Misol: CapCut, InShot, VN. CapCut dasturida juda yaxshi montaj qilaman)*",
        "💬 Instagram va Telegram'ga video joylash hamda mijozlar xabarlariga (DM/Comment) javob berish tajribangiz bormi?",
        SMM_EXP,
        keyboard=keyboard
    )

async def get_smm_exp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)

    return await process_text_step(
        update, context, SMM_EXP, 'smm_exp',
        "💬 Instagram va Telegram'ga video joylash hamda mijozlar xabarlariga (DM/Comment) javob berish tajribangiz bormi?",
        "🛍 Do'konda tovarlarni (soat/bijuteriya) chiroyli joylashtirish, mijozlar bilan muloqot qilish va video olish vazifalarini bajara olasizmi?",
        STORE_DUTIES,
        keyboard=keyboard
    )

async def get_store_duties(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)

    return await process_text_step(
        update, context, STORE_DUTIES, 'store_duties',
        "🛍 Do'konda tovarlarni (soat/bijuteriya) chiroyli joylashtirish, mijozlar bilan muloqot qilish va video olish vazifalarini bajara olasizmi?",
        "Chet el safariga chiqqanmisiz?",
        TRIP_ABROAD,
        keyboard=keyboard
    )

async def get_trip_abroad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data['trip_abroad'] = text
    marital_keyboard = ReplyKeyboardMarkup([
        ["Uylangan", "Uylanmagan"],
        ["Turmush qurgan", "Turmush qurmagan"]
    ], resize_keyboard=True, one_time_keyboard=True)

    if text.strip().lower() == "ha":
        await safe_reply_text(update.message, "Chet elga qachon, qayerga va nima sababdan chiqqansiz?\n\n*(Misol: 2022-yil Turkiyaga vaqtinchalik sayohatga)*", reply_markup=ReplyKeyboardRemove())
        return TRIP_ABROAD_DETAILS
    else:
        context.user_data['trip_abroad_details'] = "Yo'q"
        await safe_reply_text(update.message, "Oilaviy ahvolingizni tanlang:", reply_markup=marital_keyboard)
        return MARITAL_STATUS

async def get_trip_abroad_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    marital_keyboard = ReplyKeyboardMarkup([
        ["Uylangan", "Uylanmagan"],
        ["Turmush qurgan", "Turmush qurmagan"]
    ], resize_keyboard=True, one_time_keyboard=True)

    return await process_text_step(
        update, context, TRIP_ABROAD_DETAILS, 'trip_abroad_details',
        "Chet elga qachon, qayerga va nima sababdan chiqqansiz?\n\n*(Misol: 2022-yil Turkiyaga vaqtinchalik sayohatga)*",
        "Oilaviy ahvolingizni tanlang:",
        MARITAL_STATUS,
        keyboard=marital_keyboard
    )

async def get_marital_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['marital_status'] = update.message.text
    await safe_reply_text(
        update.message,
        "Oila a'zolaringiz haqida ma'lumot bering:\n\n(F.I.Sh., tug'ilgan yili, ish joyi va sudlangan/sudlanmaganligi)",
        reply_markup=ReplyKeyboardRemove()
    )
    return FAMILY_MEMBERS

async def get_family_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    military_keyboard = ReplyKeyboardMarkup([["Harbiyda bo'lganman", "Harbiyda bo'lmaganman"]], resize_keyboard=True, one_time_keyboard=True)

    return await process_text_step(
        update, context, FAMILY_MEMBERS, 'family_members',
        "Oila a'zolaringiz haqida ma'lumot bering:\n\n(F.I.Sh., tug'ilgan yili, ish joyi va sudlangan/sudlanmaganligi)",
        "Harbiy xizmatda bo'lganmisiz?",
        MILITARY,
        keyboard=military_keyboard
    )

async def get_military(update: Update, context: ContextTypes.DEFAULT_TYPE):
    criminal_keyboard = ReplyKeyboardMarkup([["Sudlanmaganman", "Sudlanganman"]], resize_keyboard=True, one_time_keyboard=True)

    return await process_text_step(
        update, context, MILITARY, 'military',
        "Harbiy xizmatda bo'lganmisiz?",
        "Sudlanganlik holatingiz:",
        CRIMINAL,
        keyboard=criminal_keyboard
    )

async def get_criminal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, CRIMINAL, 'criminal',
        "Sudlanganlik holatingiz:",
        "Qaysi tillarni bilasiz va qaysi darajada?\n\n*(Misol: O'zbek tili - a'lo, Rus tili - so'zlashuv darajasida)*",
        LANGUAGES
    )

async def get_languages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, LANGUAGES, 'languages',
        "Qaysi tillarni bilasiz va qaysi darajada?\n\n*(Misol: O'zbek tili - a'lo, Rus tili - so'zlashuv darajasida)*",
        "Bizning 'Ziynat' do'konimiz haqida qayerdan eshitdingiz?",
        HOW_HEARD
    )

async def get_how_heard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, HOW_HEARD, 'how_heard',
        "Bizning 'Ziynat' do'konimiz haqida qayerdan eshitdingiz?",
        "Sizga kim kafillik yoki tavsiya bera oladi?\n\n*(Misol: Oxirgi ish joyimdagi rahbarim: Aliyev Vali, +998901234567)*",
        GUARANTOR
    )

async def get_guarantor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['guarantor'] = update.message.text
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    await safe_reply_text(update.message, "Oxirgi ish joyingizdan siz haqida surishtirishimizga rozimisiz?", reply_markup=keyboard)
    return BACKGROUND_CHECK

async def get_background_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['background_check'] = update.message.text
    await safe_reply_text(update.message, "Oldingi ish joyingizdagi maoshingiz qancha edi?", reply_markup=ReplyKeyboardRemove())
    return PREV_SALARY

async def get_prev_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, PREV_SALARY, 'prev_salary',
        "Oldingi ish joyingizdagi maoshingiz qancha edi?",
        "Bizda qancha miqdordagi maoshga ishlamoqchisiz?",
        EXPECTED_SALARY
    )

async def get_expected_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, EXPECTED_SALARY, 'expected_salary',
        "Bizda qancha miqdordagi maoshga ishlamoqchisiz?",
        "Bizning do'konda qancha muddat ishlamoqchisiz?",
        WORK_DURATION
    )

async def get_work_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    return await process_text_step(
        update, context, WORK_DURATION, 'work_duration',
        "Bizning do'konda qancha muddat ishlamoqchisiz?",
        "Ishdan keyin qolib ishlash (overtime) va majlislarga rozimisiz?",
        OVERTIME,
        keyboard=keyboard
    )

async def get_overtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['overtime'] = update.message.text
    await safe_reply_text(update.message, "Sog'ligingizda muammolar yo'qmi?", reply_markup=ReplyKeyboardRemove())
    return HEALTH

async def get_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, HEALTH, 'health',
        "Sog'ligingizda muammolar yo'qmi?",
        "O'zingiz haqingizda qo'shimcha ma'lumot (kuchli va ijobiy taraflaringiz):\n\n*(Misol: Kirishimli, mas'uliyatliman va muloqot qilishni yaxshi ko'raman)*",
        ADDITIONAL
    )

async def get_additional(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text
    question_text = QUESTIONS.get(ADDITIONAL, "")
    result = await validate_answer(question_text, answer)

    if not result.get("valid", True):
        await safe_reply_text(
            update.message,
            f"⚠️ {result.get('reason', 'Javob mos emas.')}\n\nIltimos, ushbu savolga qaytadan javob bering:\nO'zingiz haqingizda qo'shimcha ma'lumot (kuchli va ijobiy taraflaringiz):\n\n*(Misol: Kirishimli, mas'uliyatliman va muloqot qilishni yaxshi ko'raman)*"
        )
        return ADDITIONAL

    context.user_data['additional'] = answer
    user_id = update.message.from_user.id

    await safe_reply_text(
        update.message,
        "Rahmat! Anketangiz qabul qilindi. Sun'iy intellekt ma'lumotlaringizni tahlil qilmoqda...",
        reply_markup=ReplyKeyboardRemove()
    )

    summary_text = (
        "📥 *ZIYNAT DO'KONI — YANGI NOMZOD ANKETASI*\n"
        "====================================\n\n"
        "📌 *1. SHAXSIY MA'LUMOTLAR:*\n"
        f"🎯 *Lavozim:* {context.user_data.get('position')}\n"
        f"👤 *F.I.Sh:* {context.user_data.get('fullname')}\n"
        f"🎂 *Tug'ilgan sanasi:* {context.user_data.get('birthdate')}\n"
        f"🇺🇿 *Millati:* {context.user_data.get('nationality')}\n"
        f"🏠 *Manzil:* {context.user_data.get('address')} ({context.user_data.get('housing')})\n"
        f"📞 *Tel:* {context.user_data.get('phone')}\n\n"

        "📌 *2. MA'LUMOTI VA ISH TAJRIBASI:*\n"
        f"🎓 *Ma'lumoti:* {context.user_data.get('education_level')} ({context.user_data.get('edu_details')})\n"
        f"💼 *Ish tajribasi:* {context.user_data.get('work_exp')}\n\n"

        "📌 *3. VIDEO, MONTAJ VA SMM (TALABLAR):*\n"
        f"📹 *Video olish / Telefon:* {context.user_data.get('video_skills')}\n"
        f"🎬 *Montaj (CapCut va b.):* {context.user_data.get('editing_apps')}\n"
        f"💬 *Instagram/Telegram/Mijozlar:* {context.user_data.get('smm_exp')}\n"
        f"🛍 *Do'kon vazifalariga tayyorligi:* {context.user_data.get('store_duties')}\n\n"

        "📌 *4. OILAVIY VA SHAXSIY HUDUD:*\n"
        f"💍 *Oilaviy ahvoli:* {context.user_data.get('marital_status')}\n"
        f"👨‍👩‍👧‍👦 *Oila a'zolari:* {context.user_data.get('family_members')}\n"
        f"🎖 *Harbiy xizmat:* {context.user_data.get('military')}\n"
        f"⚖️ *Sudlanganlik:* {context.user_data.get('criminal')}\n"
        f"🌐 *Tillar:* {context.user_data.get('languages')}\n"
        f"📢 *Manba:* {context.user_data.get('how_heard')}\n"
        f"🤝 *Kafillik/Tavsiya:* {context.user_data.get('guarantor')}\n"
        f"🔍 *Surishtirishga roziligi:* {context.user_data.get('background_check')}\n\n"

        "📌 *5. SHAROITLAR VA TALABLAR:*\n"
        f"💵 *Oldingi / Kutilayotgan maosh:* {context.user_data.get('prev_salary')} / {context.user_data.get('expected_salary')}\n"
        f"⏳ *Ishlash muddati:* {context.user_data.get('work_duration')}\n"
        f"⏰ *Overtime va majlislar:* {context.user_data.get('overtime')}\n"
        f"🏥 *Sog'lig'i:* {context.user_data.get('health')}\n"
        f"📝 *Qo'shimcha:* {context.user_data.get('additional')}\n"
    )

    ai_analysis = await analyze_candidate_with_ai(context.user_data)
    ai_report_text = (
        "🤖 *GEMINI AI — HR TAHLIL VA BAHOSI*\n"
        "------------------------------------\n"
        f"{ai_analysis}"
    )

    decision_keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Qabul qilish (Suhbatga)", callback_data=f"accept_{user_id}"),
            InlineKeyboardButton("❌ Rad etish", callback_data=f"reject_{user_id}")
        ]
    ])

    try:
        photo = context.user_data.get('photo')
        if photo:
            await context.bot.send_photo(
                chat_id=ADMIN_ID,
                photo=photo,
                caption=f"📥 *YANGI NOMZOD:* {context.user_data.get('fullname')}\n🎯 *Lavozim:* {context.user_data.get('position')}",
                parse_mode="Markdown"
            )

        await safe_send_message(context.bot, ADMIN_ID, summary_text)
        await safe_send_message(context.bot, ADMIN_ID, ai_report_text, reply_markup=decision_keyboard)

    except Exception as e:
        logging.error(f"Adminga yuborishda xatolik: {e}")

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_text(update.message, "Anketa bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PHOTO: [MessageHandler(filters.PHOTO, get_photo), MessageHandler(filters.TEXT & ~filters.COMMAND, get_photo)],
            POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_position)],
            FULL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_fullname)],
            BIRTH_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_birthdate)],
            NATIONALITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_nationality)],
            ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_address)],
            HOUSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_housing)],
            PHONE: [MessageHandler(filters.CONTACT, get_phone), MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            EDUCATION_LEVEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_education_level)],
            EDU_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_edudetails)],
            WORK_EXP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_workexp)],
            VIDEO_SKILLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_video_skills)],
            EDITING_APPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_editing_apps)],
            SMM_EXP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_smm_exp)],
            STORE_DUTIES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_store_duties)],
            TRIP_ABROAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trip_abroad)],
            TRIP_ABROAD_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trip_abroad_details)],
            MARITAL_STATUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_marital_status)],
            FAMILY_MEMBERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_family_members)],
            MILITARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_military)],
            CRIMINAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_criminal)],
            LANGUAGES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_languages)],
            HOW_HEARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_how_heard)],
            GUARANTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_guarantor)],
            BACKGROUND_CHECK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_background_check)],
            PREV_SALARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_prev_salary)],
            EXPECTED_SALARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_expected_salary)],
            WORK_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_work_duration)],
            OVERTIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_overtime)],
            HEALTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_health)],
            ADDITIONAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_additional)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(handle_decision))

    print("Ziynat Do'koni Anketa Boti va Gemini AI ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()
