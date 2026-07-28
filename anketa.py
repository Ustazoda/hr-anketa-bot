import os
import logging
import json
import asyncio
from aiohttp import web
from google import genai
from google.genai import types
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, KeyboardButton, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# === SOZLAMALAR (Environment Variables orqali olinadi) ===
BOT_TOKEN = os.getenv("BOT_TOKEN", "7634467401:AAGBpV1MoC0qzeo1_8OS0bXcc6NZ3_uQubI")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1168952611"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")

# Yangi Gemini Client
ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Faol va ishlaydigan Gemini modellar
VALIDATION_MODELS = ['gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-2.5-flash']
ANALYSIS_MODELS = ['gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-2.5-flash']


def call_gemini_with_fallback(contents, models):
    """Modellarni birma-bir sinab ko'radi."""
    last_error = None
    for model_name in models:
        try:
            return ai_client.models.generate_content(model=model_name, contents=contents)
        except Exception as e:
            last_error = e
            logging.warning(f"Model '{model_name}' ishlamadi ({e}). Keyingisiga o'tilmoqda...")
            continue
    raise last_error


# === RENDER UCHUN DUMMY VEB-SERVER (24/7 Bepul ishlashi uchun) ===
async def start_dummy_server():
    async def handle_ping(request):
        return web.Response(text="HR Anketa Bot is running 24/7 on Render!")

    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Render Veb-server {port}-portda ishga tushdi.")


# === TELEGRAM MENU TUGMASINI SOZLASH VA SERVERNI YOQISH ===
async def post_init(application):
    commands = [
        BotCommand("start", "Anketani boshlash 🚀"),
        BotCommand("cancel", "Anketani bekor qilish ❌")
    ]
    await application.bot.set_my_commands(commands)
    # Render uchun foniy port ochish
    await start_dummy_server()


# === BOSQICHLAR ===
(
    PHOTO, POSITION, FULL_NAME, BIRTH_DATE, NATIONALITY, BIRTH_PLACE, ADDRESS,
    HOUSING, PHONE, EDUCATION_LEVEL, EDU_DETAILS, WORK_EXP,
    TRIP_ABROAD, TRIP_ABROAD_DETAILS, MARITAL_STATUS, FAMILY_MEMBERS,
    BUSINESS_TRIP, MILITARY, CRIMINAL, CAR, DRIVER_LICENSE,
    LANGUAGES, COMPUTER, HOW_HEARD, GUARANTOR, REFERENCE,
    BACKGROUND_CHECK, PREV_SALARY, EXPECTED_SALARY, WORK_DURATION,
    OVERTIME, MEETINGS, TEAMWORK, PARENTS_CALL, HEALTH, ADDITIONAL
) = range(36)

QUESTIONS = {
    POSITION: "Qaysi bo'lim va lavozimga topshiryapsiz?",
    FULL_NAME: "Familiya, ism-sharifingiz",
    BIRTH_DATE: "Tug'ilgan sanangiz",
    NATIONALITY: "Millatingiz",
    BIRTH_PLACE: "Tug'ilgan joyingiz",
    ADDRESS: "Doimiy yashash joyingiz",
    PHONE: "Shaxsiy mobil telefon raqamingiz",
    EDU_DETAILS: "Qachon va qaysi o'quv yurtini tamomlagansiz",
    WORK_EXP: "O'qishdan keyin qaysi korxona/tashkilotlarda ishlagansiz",
    TRIP_ABROAD_DETAILS: "Chet elga qachon, qaerga va nima sababdan chiqqansiz",
    FAMILY_MEMBERS: "Oila a'zolaringiz haqida ma'lumot",
    MILITARY: "Harbiy xizmatda bo'lganmisiz",
    CRIMINAL: "Sudlanganmisiz",
    CAR: "Shaxsiy avtomobilingiz bormi",
    DRIVER_LICENSE: "Haydovchilik guvohnomasi",
    LANGUAGES: "Xorijiy tillarni bilish darajangiz",
    COMPUTER: "Kompyuter dasturlarida ishlash darajangiz",
    HOW_HEARD: "Korxona haqida qaerdan ma'lumot oldingiz",
    GUARANTOR: "Kafolat bera oladigan shaxs",
    REFERENCE: "Tavsiya xati bera oladigan shaxs",
    PREV_SALARY: "Oxirgi ish o'rningizdagi maosh",
    EXPECTED_SALARY: "Kutilayotgan maosh",
    WORK_DURATION: "Qancha muddat ishlamoqchisiz",
    TEAMWORK: "Kollektiv deganda nimani tushunasiz",
    HEALTH: "Sog'ligingizda muammo yo'qmi",
    ADDITIONAL: "O'zingiz haqingizda qo'shimcha ma'lumot",
}


# ==================== AI VALIDATSIYA ====================

async def validate_answer(question: str, answer: str) -> dict:
    prompt = f"""Siz ishga qabul anketasini tekshiruvchi yordamchisiz.
Savol: "{question}"
Foydalanuvchi javobi: "{answer}"

Ushbu javob savolga mantiqan mos keladimi?
Faqat JSON formatida javob bering:
{{"valid": true yoki false, "reason": "qisqa sabab, o'zbek tilida"}}"""

    try:
        response = call_gemini_with_fallback(prompt, VALIDATION_MODELS)
        text = response.text.strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return result if "valid" in result else {"valid": True, "reason": ""}
    except Exception as e:
        logging.error(f"Validatsiyada xatolik: {e}")
        return {"valid": True, "reason": ""}


async def validate_photo(photo_bytes: bytes) -> dict:
    prompt = """Bu rasmda aniq bitta odamning yuzi ko'rinib turibdimi (portret yoki selfie)?
Faqat JSON formatida javob bering:
{"is_person": true yoki false, "reason": "qisqa sabab, o'zbek tilida"}"""

    try:
        contents = [
            types.Part.from_bytes(data=bytes(photo_bytes), mime_type='image/jpeg'),
            prompt,
        ]
        response = call_gemini_with_fallback(contents, VALIDATION_MODELS)
        text = response.text.strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(text)
        return result if "is_person" in result else {"is_person": True, "reason": ""}
    except Exception as e:
        logging.error(f"Rasm validatsiyasida xatolik: {e}")
        return {"is_person": True, "reason": ""}


async def analyze_candidate_with_ai(user_data: dict) -> str:
    prompt = f"""
Siz professional HR menejeri va psixologsiz. Quyida nomzodning to'liq anketasi berilgan.

NOMZOD MA'LUMOTLARI:
- Lavozim va Bo'lim: {user_data.get('position')}
- F.I.Sh: {user_data.get('fullname')}
- Tug'ilgan sanasi: {user_data.get('birthdate')}
- Millati: {user_data.get('nationality')}
- Tug'ilgan joyi: {user_data.get('birthplace')}
- Manzili (propiska): {user_data.get('address')}
- Yashash sharoiti: {user_data.get('housing')}
- Tel: {user_data.get('phone')}
- Ma'lumoti va o'quv yurti: {user_data.get('education_level')} / {user_data.get('edu_details')}
- Ish tajribasi: {user_data.get('work_exp')}
- Chet el safarlari: {user_data.get('trip_abroad')} ({user_data.get('trip_abroad_details', 'Yo\'q')})
- Oilaviy ahvoli: {user_data.get('marital_status')}
- Oila a'zolari: {user_data.get('family_members')}
- Komandirovka / Overtime / Majlislar: {user_data.get('business_trip')} / {user_data.get('overtime')} / {user_data.get('meetings')}
- Harbiy xizmat / Sudlanganlik: {user_data.get('military')} / {user_data.get('criminal')}
- Avto / Haydovchilik: {user_data.get('car')} / {user_data.get('driver_license')}
- Tillar va Kompyuter: {user_data.get('languages')} / {user_data.get('computer')}
- Korxona haqida qaerdan eshitgani: {user_data.get('how_heard')}
- Kafillik va Tavsiya: {user_data.get('guarantor')} / {user_data.get('reference')}
- Surishtirishga roziligi: {user_data.get('background_check')}
- Oldingi va Kutilayotgan maosh: {user_data.get('prev_salary')} / {user_data.get('expected_salary')}
- Ishlash muddati: {user_data.get('work_duration')}
- Kollektiv va Sog'liq: {user_data.get('teamwork')} / {user_data.get('health')}
- Ota-onani chaqirish: {user_data.get('parents_call')}
- Sifatlari: {user_data.get('additional')}

QUYIDAGI MEZONLAR BO'YICHA HR UCHUN TAHLIL BERING (O'zbek tilida):
1. **Bilimi va Salohiyati (1-10 ball)**
2. **G'ayrati va Motivatsiyasi (1-10 ball)**
3. **Mantiq va Samimiylik (Rostg'oylik)**
4. **Kuchli va Kuchsiz tomonlari**
5. **YAKUNIY XULOSA VA TAVSIYA (1-10 ball va tavsiya)**
"""
    try:
        response = call_gemini_with_fallback(prompt, ANALYSIS_MODELS)
        return response.text
    except Exception as e:
        logging.error(f"Gemini AI xatoligi: {e}")
        return "⚠️ Sun'iy intellekt tahlilida xatolik yuz berdi."


async def process_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE, current_state: int, field_name: str, next_question: str, next_state: int, keyboard=None):
    answer = update.message.text
    question_text = QUESTIONS.get(current_state, "")

    if question_text:
        result = await validate_answer(question_text, answer)
        if not result.get("valid", True):
            await update.message.reply_text(f"⚠️ {result.get('reason', 'Javob savolga mos emas.')}\n\nIltimos, qaytadan kiriting:\n{question_text}")
            return current_state

    context.user_data[field_name] = answer
    reply_markup = keyboard if keyboard else ReplyKeyboardRemove()
    await update.message.reply_text(next_question, reply_markup=reply_markup)
    return next_state


async def safe_send_message(bot, chat_id, text, parse_mode="Markdown"):
    try:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode)
    except Exception:
        await bot.send_message(chat_id=chat_id, text=text)


# ==================== HANDLERS ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Assalomu alaykum! Ishga qabul qilish anketasiga xush kelibsiz.\n\n"
        "Iltimos, anketaga biriktirish uchun o'zingizning rasmingizni yuboring (yoki matn yozib o'tkazib yuboring):"
    )
    return PHOTO


async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()

        checking_msg = await update.message.reply_text("⏳ Rasmingiz tekshirilmoqda...")
        result = await validate_photo(photo_bytes)

        try:
            await checking_msg.delete()
        except Exception:
            pass

        if not result.get("is_person", True):
            await update.message.reply_text(f"❌ {result.get('reason', 'Bu rasm mos emas.')}\n\nIltimos, yuzingiz aniq ko'ringan haqiqiy suratingizni yuboring:")
            return PHOTO

        context.user_data['photo'] = update.message.photo[-1].file_id
    else:
        context.user_data['photo'] = None

    await update.message.reply_text("Qaysi bo'lim va lavozimga topshiryapsiz?\n(Masalan: *Sotuv bo'limi - Menejer*)", parse_mode="Markdown")
    return POSITION


async def get_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, POSITION, 'position', "Familiya, ism-sharifingizni to'liq kiriting:", FULL_NAME)

async def get_fullname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, FULL_NAME, 'fullname', "Tug'ilgan sanangiz (Masalan: 15.05.1998):", BIRTH_DATE)

async def get_birthdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, BIRTH_DATE, 'birthdate', "Millatingiz (Masalan: O'zbek, Rus):", NATIONALITY)

async def get_nationality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, NATIONALITY, 'nationality', "Tug'ilgan joyingiz (davlat, viloyat, tuman, shahar/qishloq):", BIRTH_PLACE)

async def get_birthplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, BIRTH_PLACE, 'birthplace', "Doimiy yashash joyingiz (propiska adresi):", ADDRESS)

async def get_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Hovli", "Dom"]], resize_keyboard=True, one_time_keyboard=True)
    return await process_text_step(update, context, ADDRESS, 'address', "Yashash sharoitingizni tanlang:", HOUSING, keyboard=keyboard)

async def get_housing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['housing'] = update.message.text
    reply_keyboard = [[KeyboardButton("📱 Telefon raqamni yuborish", request_contact=True)]]
    await update.message.reply_text("Shaxsiy mobil telefon raqamingizni yuboring:", reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True, one_time_keyboard=True))
    return PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.contact.phone_number if update.message.contact else update.message.text
    context.user_data['phone'] = phone
    keyboard = ReplyKeyboardMarkup([["Oliy", "O'rta maxsus", "O'rta"]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Ma'lumotingiz darajasi:", reply_markup=keyboard)
    return EDUCATION_LEVEL

async def get_education_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['education_level'] = update.message.text
    await update.message.reply_text("Qachon va qaysi o'quv yurtini tamomlagansiz?\n(O'quv yili, o'quv yurti nomi va fakultetingiz):", reply_markup=ReplyKeyboardRemove())
    return EDU_DETAILS

async def get_edudetails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, EDU_DETAILS, 'edu_details', "O'qishdan keyin qaysi korxona yoki tashkilotlarda va qaysi lavozimlarda ishlagansiz?\n(Ishga kirgan/ketgan sana, tashkilot nomi, mas'uliyatingiz va bo'shash sababi):", WORK_EXP)

async def get_workexp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    return await process_text_step(update, context, WORK_EXP, 'work_exp', "Chet el safariga chiqqanmisiz?", TRIP_ABROAD, keyboard=keyboard)

async def get_trip_abroad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data['trip_abroad'] = text
    if text.strip().lower() == "ha":
        await update.message.reply_text("Chet elga qachon, qaerga va nima sababdan chiqqansiz?", reply_markup=ReplyKeyboardRemove())
        return TRIP_ABROAD_DETAILS
    else:
        context.user_data['trip_abroad_details'] = "Yo'q"
        keyboard = ReplyKeyboardMarkup([["Turmush qurgan", "Turmush qurmagan"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("Oilaviy ahvolingiz:", reply_markup=keyboard)
        return MARITAL_STATUS

async def get_trip_abroad_details(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Turmush qurgan", "Turmush qurmagan"]], resize_keyboard=True, one_time_keyboard=True)
    return await process_text_step(update, context, TRIP_ABROAD_DETAILS, 'trip_abroad_details', "Oilaviy ahvolingiz:", MARITAL_STATUS, keyboard=keyboard)

async def get_marital_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['marital_status'] = update.message.text
    await update.message.reply_text("Oila a'zolaringiz haqida ma'lumot bering:\n(Oila a'zosi, F.I.Sh., tug'ilgan sanasi, ish joyi/lavozimi, tel raqami, manzili va sudlangan/sudlanmaganligi):", reply_markup=ReplyKeyboardRemove())
    return FAMILY_MEMBERS

async def get_family_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    return await process_text_step(update, context, FAMILY_MEMBERS, 'family_members', "Korxona tomonidan xizmat safariga (komandirovka) chiqishga rozimisiz?", BUSINESS_TRIP, keyboard=keyboard)

async def get_business_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['business_trip'] = update.message.text
    await update.message.reply_text("Harbiy xizmatda bo'lganmisiz? (Qachon va qancha muddatga / Bo'lmaganman):", reply_markup=ReplyKeyboardRemove())
    return MILITARY

async def get_military(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, MILITARY, 'military', "Sudlanganmisiz? (Agar sudlangan bo'lsangiz sababi / Sudlanmaganman):", CRIMINAL)

async def get_criminal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, CRIMINAL, 'criminal', "Shaxsiy avtomobilingiz bormi? Qaysi rusumda? (Bor - rusumi / Yo'q):", CAR)

async def get_car(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, CAR, 'car', "Haydovchilik guvohnomangiz bormi? Qaysi toifa (A, B, C, D, E) / Yo'q:", DRIVER_LICENSE)

async def get_driver_license(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, DRIVER_LICENSE, 'driver_license', "Xorijiy tillarni bilish darajangiz:\n(O'zbek, Rus, Ingliz va h.k. - So'zlashuv, Yoziash, O'qish darajasi):", LANGUAGES)

async def get_languages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, LANGUAGES, 'languages', "Qaysi kompyuter dasturlarida ilgari ishlagansiz?\n(OS, Office, 1C, AutoCAD, Photoshop va h.k.):", COMPUTER)

async def get_computer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, COMPUTER, 'computer', "Bizning korxona haqida qaerdan ma'lumot oldingiz yoki kim sizga taklif qildi?", HOW_HEARD)

async def get_how_heard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, HOW_HEARD, 'how_heard', "Sizni korxonada ishlashingizga kafolat bera oladigan shaxs:\n(F.I.Sh, ish joyi, lavozimi, telefon raqami):", GUARANTOR)

async def get_guarantor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, GUARANTOR, 'guarantor', "Oxirgi ish joyingizdan kim sizga tavsiya xati bera oladi?\n(F.I.Sh, ish joyi, lavozimi, telefon raqami):", REFERENCE)

async def get_reference(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    return await process_text_step(update, context, REFERENCE, 'reference', "Oxirgi ish joyingizdan surishtirishimizga rozimisiz?", BACKGROUND_CHECK, keyboard=keyboard)

async def get_background_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['background_check'] = update.message.text
    await update.message.reply_text("Oxirgi ish o'rningizdagi oylik maoshingiz qancha edi?", reply_markup=ReplyKeyboardRemove())
    return PREV_SALARY

async def get_prev_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, PREV_SALARY, 'prev_salary', "Bizda qancha miqdordagi maoshga ishlamoqchisiz?", EXPECTED_SALARY)

async def get_expected_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, EXPECTED_SALARY, 'expected_salary', "Bizning korxonada qancha muddat ishlamoqchisiz?", WORK_DURATION)

async def get_work_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    return await process_text_step(update, context, WORK_DURATION, 'work_duration', "Ishdan keyin ham qolib ishlash kerak bo'lib qolsa ishlaysizmi?", OVERTIME, keyboard=keyboard)

async def get_overtime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    context.user_data['overtime'] = update.message.text
    await update.message.reply_text("Korxona majlislariga qatnashishga rozimisiz?", reply_markup=keyboard)
    return MEETINGS

async def get_meetings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['meetings'] = update.message.text
    await update.message.reply_text("Kollektiv deganda nimani tushunasiz?", reply_markup=ReplyKeyboardRemove())
    return TEAMWORK

async def get_teamwork(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    return await process_text_step(update, context, TEAMWORK, 'teamwork', "Ota-onangizni korxonaga chaqirishimizga rozimisiz?", PARENTS_CALL, keyboard=keyboard)

async def get_parents_call(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['parents_call'] = update.message.text
    await update.message.reply_text("Sog'ligingizda muammo yo'qmi?", reply_markup=ReplyKeyboardRemove())
    return HEALTH

async def get_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, HEALTH, 'health', "O'zingiz haqingizda qo'shimcha ma'lumot (Ijobiy va salbiy taraflaringiz):", ADDITIONAL)


async def get_additional(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text
    question_text = QUESTIONS.get(ADDITIONAL, "")
    result = await validate_answer(question_text, answer)

    if not result.get("valid", True):
        await update.message.reply_text(f"⚠️ {result.get('reason', 'Javob mos emas.')}\n\nIltimos, to'g'ri javob yozing:")
        return ADDITIONAL

    context.user_data['additional'] = answer

    await update.message.reply_text(
        "Rahmat! Anketangiz qabul qilindi. Sun'iy intellekt ma'lumotlaringizni tahlil qilmoqda...",
        reply_markup=ReplyKeyboardRemove()
    )

    summary_text = (
        "📥 *YANGI ISHGA QABUL ANKETASI (TO'LIQ)*\n"
        "====================================\n\n"
        "📌 *1. SHAXSIY MA'LUMOTLAR:*\n"
        f"🎯 *Lavozim/Bo'lim:* {context.user_data.get('position')}\n"
        f"👤 *F.I.Sh:* {context.user_data.get('fullname')}\n"
        f"🎂 *Tug'ilgan sanasi:* {context.user_data.get('birthdate')}\n"
        f"🇺🇿 *Millati:* {context.user_data.get('nationality')}\n"
        f"📍 *Tug'ilgan joyi:* {context.user_data.get('birthplace')}\n"
        f"🏠 *Manzil (propiska):* {context.user_data.get('address')}\n"
        f"🏡 *Yashash sharoiti:* {context.user_data.get('housing')}\n"
        f"📞 *Tel:* {context.user_data.get('phone')}\n\n"

        "📌 *2. MA'LUMOTI VA ISH TAJRIBASI:*\n"
        f"🎓 *Ma'lumoti:* {context.user_data.get('education_level')}\n"
        f"🏫 *O'quv yurti:* {context.user_data.get('edu_details')}\n"
        f"💼 *Ish tajribasi:* {context.user_data.get('work_exp')}\n"
        f"✈️ *Chet el safarlari:* {context.user_data.get('trip_abroad')} ({context.user_data.get('trip_abroad_details')})\n\n"

        "📌 *3. OILAVIY AHVOLI VA OILASI:*\n"
        f"💍 *Oilaviy ahvoli:* {context.user_data.get('marital_status')}\n"
        f"👨‍👩‍👧‍👦 *Oila a'zolari:* {context.user_data.get('family_members')}\n\n"

        "📌 *4. SHAXSIY HUDUD VA KO'NIKMALAR:*\n"
        f"🧳 *Komandirovka:* {context.user_data.get('business_trip')}\n"
        f"🎖 *Harbiy xizmat:* {context.user_data.get('military')}\n"
        f"⚖️ *Sudlanganlik:* {context.user_data.get('criminal')}\n"
        f"🚘 *Shaxsiy avto:* {context.user_data.get('car')}\n"
        f"🪪 *Haydovchilik guvohnomasi:* {context.user_data.get('driver_license')}\n"
        f"🌐 *Tillar:* {context.user_data.get('languages')}\n"
        f"💻 *Kompyuter:* {context.user_data.get('computer')}\n\n"

        "📌 *5. KAFOLAT VA TAVSIYALAR:*\n"
        f"📢 *Qaerdan eshitgan:* {context.user_data.get('how_heard')}\n"
        f"🤝 *Kafillik beruvchi:* {context.user_data.get('guarantor')}\n"
        f"📋 *Tavsiya beruvchi:* {context.user_data.get('reference')}\n"
        f"🔍 *Surishtirishga roziligi:* {context.user_data.get('background_check')}\n\n"

        "📌 *6. ISH SHAROITLARI VA TALABLAR:*\n"
        f"💵 *Oldingi maoshi:* {context.user_data.get('prev_salary')}\n"
        f"💰 *Kutilayotgan maosh:* {context.user_data.get('expected_salary')}\n"
        f"⏳ *Ishlash muddati:* {context.user_data.get('work_duration')}\n"
        f"⏰ *Overtime (qolib ishlash):* {context.user_data.get('overtime')}\n"
        f"👥 *Majlislar:* {context.user_data.get('meetings')}\n"
        f"🤝 *Kollektiv haqida:* {context.user_data.get('teamwork')}\n"
        f"👨‍👩‍👦 *Ota-onani chaqirish:* {context.user_data.get('parents_call')}\n"
        f"🏥 *Sog'lig'i:* {context.user_data.get('health')}\n"
        f"📝 *Sifatlari:* {context.user_data.get('additional')}\n"
    )

    ai_analysis = await analyze_candidate_with_ai(context.user_data)
    ai_report_text = (
        "🤖 *GEMINI AI HR TAHLILI VA BAHOSI*\n"
        "------------------------------------\n"
        f"{ai_analysis}"
    )

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
        await safe_send_message(context.bot, ADMIN_ID, ai_report_text)

    except Exception as e:
        logging.error(f"Adminga yuborishda xatolik: {e}")

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Anketa bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            PHOTO: [MessageHandler(filters.PHOTO, get_photo), CommandHandler("skip", get_photo), MessageHandler(filters.TEXT & ~filters.COMMAND, get_photo)],
            POSITION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_position)],
            FULL_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_fullname)],
            BIRTH_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_birthdate)],
            NATIONALITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_nationality)],
            BIRTH_PLACE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_birthplace)],
            ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_address)],
            HOUSING: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_housing)],
            PHONE: [MessageHandler(filters.CONTACT, get_phone), MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            EDUCATION_LEVEL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_education_level)],
            EDU_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_edudetails)],
            WORK_EXP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_workexp)],
            TRIP_ABROAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trip_abroad)],
            TRIP_ABROAD_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_trip_abroad_details)],
            MARITAL_STATUS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_marital_status)],
            FAMILY_MEMBERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_family_members)],
            BUSINESS_TRIP: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_business_trip)],
            MILITARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_military)],
            CRIMINAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_criminal)],
            CAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_car)],
            DRIVER_LICENSE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_driver_license)],
            LANGUAGES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_languages)],
            COMPUTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_computer)],
            HOW_HEARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_how_heard)],
            GUARANTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_guarantor)],
            REFERENCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_reference)],
            BACKGROUND_CHECK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_background_check)],
            PREV_SALARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_prev_salary)],
            EXPECTED_SALARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_expected_salary)],
            WORK_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_work_duration)],
            OVERTIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_overtime)],
            MEETINGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_meetings)],
            TEAMWORK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_teamwork)],
            PARENTS_CALL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_parents_call)],
            HEALTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_health)],
            ADDITIONAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_additional)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    print("Mukammal ANKETA boti va Gemini AI ishga tushdi...")
    app.run_polling()


if __name__ == "__main__":
    main()