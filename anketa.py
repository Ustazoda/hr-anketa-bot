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
BOT_TOKEN = os.getenv("BOT_TOKEN", "7634467401:AAFrqyCa40pb-bpk1jYZBk0d4IsczVsGkhM")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1168952611"))
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Gemini Client
ai_client = genai.Client(api_key=GEMINI_API_KEY)

VALIDATION_MODELS = ['gemini-3.5-flash-lite', 'gemini-3.5-flash', 'gemini-3.1-pro-preview', 'gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-1.0-pro']
ANALYSIS_MODELS = ['gemini-3.6-flash', 'gemini-3.5-flash-lite', 'gemini-3.5-flash', 'gemini-3.1-pro-preview', 'gemini-1.5-flash', 'gemini-1.5-pro', 'gemini-1.0-pro']


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


# === RENDER PORTI VA UPTIMEROBOT UCHUN DUMMY SERVER ===
async def start_dummy_server():
    async def handle_ping(request):
        return web.Response(text="Ziynat HR Anketa Bot is running on Render!", status=200)

    app = web.Application()
    # Barcha HTTP metodlarini (GET, HEAD, POST) qabul qiladi
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


# === BOSQICHLAR (31 ta savol) ===
(
    PHOTO, POSITION, FULL_NAME, BIRTH_DATE, NATIONALITY, ADDRESS, HOUSING, PHONE,
    EDUCATION_LEVEL, EDU_DETAILS, WORK_EXP,
    VIDEO_SKILLS, EDITING_APPS, SMM_EXP, STORE_DUTIES,
    TRIP_ABROAD, TRIP_ABROAD_DETAILS, MARITAL_STATUS, FAMILY_MEMBERS, MILITARY_CRIMINAL,
    LANGUAGES, HOW_HEARD, GUARANTOR, BACKGROUND_CHECK, PREV_SALARY, EXPECTED_SALARY,
    WORK_DURATION, OVERTIME_MEETINGS, TEAMWORK_HEALTH, ADDITIONAL
) = range(30)

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
    MILITARY_CRIMINAL: "Harbiy xizmat va sudlanganlik",
    LANGUAGES: "Xorijiy tillarni bilish darajangiz",
    HOW_HEARD: "Do'konimiz haqida qayerdan eshitdingiz",
    GUARANTOR: "Kafillik yoki tavsiya bera oladigan shaxs",
    PREV_SALARY: "Oldingi ish joyingizdagi maosh",
    EXPECTED_SALARY: "Kutilayotgan maosh",
    WORK_DURATION: "Qancha muddat ishlamoqchisiz",
    TEAMWORK_HEALTH: "Kollektiv va sog'lingiz",
    ADDITIONAL: "O'zingiz haqingizda qo'shimcha ma'lumot",
}


# ==================== AI VALIDATSIYA ====================

async def validate_answer(question: str, answer: str) -> dict:
    prompt = f"""Siz "Ziynat" bijuteriya va soatlar do'koni uchun ishga qabul anketasini tekshiruvchi yordamchisiz.
Savol: "{question}"
Foydalanuvchi javobi: "{answer}"

Ushbu javob savolga mantiqan mos keladimi?
Faqat JSON formatida javob bering:
{{"valid": true yoki false, "reason": "qisqa sabab, o'zbek tilida"}}"""

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
Ushbu rasmda haqiqiy INSON YUZI (portret, selfie yoki inson qiyofasi) ko'rinib turibdimi?

QOIDALAR:
1. Buyumlar, hujjat, mashina, hayvonlar -> "is_person": false
2. Inson yuzi yoki qiyofasi ko'ringan bo'lsa -> "is_person": true

FAQAT ushbu JSON formatida javob bering:
{"is_person": true, "reason": "Rasmda inson yuzi ko'rinib turibdi."}"""

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

        if "true" in text or "ha" in text or "inson" in text or "odam" in text or "yuzi" in text:
            return {"is_person": True, "reason": "Rasmda inson ko'rinib turibdi."}
        elif "false" in text or "yo'q" in text:
            return {"is_person": False, "reason": "Rasmda inson yuzi ko'rinmayapti."}

        return {"is_person": True, "reason": ""}

    except Exception as e:
        logging.error(f"Rasm validatsiyasida xatolik: {e}")
        return {"is_person": True, "reason": ""}


async def analyze_candidate_with_ai(user_data: dict) -> str:
    prompt = f"""
Siz "ZIYNAT" bijuteriya va soatlar do'konining HR menejeri va tajribali analitigisiz.
Do'konda xodimlar mijozlarga soat va zargarlik buyumlarini sotishi, tovarlarni chiroyli joylashtirishi, mobil telefon orqali video olib, CapCut kabi ilovalarda montaj qilishi hamda Instagram/Telegram'da mijozlar bilan muloqot qilishi kerak.

NOMZOD MA'LUMOTLARI:
- F.I.Sh va Lavozim: {user_data.get('fullname')} / {user_data.get('position')}
- Yoshi va Manzili: {user_data.get('birthdate')} / {user_data.get('address')} ({user_data.get('housing')})
- Tel: {user_data.get('phone')}
- Ma'lumoti va Ish tajribasi: {user_data.get('education_level')} / {user_data.get('work_exp')}
- 📹 Video olish va Telefon modeli: {user_data.get('video_skills')}
- 🎬 Montaj ko'nikmalari (CapCut va b.): {user_data.get('editing_apps')}
- 📱 SMM va Mijozlar bilan muloqot: {user_data.get('smm_exp')}
- 🛍 Do'kon vazifalariga tayyorligi: {user_data.get('store_duties')}
- Oilaviy ahvoli va A'zolari: {user_data.get('marital_status')} / {user_data.get('family_members')}
- Tillar: {user_data.get('languages')}
- Oldingi va Kutilayotgan maosh: {user_data.get('prev_salary')} / {user_data.get('expected_salary')}
- Ishlash muddati / Qolib ishlash: {user_data.get('work_duration')} / {user_data.get('overtime')}
- Qo'shimcha sifatlari: {user_data.get('additional')}

QUYIDAGI MEZONLAR BO'YICHA "ZIYNAT" DO'KONI DIREKTORI UCHUN HR TAHLIL VA TAVSIYA BERING (O'zbek tilida):
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


async def process_text_step(update: Update, context: ContextTypes.DEFAULT_TYPE, current_state: int, field_name: str, next_question: str, next_state: int, keyboard=None):
    answer = update.message.text
    question_text = QUESTIONS.get(current_state, "")

    if question_text:
        result = await validate_answer(question_text, answer)
        if not result.get("valid", True):
            await update.message.reply_text(f"⚠️ {result.get('reason', 'Javob savolga mos emas.')}\n\nIltimos, qaytadan kiriting:\n{next_question}")
            return current_state

    context.user_data[field_name] = answer
    reply_markup = keyboard if keyboard else ReplyKeyboardRemove()
    await update.message.reply_text(next_question, reply_markup=reply_markup)
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
    await update.message.reply_text(
        "Assalomu alaykum! 'Ziynat' bijuteriya va soatlar do'koni ishga qabul anketasiga xush kelibsiz. ✨\n\n"
        "📸 Iltimos, anketaga biriktirish uchun o'zingizning rasmingizni yuboring:\n"
        "*(Yuzingiz aniq ko'ringan tushunarli rasm yuboring)*",
        parse_mode="Markdown"
    )
    return PHOTO


async def get_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        await update.message.reply_text("⚠️ Rasm yuborish majburiy! Iltimos, faqat o'zingizning rasmingizni yuboring.")
        return PHOTO

    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()

    checking_msg = await update.message.reply_text("⏳ Rasmingiz tekshirilmoqda...")
    result = await validate_photo(photo_bytes)

    try:
        await checking_msg.delete()
    except Exception:
        pass

    if not result.get("is_person", True):
        await update.message.reply_text(
            f"❌ {result.get('reason', 'Bu rasmda inson yuzi ko\'rinmayapti.')}\n\n"
            "Iltimos, yuzingiz aniq ko'ringan haqiqiy suratingizni yuboring:"
        )
        return PHOTO

    context.user_data['photo'] = update.message.photo[-1].file_id

    await update.message.reply_text(
        "Qaysi bo'lim va lavozimga topshiryapsiz?\n\n*(Misol: Do'kon sotuvchisi va kontent-menejer)*", 
        parse_mode="Markdown"
    )
    return POSITION


async def get_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, POSITION, 'position', "Familiya, ism va sharifingizni kiriting:\n\n*(Misol: Abdullayeva Dilnoza Karim qizi)*", FULL_NAME)

async def get_fullname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, FULL_NAME, 'fullname', "Tug'ilgan sanangizni kiriting:\n\n*(Misol: 15.05.2001)*", BIRTH_DATE)

async def get_birthdate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, BIRTH_DATE, 'birthdate', "Millatingizni kiriting:\n\n*(Misol: O'zbek)*", NATIONALITY)

async def get_nationality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, NATIONALITY, 'nationality', "Doimiy yashash joyingiz (propiska manzilingiz):\n\n*(Misol: Toshkent sh., Chilonzor tumani, 5-mavze)*", ADDRESS)

async def get_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["Hovli", "Dom"]], resize_keyboard=True, one_time_keyboard=True)
    context.user_data['address'] = update.message.text
    await update.message.reply_text("Yashash sharoitingizni tanlang:", reply_markup=keyboard)
    return HOUSING

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
    await update.message.reply_text("Qachon va qaysi o'quv yurtini tamomlagansiz?\n\n*(Misol: 2022-yil, Toshkent Moliya Instituti)*", reply_markup=ReplyKeyboardRemove())
    return EDU_DETAILS

async def get_edudetails(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, EDU_DETAILS, 'edu_details', "Avval qaysi korxona yoki do'konlarda va qanday lavozimda ishlagansiz?\n\n*(Misol: 2023-yil, 'X' kiyim do'konida sotuvchi bo'lib 1 yil ishlaganman)*", WORK_EXP)

async def get_workexp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['work_exp'] = update.message.text
    await update.message.reply_text(
        "📱 Telefoningizda sifatli video ololaysizmi va qaysi rusumdagi telefondan foydalanasiz?\n\n"
        "*(Misol: Ha, video olaman. Telefonim iPhone 13 / Samsung S21)*"
    )
    return VIDEO_SKILLS

async def get_video_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, VIDEO_SKILLS, 'video_skills',
        "🎬 Videolarni qaysi ilovalarda montaj qilasiz va qaysi birida yaxshi ishlay olasiz?\n\n"
        "*(Misol: CapCut, InShot, VN. CapCut dasturida juda yaxshi montaj qilaman)*",
        EDITING_APPS
    )

async def get_editing_apps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, EDITING_APPS, 'editing_apps',
        "💬 Instagram va Telegram'ga video joylash hamda mijozlar xabarlariga (DM/Comment) javob berish tajribangiz bormi?\n\n"
        "*(Misol: Ha, avvalgi do'konda sahifani yuritganman / Yo'q, lekin tez o'rganib olaman)*",
        SMM_EXP
    )

async def get_smm_exp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, SMM_EXP, 'smm_exp',
        "🛍 Do'konda tovarlarni (soat/bijuteriya) chiroyli joylashtirish, mijozlar bilan muloqot qilish va video olish vazifalarini bajara olasizmi?\n\n"
        "*(Misol: Ha, barcha vazifalarni mas'uliyat bilan bajara olaman)*",
        STORE_DUTIES
    )

async def get_store_duties(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['store_duties'] = update.message.text
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Chet el safariga chiqqanmisiz?", reply_markup=keyboard)
    return TRIP_ABROAD

async def get_trip_abroad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    context.user_data['trip_abroad'] = text
    if text.strip().lower() == "ha":
        await update.message.reply_text("Chet elga qachon, qayerga va nima sababdan chiqqansiz?\n\n*(Misol: 2022-yil Turkiyaga vaqtinchalik sayohatga)*", reply_markup=ReplyKeyboardRemove())
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
    await update.message.reply_text("Oila a'zolaringiz haqida ma'lumot bering:\n\n*(Misol: Otam - tadbirkor, Onam - uy bekasi)*", reply_markup=ReplyKeyboardRemove())
    return FAMILY_MEMBERS

async def get_family_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, FAMILY_MEMBERS, 'family_members',
        "Harbiy xizmat va sudlanganlik holatingiz haqida yozing:\n\n*(Misol: Harbiyda bo'lmaganman / Sudlanmaganman)*",
        MILITARY_CRIMINAL
    )

async def get_military_criminal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, MILITARY_CRIMINAL, 'military_criminal',
        "Qaysi tillarni bilasiz va qaysi darajada?\n\n*(Misol: O'zbek tili - a'lo, Rus tili - so'zlashuv darajasida)*",
        LANGUAGES
    )

async def get_languages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, LANGUAGES, 'languages',
        "Bizning 'Ziynat' do'konimiz haqida qayerdan eshitdingiz?\n\n*(Misol: Telegram kanaldan / Tanishim tavsiya qildi)*",
        HOW_HEARD
    )

async def get_how_heard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, HOW_HEARD, 'how_heard',
        "Sizga kim kafillik yoki tavsiya bera oladi?\n\n*(Misol: Oxirgi ish joyimdagi rahbarim: Aliyev Vali, +998901234567)*",
        GUARANTOR
    )

async def get_guarantor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['guarantor'] = update.message.text
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Oxirgi ish joyingizdan siz haqida surishtirishimizga rozimisiz?", reply_markup=keyboard)
    return BACKGROUND_CHECK

async def get_background_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['background_check'] = update.message.text
    await update.message.reply_text("Oldingi ish joyingizdagi maoshingiz qancha edi?\n\n*(Misol: 3.500.000 so'm)*", reply_markup=ReplyKeyboardRemove())
    return PREV_SALARY

async def get_prev_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, PREV_SALARY, 'prev_salary', "Bizda qancha miqdordagi maoshga ishlamoqchisiz?\n\n*(Misol: 4.500.000 - 5.000.000 so'm)*", EXPECTED_SALARY)

async def get_expected_salary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(update, context, EXPECTED_SALARY, 'expected_salary', "Bizning do'konda qancha muddat ishlamoqchisiz?\n\n*(Misol: 1 yildan ortiq / Doimiy)*", WORK_DURATION)

async def get_work_duration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['work_duration'] = update.message.text
    keyboard = ReplyKeyboardMarkup([["Ha", "Yo'q"]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("Ishdan keyin qolib ishlash (overtime) va majlislarga rozimisiz?", reply_markup=keyboard)
    return OVERTIME_MEETINGS

async def get_overtime_meetings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['overtime'] = update.message.text
    await update.message.reply_text("Sog'ligingizda muammolar yo'qmi?\n\n*(Misol: Yo'q, sog'lig'im joyida)*", reply_markup=ReplyKeyboardRemove())
    return TEAMWORK_HEALTH

async def get_teamwork_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_text_step(
        update, context, TEAMWORK_HEALTH, 'health',
        "O'zingiz haqingizda qo'shimcha ma'lumot (kuchli va ijobiy taraflaringiz):\n\n*(Misol: Kirishimli, mas'uliyatliman va muloqot qilishni yaxshi ko'raman)*",
        ADDITIONAL
    )

async def get_additional(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text
    question_text = QUESTIONS.get(ADDITIONAL, "")
    result = await validate_answer(question_text, answer)

    if not result.get("valid", True):
        await update.message.reply_text(f"⚠️ {result.get('reason', 'Javob mos emas.')}\n\nIltimos, to'g'ri javob yozing:")
        return ADDITIONAL

    context.user_data['additional'] = answer
    user_id = update.message.from_user.id

    await update.message.reply_text(
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
        f"🎖 *Harbiy xizmat/Sudlanganlik:* {context.user_data.get('military_criminal')}\n"
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
    await update.message.reply_text("Anketa bekor qilindi.", reply_markup=ReplyKeyboardRemove())
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
            MILITARY_CRIMINAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_military_criminal)],
            LANGUAGES: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_languages)],
            HOW_HEARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_how_heard)],
            GUARANTOR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_guarantor)],
            BACKGROUND_CHECK: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_background_check)],
            PREV_SALARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_prev_salary)],
            EXPECTED_SALARY: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_expected_salary)],
            WORK_DURATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_work_duration)],
            OVERTIME_MEETINGS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_overtime_meetings)],
            TEAMWORK_HEALTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_teamwork_health)],
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
