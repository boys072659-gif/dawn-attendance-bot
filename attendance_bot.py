import asyncio
import csv
import io
import logging
import os
import sqlite3
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import holidays
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# 설정
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8301434439:AAHz9Bj7E-nlchRVI8I6TvVNi7qegRqzaYs")

DB_PATH = "attendance.db"
TZ = ZoneInfo("Asia/Seoul")

# 100회 카운트 시작일(원하는 시작일로 수정)
START_DATE = date(2026, 3, 2)  # 예: 2026-03-02(월)

# 리마인드 시작/종료 시간(미기록 시 1시간마다)
REMIND_START = time(7, 0)
REMIND_END = time(22, 0)

# 새벽기도 요일: 월(0), 화(1), 금(4)
PRAYER_WEEKDAYS = {0, 1, 4}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# 한글 빠른메뉴(명령어 대신 버튼/텍스트로도 동작)
MAIN_MENU = ReplyKeyboardMarkup(
    [["참석", "불참"], ["수정", "내정보"], ["등록", "도움말"]],
    resize_keyboard=True
)

# =========================
# DB
# =========================
def db_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            church TEXT NOT NULL,
            dept   TEXT NOT NULL,
            region TEXT NOT NULL,
            group_name TEXT NOT NULL,
            name   TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            att_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ATTEND', 'ABSENT')),
            reason TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(chat_id, att_date),
            FOREIGN KEY(chat_id) REFERENCES users(chat_id)
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        )
        """)
        c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('start_date', ?)", (START_DATE.isoformat(),))
        conn.commit()

def upsert_user(chat_id: int, church: str, dept: str, region: str, group_name: str, name: str):
    now = datetime.now(TZ).isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
        INSERT INTO users(chat_id, church, dept, region, group_name, name, created_at, updated_at)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
          church=excluded.church,
          dept=excluded.dept,
          region=excluded.region,
          group_name=excluded.group_name,
          name=excluded.name,
          updated_at=excluded.updated_at
        """, (chat_id, church, dept, region, group_name, name, now, now))
        conn.commit()

def get_user(chat_id: int):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,))
        return c.fetchone()

def set_attendance(chat_id: int, att_date: date, status: str, reason: str | None):
    now = datetime.now(TZ).isoformat()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
        INSERT INTO attendance(chat_id, att_date, status, reason, created_at, updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(chat_id, att_date) DO UPDATE SET
          status=excluded.status,
          reason=excluded.reason,
          updated_at=excluded.updated_at
        """, (chat_id, att_date.isoformat(), status, reason, now, now))
        conn.commit()

def get_attendance(chat_id: int, att_date: date):
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM attendance WHERE chat_id=? AND att_date=?", (chat_id, att_date.isoformat()))
        return c.fetchone()

def list_recent_prayer_dates(limit: int = 10) -> list[date]:
    today = datetime.now(TZ).date()
    out = []
    for i in range(limit * 3):  # 넉넉히
        d = today - timedelta(days=i)
        if is_prayer_day(d):
            out.append(d)
        if len(out) >= limit:
            break
    return out

# =========================
# 공휴일 / 새벽기도일 판정
# =========================
def is_korean_holiday(d: date) -> bool:
    kr = holidays.KR(years=[d.year])
    return d in kr

def is_prayer_day(d: date) -> bool:
    if d.weekday() not in PRAYER_WEEKDAYS:
        return False
    if is_korean_holiday(d):
        return False
    return True

def get_start_date_from_db() -> date:
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("SELECT v FROM meta WHERE k='start_date'")
        row = c.fetchone()
        return date.fromisoformat(row["v"]) if row else START_DATE

def count_prayer_sessions_so_far(upto: date | None = None) -> int:
    if upto is None:
        upto = datetime.now(TZ).date()
    start = get_start_date_from_db()
    if upto < start:
        return 0
    cnt = 0
    d = start
    while d <= upto:
        if is_prayer_day(d):
            cnt += 1
        d += timedelta(days=1)
    return cnt

def progress_text() -> str:
    done = count_prayer_sessions_so_far()
    return f"진행: {done}/100회"

def fmt_user(urow) -> str:
    return f"{urow['church']} / {urow['dept']} / {urow['region']} / {urow['group_name']} / {urow['name']}"

def today_kst() -> date:
    return datetime.now(TZ).date()

# =========================
# Conversation States
# =========================
CHURCH, DEPT, REGION, GROUP, NAME = range(5)

WAITING_REASON = 100
EDIT_PICK_DATE = 200
EDIT_PICK_STATUS = 201
EDIT_WAIT_REASON = 202

# =========================
# Decorator
# =========================
def require_registered(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_user.id
        if not get_user(chat_id):
            await update.effective_message.reply_text(
                "먼저 '등록'을 해주세요.\n- /register (또는 버튼 '등록')",
                reply_markup=MAIN_MENU
            )
            return
        return await func(update, context)
    return wrapper

# =========================
# UI Helpers
# =========================
def action_buttons_for_today(d: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ 참석", callback_data=f"BTN_ATTEND:{d.isoformat()}"),
            InlineKeyboardButton("📝 불참", callback_data=f"BTN_ABSENT:{d.isoformat()}"),
        ],
        [InlineKeyboardButton("✏️ 수정", callback_data="BTN_EDIT")]
    ])

# =========================
# Basic
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📌 새벽기도 출석부 봇\n\n"
        "✔ 새벽기도일: 월/화/금 (한국 공휴일 제외)\n"
        "✔ 출석: 참석/불참(사유)\n"
        "✔ 미기록 시 1시간마다 리마인드\n"
        "✔ 100회 진행 카운트\n\n"
        "처음이면 '등록'부터 해주세요.\n"
        f"⏱ {progress_text()}"
    )
    await update.message.reply_text(msg, reply_markup=MAIN_MENU)

async def help_kor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🧭 사용 방법\n\n"
        "1) 등록: /register (또는 '등록')\n"
        "2) 참석: /attend (또는 '참석')\n"
        "3) 불참: /absent (또는 '불참') → 사유 입력\n"
        "4) 수정: /edit (또는 '수정')\n"
        "5) 내정보: /my (또는 '내정보')\n"
        "6) 통계: /stats church | /stats dept | /stats person\n"
        "7) CSV: /export\n"
    )
    await update.effective_message.reply_text(msg, reply_markup=MAIN_MENU)

# =========================
# Register flow (등록 1회 저장)
# =========================
async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("교회명을 입력해주세요. (예: 서울야고보)", reply_markup=MAIN_MENU)
    return CHURCH

async def reg_church(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["church"] = update.message.text.strip()
    await update.message.reply_text("부서를 입력해주세요. (예: 문화부/찬양대/전도과 등)")
    return DEPT

async def reg_dept(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["dept"] = update.message.text.strip()
    await update.message.reply_text("지역을 입력해주세요. (예: 대학지역/청년회/부녀회 등)")
    return REGION

async def reg_region(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["region"] = update.message.text.strip()
    await update.message.reply_text("구역(또는 소그룹)을 입력해주세요. (예: 3구역)")
    return GROUP

async def reg_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["group_name"] = update.message.text.strip()
    await update.message.reply_text("이름을 입력해주세요.")
    return NAME

async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    context.user_data["name"] = update.message.text.strip()

    upsert_user(
        chat_id=chat_id,
        church=context.user_data["church"],
        dept=context.user_data["dept"],
        region=context.user_data["region"],
        group_name=context.user_data["group_name"],
        name=context.user_data["name"],
    )
    u = get_user(chat_id)
    await update.message.reply_text(
        "✅ 등록(또는 수정) 완료!\n"
        f"내 정보: {fmt_user(u)}\n\n"
        "이제 '참석' 또는 '불참'으로 기록하세요.",
        reply_markup=MAIN_MENU
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("취소했습니다.", reply_markup=MAIN_MENU)
    return ConversationHandler.END

@require_registered
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = get_user(update.effective_user.id)
    await update.effective_message.reply_text(
        "현재 등록 정보:\n"
        f"- {fmt_user(u)}\n\n"
        "수정하려면 /register 또는 '등록'을 다시 실행하면 됩니다.",
        reply_markup=MAIN_MENU
    )

# =========================
# Attend / Absent
# =========================
@require_registered
async def attend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = today_kst()
    if not is_prayer_day(d):
        await update.effective_message.reply_text(
            "오늘은 새벽기도 출석 대상일이 아닙니다.\n※ 월/화/금, 한국 공휴일 제외",
            reply_markup=MAIN_MENU
        )
        return

    set_attendance(update.effective_user.id, d, "ATTEND", None)
    await update.effective_message.reply_text(
        f"✅ 참석 기록 완료 ({d.isoformat()})\n{progress_text()}",
        reply_markup=MAIN_MENU
    )

@require_registered
async def absent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = today_kst()
    if not is_prayer_day(d):
        await update.effective_message.reply_text(
            "오늘은 새벽기도 출석 대상일이 아닙니다.\n※ 월/화/금, 한국 공휴일 제외",
            reply_markup=MAIN_MENU
        )
        return ConversationHandler.END

    context.user_data["absent_date"] = d
    await update.effective_message.reply_text("불참 사유를 적어주세요. (예: 늦잠, 병가, 출장 등)", reply_markup=MAIN_MENU)
    return WAITING_REASON

async def absent_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    reason = update.message.text.strip()
    d: date = context.user_data["absent_date"]
    set_attendance(chat_id, d, "ABSENT", reason)
    await update.message.reply_text(
        f"📝 불참 기록 완료 ({d.isoformat()})\n사유: {reason}\n{progress_text()}",
        reply_markup=MAIN_MENU
    )
    return ConversationHandler.END

# =========================
# Buttons: attend/absent/edit
# =========================
@require_registered
async def btn_attend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, iso = q.data.split(":")
    d = date.fromisoformat(iso)

    if not is_prayer_day(d):
        await q.edit_message_text("해당 날짜는 새벽기도 출석 대상일이 아닙니다.")
        return

    set_attendance(q.from_user.id, d, "ATTEND", None)
    await q.edit_message_text(f"✅ 참석 기록 완료 ({d.isoformat()})\n{progress_text()}")

@require_registered
async def btn_absent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, iso = q.data.split(":")
    d = date.fromisoformat(iso)

    if not is_prayer_day(d):
        await q.edit_message_text("해당 날짜는 새벽기도 출석 대상일이 아닙니다.")
        return ConversationHandler.END

    context.user_data["absent_date"] = d
    await q.edit_message_text("불참 사유를 적어주세요. (예: 늦잠, 병가, 출장 등)")
    return WAITING_REASON

# =========================
# My summary
# =========================
@require_registered
async def my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    u = get_user(chat_id)
    start = get_start_date_from_db()

    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
        SELECT status, COUNT(*) as cnt
        FROM attendance
        WHERE chat_id=? AND att_date>=?
        GROUP BY status
        """, (chat_id, start.isoformat()))
        rows = c.fetchall()

    attend_cnt = 0
    absent_cnt = 0
    for r in rows:
        if r["status"] == "ATTEND":
            attend_cnt = r["cnt"]
        elif r["status"] == "ABSENT":
            absent_cnt = r["cnt"]

    done = count_prayer_sessions_so_far(today_kst())
    msg = (
        f"🙋 내 정보: {fmt_user(u)}\n"
        f"📅 시작일: {start.isoformat()}\n"
        f"⏱ 진행: {done}/100회\n\n"
        f"✅ 참석: {attend_cnt}\n"
        f"📝 불참: {absent_cnt}\n"
    )
    await update.effective_message.reply_text(msg, reply_markup=MAIN_MENU)

# =========================
# Edit flow (수정)
# =========================
@require_registered
async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dates = list_recent_prayer_dates(limit=10)
    buttons = [[InlineKeyboardButton(d.isoformat(), callback_data=f"EDITDATE:{d.isoformat()}")] for d in dates]
    buttons.append([InlineKeyboardButton("취소", callback_data="EDITCANCEL")])

    await update.effective_message.reply_text(
        "수정할 날짜를 선택하세요 (최근 새벽기도일 10개):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return EDIT_PICK_DATE

async def edit_pick_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "EDITCANCEL":
        await q.edit_message_text("취소했습니다.")
        return ConversationHandler.END

    _, iso = q.data.split(":")
    d = date.fromisoformat(iso)
    context.user_data["edit_date"] = d

    btns = [
        [InlineKeyboardButton("✅ 참석으로 수정", callback_data="EDITSTATUS:ATTEND")],
        [InlineKeyboardButton("📝 불참으로 수정", callback_data="EDITSTATUS:ABSENT")],
        [InlineKeyboardButton("취소", callback_data="EDITCANCEL")]
    ]
    await q.edit_message_text(
        f"{d.isoformat()} 기록을 무엇으로 수정할까요?",
        reply_markup=InlineKeyboardMarkup(btns),
    )
    return EDIT_PICK_STATUS

async def edit_pick_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "EDITCANCEL":
        await q.edit_message_text("취소했습니다.")
        return ConversationHandler.END

    _, st = q.data.split(":")
    d: date = context.user_data["edit_date"]
    chat_id = q.from_user.id

    if st == "ATTEND":
        set_attendance(chat_id, d, "ATTEND", None)
        await q.edit_message_text(f"✅ 수정 완료: {d.isoformat()} → 참석")
        return ConversationHandler.END

    await q.edit_message_text("불참 사유를 입력해주세요. (예: 늦잠, 병가 등)")
    return EDIT_WAIT_REASON

async def edit_wait_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    d: date = context.user_data["edit_date"]
    reason = update.message.text.strip()
    set_attendance(chat_id, d, "ABSENT", reason)
    await update.message.reply_text(f"📝 수정 완료: {d.isoformat()} → 불참\n사유: {reason}", reply_markup=MAIN_MENU)
    return ConversationHandler.END

# =========================
# Stats / Export
# =========================
@require_registered
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    mode = args[0].lower() if args else "church"
    start = get_start_date_from_db()

    with db_conn() as conn:
        c = conn.cursor()
        if mode == "church":
            c.execute("""
            SELECT u.church as key,
                   SUM(CASE WHEN a.status='ATTEND' THEN 1 ELSE 0 END) as attend,
                   SUM(CASE WHEN a.status='ABSENT' THEN 1 ELSE 0 END) as absent
            FROM attendance a
            JOIN users u ON u.chat_id=a.chat_id
            WHERE a.att_date>=?
            GROUP BY u.church
            ORDER BY attend DESC
            """, (start.isoformat(),))
            title = "📊 교회별 집계"
        elif mode == "dept":
            c.execute("""
            SELECT u.dept as key,
                   SUM(CASE WHEN a.status='ATTEND' THEN 1 ELSE 0 END) as attend,
                   SUM(CASE WHEN a.status='ABSENT' THEN 1 ELSE 0 END) as absent
            FROM attendance a
            JOIN users u ON u.chat_id=a.chat_id
            WHERE a.att_date>=?
            GROUP BY u.dept
            ORDER BY attend DESC
            """, (start.isoformat(),))
            title = "📊 부서별 집계"
        else:
            c.execute("""
            SELECT (u.church || '/' || u.dept || '/' || u.region || '/' || u.group_name || '/' || u.name) as key,
                   SUM(CASE WHEN a.status='ATTEND' THEN 1 ELSE 0 END) as attend,
                   SUM(CASE WHEN a.status='ABSENT' THEN 1 ELSE 0 END) as absent
            FROM attendance a
            JOIN users u ON u.chat_id=a.chat_id
            WHERE a.att_date>=?
            GROUP BY u.chat_id
            ORDER BY attend DESC
            LIMIT 30
            """, (start.isoformat(),))
            title = "📊 개인별 TOP30 집계"

        rows = c.fetchall()

    if not rows:
        await update.effective_message.reply_text("집계할 데이터가 아직 없습니다.", reply_markup=MAIN_MENU)
        return

    lines = [title, f"(기준 시작일: {start.isoformat()})", ""]
    for r in rows:
        lines.append(f"- {r['key']}: ✅{r['attend']} / 📝{r['absent']}")
    await update.effective_message.reply_text("\n".join(lines), reply_markup=MAIN_MENU)

@require_registered
async def export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start = get_start_date_from_db()
    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
        SELECT a.att_date, a.status, COALESCE(a.reason,'') as reason,
               u.church, u.dept, u.region, u.group_name, u.name, u.chat_id
        FROM attendance a
        JOIN users u ON u.chat_id=a.chat_id
        WHERE a.att_date>=?
        ORDER BY a.att_date ASC
        """, (start.isoformat(),))
        rows = c.fetchall()

    if not rows:
        await update.effective_message.reply_text("내보낼 데이터가 아직 없습니다.", reply_markup=MAIN_MENU)
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", "status", "reason", "church", "dept", "region", "group", "name", "chat_id"])
    for r in rows:
        writer.writerow([r["att_date"], r["status"], r["reason"], r["church"], r["dept"], r["region"], r["group_name"], r["name"], r["chat_id"]])

    bio = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    bio.name = f"attendance_export_{today_kst().isoformat()}.csv"
    await update.message.reply_document(document=bio, caption="📎 출석 데이터 CSV", reply_markup=MAIN_MENU)

# =========================
# Reminder job (모든 개인 대상)
# =========================
async def remind_missing_reports(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ)
    d = now.date()

    if not is_prayer_day(d):
        return
    if not (REMIND_START <= now.time() <= REMIND_END):
        return

    with db_conn() as conn:
        c = conn.cursor()
        c.execute("""
        SELECT u.chat_id
        FROM users u
        LEFT JOIN attendance a
          ON a.chat_id=u.chat_id AND a.att_date=?
        WHERE a.id IS NULL
        """, (d.isoformat(),))
        targets = c.fetchall()

    if not targets:
        return

    kb = action_buttons_for_today(d)

    for t in targets:
        try:
            await context.bot.send_message(
                chat_id=t["chat_id"],
                text=(
                    f"⏰ 오늘({d.isoformat()}) 새벽기도 출석 기록이 아직 없습니다.\n"
                    "버튼으로 바로 기록하세요."
                ),
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"Failed to remind {t['chat_id']}: {e}")

# =========================
# Text shortcuts (한글 입력으로도 동작)
# =========================
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()

    if t in ("도움말", "help", "메뉴"):
        return await help_kor(update, context)
    if t == "등록":
        return await register(update, context)
    if t == "참석":
        return await attend(update, context)
    if t == "불참":
        return await absent(update, context)
    if t == "수정":
        return await edit(update, context)
    if t in ("내정보", "내 정보", "내출석", "내 출석"):
        return await my(update, context)

    await update.message.reply_text("메뉴에서 선택해주세요.", reply_markup=MAIN_MENU)

# =========================
# Main
# =========================
def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # register conversation (명령어 + '등록' 텍스트)
    reg_conv = ConversationHandler(
        entry_points=[
            CommandHandler("register", register),
            MessageHandler(filters.Regex(r"^등록$"), register),
        ],
        states={
            CHURCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_church)],
            DEPT:   [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_dept)],
            REGION: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_region)],
            GROUP:  [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_group)],
            NAME:   [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # absent conversation (명령어 + 버튼)
    absent_conv = ConversationHandler(
        entry_points=[
            CommandHandler("absent", absent),
            MessageHandler(filters.Regex(r"^불참$"), absent),
            CallbackQueryHandler(btn_absent, pattern=r"^BTN_ABSENT:"),
        ],
        states={
            WAITING_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, absent_reason)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    # edit conversation
    edit_conv = ConversationHandler(
        entry_points=[
            CommandHandler("edit", edit),
            MessageHandler(filters.Regex(r"^수정$"), edit),
            CallbackQueryHandler(lambda u, c: edit(u, c), pattern=r"^BTN_EDIT$"),
        ],
        states={
            EDIT_PICK_DATE: [CallbackQueryHandler(edit_pick_date, pattern=r"^(EDITDATE:|EDITCANCEL$).*$")],
            EDIT_PICK_STATUS: [CallbackQueryHandler(edit_pick_status, pattern=r"^(EDITSTATUS:|EDITCANCEL$).*$")],
            EDIT_WAIT_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_wait_reason)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_kor))
    app.add_handler(MessageHandler(filters.Regex(r"^(도움말)$"), help_kor))

    app.add_handler(reg_conv)
    app.add_handler(absent_conv)
    app.add_handler(edit_conv)

    # English commands (Telegram 규칙상 /한글 불가)
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("attend", attend))
    app.add_handler(CommandHandler("my", my))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("export", export))

    # 버튼 참석
    app.add_handler(CallbackQueryHandler(btn_attend, pattern=r"^BTN_ATTEND:"))

    # 한글 텍스트 라우팅(메뉴 버튼/직접 입력)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # 1시간마다 리마인드
    app.job_queue.run_repeating(remind_missing_reports, interval=3600, first=10)

    logger.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
