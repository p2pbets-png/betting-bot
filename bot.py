import os
import asyncio
import psycopg2
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
OWNER_ID = (
    int(os.environ["OWNER_ID"])
    if os.environ.get("OWNER_ID", "").lstrip("-").isdigit()
    else None
)


def _parse_group_id(val: str):
    if not val:
        return None
    val = val.strip()
    if not val.lstrip("-").isdigit():
        return None
    n = int(val)
    if n > 0:
        n = int(f"-100{n}")
    return n


ADMIN_GROUP_ID = _parse_group_id(os.environ.get("ADMIN_GROUP_ID", ""))
ADMIN_TOPIC_ID = (
    int(os.environ["ADMIN_TOPIC_ID"])
    if os.environ.get("ADMIN_TOPIC_ID", "").lstrip("-").isdigit()
    else None
)
ANNOUNCE_GROUP_ID = _parse_group_id(os.environ.get("ANNOUNCE_GROUP_ID", ""))
ANNOUNCE_TOPIC_ID = (
    int(os.environ["ANNOUNCE_TOPIC_ID"])
    if os.environ.get("ANNOUNCE_TOPIC_ID", "").lstrip("-").isdigit()
    else None
)
GIVEAWAY_TOPIC_ID = (
    int(os.environ["GIVEAWAY_TOPIC_ID"])
    if os.environ.get("GIVEAWAY_TOPIC_ID", "").lstrip("-").isdigit()
    else None
)

AMOUNT, ODDS, DESCRIPTION = range(3)

# Column indices for bets rows
# 0:id 1:user_id 2:username 3:amount 4:description 5:status
# 6:taker_id 7:taker_username 8:created_at 9:poster_vote
# 10:taker_vote 11:cancel_initiator 12:chat_id 13:odds


def get_db_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS bets (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    username TEXT,
                    amount INTEGER,
                    description TEXT,
                    status TEXT DEFAULT 'open',
                    taker_id BIGINT,
                    taker_username TEXT,
                    created_at TEXT,
                    poster_vote TEXT,
                    taker_vote TEXT,
                    cancel_initiator TEXT,
                    chat_id BIGINT,
                    odds TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    total_won INTEGER DEFAULT 0,
                    total_lost INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS debts (
                    id SERIAL PRIMARY KEY,
                    username TEXT,
                    amount INTEGER,
                    reason TEXT,
                    created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS reviews (
                    id SERIAL PRIMARY KEY,
                    bet_id INTEGER,
                    reviewer_id BIGINT,
                    reviewer_username TEXT,
                    reviewed_username TEXT,
                    rating TEXT,
                    created_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS giveaways (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    message_id BIGINT,
                    prize TEXT NOT NULL,
                    num_winners INTEGER DEFAULT 1,
                    require_member BIGINT,
                    ends_at TIMESTAMP NOT NULL,
                    created_by BIGINT NOT NULL,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT NOW())""")
    c.execute("""CREATE TABLE IF NOT EXISTS ga_entries (
                    id SERIAL PRIMARY KEY,
                    giveaway_id INTEGER REFERENCES giveaways(id),
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    entered_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(giveaway_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS ga_winners (
                    id SERIAL PRIMARY KEY,
                    giveaway_id INTEGER REFERENCES giveaways(id),
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    won_at TIMESTAMP DEFAULT NOW())""")
    c.execute("""CREATE TABLE IF NOT EXISTS chat_activity (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_seen TIMESTAMP DEFAULT NOW(),
                    UNIQUE(chat_id, user_id))""")
    conn.commit()
    conn.close()


init_db()

# ── Helpers ──────────────────────────────────────────────────────────────────


def upsert_user(c, user_id, username):
    c.execute(
        "INSERT INTO users (user_id, username) VALUES (%s, %s) "
        "ON CONFLICT(user_id) DO UPDATE SET username=EXCLUDED.username",
        (user_id, username),
    )


def record_result(conn, c, winner_id, winner_name, loser_id, loser_name, amount):
    upsert_user(c, winner_id, winner_name)
    upsert_user(c, loser_id, loser_name)
    c.execute(
        "UPDATE users SET wins=wins+1, total_won=total_won+%s WHERE user_id=%s",
        (amount, winner_id),
    )
    c.execute(
        "UPDATE users SET losses=losses+1, total_lost=total_lost+%s WHERE user_id=%s",
        (amount, loser_id),
    )
    conn.commit()


def reverse_result(conn, c, winner_id, loser_id, amount):
    """Undo a settled bet's win/loss stats."""
    c.execute(
        "UPDATE users SET wins=GREATEST(0,wins-1), total_won=GREATEST(0,total_won-%s) WHERE user_id=%s",
        (amount, winner_id),
    )
    c.execute(
        "UPDATE users SET losses=GREATEST(0,losses-1), total_lost=GREATEST(0,total_lost-%s) WHERE user_id=%s",
        (amount, loser_id),
    )
    conn.commit()


def do_settle_bet(conn, c, bet, winner_side):
    poster_id, poster_name = bet[1], bet[2]
    taker_id, taker_name = bet[6], bet[7]
    amount, bet_id = bet[3], bet[0]
    if winner_side == "poster":
        record_result(conn, c, poster_id, poster_name, taker_id, taker_name, amount)
        return f"🏁 Bet #{bet_id} settled! ✅ @{poster_name} wins ${amount}!"
    else:
        record_result(conn, c, taker_id, taker_name, poster_id, poster_name, amount)
        return f"🏁 Bet #{bet_id} settled! ✅ @{taker_name} wins ${amount}!"


async def notify_user(
    context, user_id, username, fallback_chat_id, text, keyboard=None
):
    """Try to DM a user. If it fails, post in the group and mention them."""
    kwargs = {"text": text, "parse_mode": "Markdown"}
    if keyboard:
        kwargs["reply_markup"] = keyboard
    try:
        await context.bot.send_message(chat_id=user_id, **kwargs)
    except Exception:
        if fallback_chat_id and fallback_chat_id != user_id:
            mention = f"@{username}" if username else "the other party"
            kwargs["text"] = f"{mention} ↓\n{text}"
            try:
                await context.bot.send_message(chat_id=fallback_chat_id, **kwargs)
            except Exception:
                pass


async def send_review_prompt(context, winner_id, loser_name, bet_id, winner_side):
    """DM the winner a payment review prompt about the loser."""
    text = (
        f"💬 *Rate @{loser_name}'s payment for Bet #{bet_id}*\n"
        f"How quickly did they pay up?"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Paid within 1 hour", callback_data=f"review_{bet_id}_{winner_side}_fast")],
            [InlineKeyboardButton("⏰ Paid within 12 hours", callback_data=f"review_{bet_id}_{winner_side}_slow")],
            [InlineKeyboardButton("❌ Did not pay", callback_data=f"review_{bet_id}_{winner_side}_nopay")],
            [InlineKeyboardButton("🔙 Cancel (misclick)", callback_data=f"reviewcancel_{bet_id}")],
        ]
    )
    try:
        await context.bot.send_message(
            chat_id=winner_id, text=text, parse_mode="Markdown", reply_markup=kb
        )
    except Exception:
        pass


STATUS_LABELS = {
    "settled": "🏁 Settled",
    "disputed": "⚠️ Disputed",
    "pending_confirm": "⏳ Awaiting result confirmation",
    "pending_cancel": "🔄 Awaiting cancel confirmation",
    "cancelled": "🚫 Cancelled",
    "matched": "✅ Matched",
    "open": "🔴 Open",
}

# ── Commands ─────────────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏦 *P2P Betting Board*\n\n"
        "/postbet — Post a wager\n"
        "/openbets — View open bets\n"
        "/mybets — Your bets\n"
        "/settle — Report your bet outcome\n"
        "/cancel — Cancel a bet\n"
        "/leaderboard — Top bettors\n"
        "/rep @username — Look up any player's rep\n"
        "/gcactivebets — All active bets in this group\n\n"
        "⚖️ Both sides must confirm before a result or cancellation is recorded.\n\n"
        "🎉 *Giveaways*\n"
        "/giveaways — See active giveaways\n"
        "/giveawayinfo <id> — Details on a giveaway",
        parse_mode="Markdown",
    )


async def postbet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["bet_chat_id"] = update.effective_chat.id
    await update.message.reply_text("💰 Enter the wager amount:")
    return AMOUNT


async def postbet_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = int(update.message.text)
        context.user_data["amount"] = amount
        await update.message.reply_text(
            "📊 Enter the odds for your bet:\n"
            "Examples: `2:1`, `1.5:1`, `even`, `-110`, `+200`",
            parse_mode="Markdown",
        )
        return ODDS
    except Exception:
        await update.message.reply_text("Please send a whole number (e.g. 50).")
        return AMOUNT


async def postbet_odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    odds = update.message.text.strip()
    if not odds:
        await update.message.reply_text("Please enter the odds (e.g. 2:1, even, -110).")
        return ODDS
    context.user_data["odds"] = odds
    await update.message.reply_text("📝 Describe what you're betting on:")
    return DESCRIPTION


async def postbet_description(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = update.message.text
    amount = context.user_data["amount"]
    odds = context.user_data.get("odds", "even")
    chat_id = context.user_data.get("bet_chat_id", update.effective_chat.id)
    user = update.message.from_user
    username = user.username or user.first_name

    conn = get_db_conn()
    c = conn.cursor()
    upsert_user(c, user.id, username)
    c.execute(
        "INSERT INTO bets (user_id, username, amount, description, created_at, chat_id, odds) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (user.id, username, amount, desc, datetime.now().strftime("%H:%M"), chat_id, odds),
    )
    bet_id = c.fetchone()[0]

    # Fetch poster rep for announcement
    c.execute("SELECT wins, losses FROM users WHERE user_id=%s", (user.id,))
    user_row = c.fetchone()
    wins, losses = (user_row[0], user_row[1]) if user_row else (0, 0)
    total_games = wins + losses
    win_rate = int((wins / total_games) * 100) if total_games else 0

    c.execute(
        "SELECT rating, COUNT(*) FROM reviews WHERE LOWER(reviewed_username)=LOWER(%s) GROUP BY rating",
        (username,),
    )
    review_counts = dict(c.fetchall())
    fast = review_counts.get("fast", 0)
    slow = review_counts.get("slow", 0)
    nopay = review_counts.get("nopay", 0)
    total_reviews = fast + slow + nopay

    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Bet #{bet_id} posted!\n💰 ${amount} at {odds}\n📝 {desc}"
    )

    if ANNOUNCE_GROUP_ID and ANNOUNCE_TOPIC_ID:
        try:
            if total_reviews > 0:
                pay_rep = f"✅{fast} ⏰{slow} ❌{nopay}"
            else:
                pay_rep = "No reviews yet"
            rep_line = f"{wins}W/{losses}L ({win_rate}%) | 💳 {pay_rep}"
            announce_text = (
                f"🔔 *New Open Bet #{bet_id}*\n"
                f"👤 @{username} — {rep_line}\n"
                f"💰 ${amount} @ {odds}\n"
                f"📝 {desc}\n\n"
                f"👉 Use /openbets to take it!"
            )
            await context.bot.send_message(
                chat_id=ANNOUNCE_GROUP_ID,
                message_thread_id=ANNOUNCE_TOPIC_ID,
                text=announce_text,
                parse_mode="Markdown",
            )
            print(f"[ANNOUNCE] Posted Bet #{bet_id}")
        except Exception as e:
            print(f"[ANNOUNCE] Failed to post Bet #{bet_id}: {e}")

    return ConversationHandler.END


async def openbets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT id, user_id, username, amount, description, status, taker_id, taker_username, created_at, poster_vote, taker_vote, cancel_initiator, chat_id, odds FROM bets WHERE status='open' ORDER BY id DESC")
    bets = c.fetchall()

    if not bets:
        await update.message.reply_text("No open bets at the moment.")
        conn.close()
        return

    text = "🔴 *OPEN BETS*\n\n"
    keyboard = []
    for b in bets:
        odds = b[13] if b[13] else "?"
        username = b[2]

        c.execute("SELECT wins, losses FROM users WHERE LOWER(username)=LOWER(%s)", (username,))
        ur = c.fetchone()
        wins, losses = (ur[0], ur[1]) if ur else (0, 0)
        total_g = wins + losses
        win_rate = int((wins / total_g) * 100) if total_g else 0

        c.execute(
            "SELECT rating, COUNT(*) FROM reviews WHERE LOWER(reviewed_username)=LOWER(%s) GROUP BY rating",
            (username,),
        )
        rc = dict(c.fetchall())
        fast, slow, nopay = rc.get("fast", 0), rc.get("slow", 0), rc.get("nopay", 0)
        total_rev = fast + slow + nopay
        pay_rep = f"💳 ✅{fast} ⏰{slow} ❌{nopay}" if total_rev > 0 else "💳 No reviews"

        text += (
            f"*#{b[0]}* | ${b[3]} @ {odds}\n"
            f"👤 @{username} — {wins}W/{losses}L ({win_rate}%) {pay_rep}\n"
            f"📝 {b[4]}\n\n"
        )
        keyboard.append([
            InlineKeyboardButton(f"✅ Take #{b[0]}", callback_data=f"take_{b[0]}"),
            InlineKeyboardButton(f"💱 Counter #{b[0]}", callback_data=f"counteroffer_{b[0]}"),
        ])

    conn.close()
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def mybets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, username, amount, description, status, taker_id, taker_username, created_at, poster_vote, taker_vote, cancel_initiator, chat_id, odds FROM bets WHERE user_id=%s OR taker_id=%s",
        (user_id, user_id),
    )
    bets = c.fetchall()
    conn.close()

    if not bets:
        await update.message.reply_text("You have no bets.")
        return

    text = "📋 *Your Bets*\n\n"
    for b in bets:
        label = STATUS_LABELS.get(b[5], b[5])
        odds = b[13] if b[13] else "?"
        desc = b[4][:40] + ("…" if len(b[4]) > 40 else "")
        text += f"*#{b[0]}* | ${b[3]} @ {odds} | {label}\n📝 {desc}\n\n"
    await update.message.reply_text(text, parse_mode="Markdown")


async def settle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, username, amount, description, status, taker_id, taker_username, created_at, poster_vote, taker_vote, cancel_initiator, chat_id, odds FROM bets WHERE (user_id=%s OR taker_id=%s) AND status IN ('matched','pending_confirm','disputed')",
        (user.id, user.id),
    )
    bets = c.fetchall()
    conn.close()

    if not bets:
        await update.message.reply_text("You have no active matched bets to settle.")
        return

    text = "⚖️ *Report Outcome*\nWho won?\n\n"
    keyboard = []
    for b in bets:
        bet_id, amount = b[0], b[3]
        poster_name = b[2]
        taker_name = b[7] or "opponent"

        if user.id == b[1]:
            my_side, their_side = "poster", "taker"
        else:
            my_side, their_side = "taker", "poster"

        text += f"*#{bet_id}* — ${amount}\n{b[4][:40]}{'…' if len(b[4]) > 40 else ''}\n\n"
        keyboard.append([
            InlineKeyboardButton(f"✅ I won #{bet_id}", callback_data=f"vote_{bet_id}_{my_side}_{my_side}"),
            InlineKeyboardButton(f"❌ They won #{bet_id}", callback_data=f"vote_{bet_id}_{my_side}_{their_side}"),
        ])

    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, username, amount, description, status, taker_id, taker_username, created_at, poster_vote, taker_vote, cancel_initiator, chat_id, odds FROM bets WHERE (user_id=%s AND status='open') "
        "OR ((user_id=%s OR taker_id=%s) AND status IN ('matched','pending_confirm','pending_cancel'))",
        (user.id, user.id, user.id),
    )
    bets = c.fetchall()
    conn.close()

    if not bets:
        await update.message.reply_text("You have no cancellable bets.")
        return

    text = "🚫 *Cancel a Bet*\n\n"
    keyboard = []
    for b in bets:
        bet_id, status, amount = b[0], b[5], b[3]
        opponent = b[7] if user.id == b[1] else b[2]
        text += f"*#{bet_id}* — ${amount}\n{b[4][:40]}{'…' if len(b[4]) > 40 else ''}\n\n"
        if status == "open":
            keyboard.append([InlineKeyboardButton(f"🚫 Cancel #{bet_id} (no taker — instant)", callback_data=f"cancelopen_{bet_id}")])
        else:
            keyboard.append([InlineKeyboardButton(f"🔄 Request cancel #{bet_id} (needs @{opponent or 'opponent'} to agree)", callback_data=f"cancelreq_{bet_id}")])

    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown"
    )


async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT username, wins, losses, total_won FROM users "
        "WHERE wins+losses>0 ORDER BY wins DESC, total_won DESC LIMIT 10"
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No settled bets yet. Play some games first!")
        return

    medals = ["🥇", "🥈", "🥉"]
    text = "🏆 *LEADERBOARD*\n\n"
    for i, (username, wins, losses, total_won) in enumerate(rows):
        medal = medals[i] if i < 3 else f"{i + 1}."
        total = wins + losses
        rate = int((wins / total) * 100) if total else 0
        text += f"{medal} @{username} — {wins}W/{losses}L ({rate}%) | ${total_won} won\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    target = (
        context.args[0].lstrip("@")
        if context.args
        else (user.username or user.first_name)
    )

    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT user_id, username, wins, losses, total_won, total_lost FROM users WHERE LOWER(username)=LOWER(%s)", (target,))
    row = c.fetchone()

    if not row:
        await update.message.reply_text(f"No record found for @{target}. They may not have placed any bets yet.")
        conn.close()
        return

    uid, username, wins, losses, total_won, total_lost = row
    total = wins + losses
    rate = int((wins / total) * 100) if total else 0

    c.execute(
        "SELECT id, amount, reason, created_at FROM debts WHERE LOWER(username)=LOWER(%s) ORDER BY id DESC",
        (username,),
    )
    debt_rows = c.fetchall()
    total_debt = sum(d[1] for d in debt_rows)

    c.execute(
        "SELECT rating, COUNT(*) FROM reviews WHERE LOWER(reviewed_username)=LOWER(%s) GROUP BY rating",
        (username,),
    )
    review_counts = dict(c.fetchall())
    conn.close()

    fast = review_counts.get("fast", 0)
    slow = review_counts.get("slow", 0)
    nopay = review_counts.get("nopay", 0)
    total_reviews = fast + slow + nopay

    if total_reviews > 0:
        rep_line = f"✅ {fast}  ⏰ {slow}  ❌ {nopay}  ({total_reviews} reviews)"
    else:
        rep_line = "No payment reviews yet"

    text = (
        f"📊 *@{username}*\n\n"
        f"🏆 {wins}W / {losses}L ({rate}% win rate)\n"
        f"💳 *Payment rep:* {rep_line}\n"
    )

    if total_debt > 0:
        text += f"\n⚠️ *Unpaid debts: ${total_debt}*\n"
        for d in debt_rows:
            reason_str = f" — {d[2]}" if d[2] else ""
            text += f"  #{d[0]} | ${d[1]}{reason_str} ({d[3][:10]})\n"

    await update.message.reply_text(text, parse_mode="Markdown")


async def activebets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, username, amount, odds, description, status, taker_username "
        "FROM bets WHERE status IN ('open','matched','pending_confirm','disputed') "
        "ORDER BY username, id DESC"
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No active bets right now.")
        return

    from collections import defaultdict
    icons = {"open": "🟡", "matched": "🤝", "pending_confirm": "⏳", "disputed": "⚔️"}
    is_owner = update.message.from_user.id == OWNER_ID

    by_user = defaultdict(list)
    for b in rows:
        by_user[b[1]].append(b)

    text = "🎯 *Active Bets — All Users*\n\n"
    keyboard = []
    for username, bets in by_user.items():
        text += f"👤 *@{username}*\n"
        for b in bets:
            bid, _, amt, odds, desc, status, taker_name = b
            icon = icons.get(status, "❓")
            short = desc[:35] + ("…" if len(desc) > 35 else "")
            vs = f" vs @{taker_name}" if taker_name else ""
            text += f"  {icon} #{bid} | ${amt} @ {odds or '?'}{vs} — {short}\n"
            if status == "open":
                keyboard.append([
                    InlineKeyboardButton(f"✅ Take #{bid}", callback_data=f"take_{bid}"),
                    InlineKeyboardButton(f"💱 Counter #{bid}", callback_data=f"counteroffer_{bid}"),
                ])
            elif status in ("matched", "pending_confirm", "disputed") and is_owner:
                keyboard.append([
                    InlineKeyboardButton(f"⚖️ Poster wins #{bid}", callback_data=f"gcsettle_{bid}_poster"),
                    InlineKeyboardButton(f"⚖️ Taker wins #{bid}", callback_data=f"gcsettle_{bid}_taker"),
                ])
        text += "\n"

    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


async def adddebt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ This command is restricted to the bot owner.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "📋 *Usage:* `/adddebt @username amount [reason]`\n"
            "Example: `/adddebt @john 50 Didn't pay Bet #12`",
            parse_mode="Markdown",
        )
        return

    target = context.args[0].lstrip("@")
    try:
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("Amount must be a whole number.")
        return
    reason = " ".join(context.args[2:]) if len(context.args) > 2 else None

    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO debts (username, amount, reason, created_at) VALUES (%s,%s,%s,%s) RETURNING id",
        (target, amount, reason, datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    debt_id = c.fetchone()[0]
    c.execute("SELECT SUM(amount) FROM debts WHERE LOWER(username)=LOWER(%s)", (target,))
    total = c.fetchone()[0] or 0
    conn.commit()
    conn.close()

    reason_str = f"\nReason: {reason}" if reason else ""
    await update.message.reply_text(
        f"⚠️ Debt #{debt_id} recorded for @{target}\n"
        f"Amount: ${amount}{reason_str}\n"
        f"Total outstanding: ${total}",
        parse_mode="Markdown",
    )


async def cleardebt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ This command is restricted to the bot owner.")
        return

    if not context.args:
        await update.message.reply_text(
            "📋 *Usage:*\n"
            "`/cleardebt @username` — clears ALL debts for user\n"
            "`/cleardebt @username 3` — clears specific debt #3",
            parse_mode="Markdown",
        )
        return

    target = context.args[0].lstrip("@")
    debt_id = (
        int(context.args[1])
        if len(context.args) > 1 and context.args[1].isdigit()
        else None
    )

    conn = get_db_conn()
    c = conn.cursor()
    if debt_id:
        c.execute(
            "DELETE FROM debts WHERE id=%s AND LOWER(username)=LOWER(%s)",
            (debt_id, target),
        )
        removed = c.rowcount
        msg = (
            f"✅ Debt #{debt_id} cleared for @{target}."
            if removed
            else f"No matching debt #{debt_id} for @{target}."
        )
    else:
        c.execute("SELECT SUM(amount) FROM debts WHERE LOWER(username)=LOWER(%s)", (target,))
        total = c.fetchone()[0] or 0
        c.execute("DELETE FROM debts WHERE LOWER(username)=LOWER(%s)", (target,))
        removed = c.rowcount
        msg = f"✅ All {removed} debt(s) cleared for @{target} (${total} total)."
    conn.commit()
    conn.close()
    await update.message.reply_text(msg)


async def searchbet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ This command is restricted to the bot owner.")
        return
    if not context.args:
        await update.message.reply_text(
            "📋 *Usage:* `/searchbet <query>`\n"
            "Query can be a bet ID, @username, or keyword from the description.",
            parse_mode="Markdown",
        )
        return

    query = " ".join(context.args).lstrip("@")
    conn = get_db_conn()
    c = conn.cursor()

    if query.isdigit():
        c.execute(
            "SELECT id, user_id, username, amount, description, status, taker_id, taker_username, created_at, poster_vote, taker_vote, cancel_initiator, chat_id, odds FROM bets WHERE id=%s",
            (int(query),),
        )
    else:
        c.execute(
            "SELECT id, user_id, username, amount, description, status, taker_id, taker_username, created_at, poster_vote, taker_vote, cancel_initiator, chat_id, odds FROM bets WHERE LOWER(username)=LOWER(%s) OR LOWER(taker_username)=LOWER(%s) "
            "OR LOWER(description) LIKE LOWER(%s) ORDER BY id DESC LIMIT 8",
            (query, query, f"%{query}%"),
        )
    rows = c.fetchall()

    if not rows:
        await update.message.reply_text(f"No bets found for *{query}*.", parse_mode="Markdown")
        conn.close()
        return

    status_icons = {"open": "🟡", "matched": "🤝", "pending_confirm": "⏳", "disputed": "⚔️", "settled": "🏁", "cancelled": "🚫"}
    text = f"🔍 *Search results for '{query}':*\n\n"
    keyboard = []

    for bet in rows:
        bid, _, poster, amt, desc, status = bet[0], bet[1], bet[2], bet[3], bet[4], bet[5]
        taker = bet[7] or "—"
        odds = bet[13] if bet[13] else "?"
        icon = status_icons.get(status, "❓")
        short = desc[:35] + ("…" if len(desc) > 35 else "")
        text += f"{icon} *#{bid}* | ${amt} @ {odds} | @{poster} vs @{taker}\n  {short}\n\n"

        row_buttons = []
        if status in ("open", "matched", "pending_confirm", "disputed"):
            row_buttons.append(InlineKeyboardButton(f"🚫 Cancel #{bid}", callback_data=f"sbcancel_{bid}"))
        elif status == "settled":
            row_buttons.append(InlineKeyboardButton(f"↩️ Reverse #{bid}", callback_data=f"sbreverse_{bid}"))

        c.execute("SELECT COUNT(*) FROM reviews WHERE bet_id=%s", (bid,))
        if c.fetchone()[0] > 0:
            row_buttons.append(InlineKeyboardButton(f"🗑️ Reviews #{bid}", callback_data=f"sbclearreviews_{bid}"))

        if row_buttons:
            keyboard.append(row_buttons)

    conn.close()
    await update.message.reply_text(
        text,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


async def admin_topic_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message or update.edited_message
    if not msg:
        return
    if msg.chat.id != ADMIN_GROUP_ID or msg.message_thread_id != ADMIN_TOPIC_ID:
        return
    sender_id = msg.from_user.id if msg.from_user else None
    if sender_id == OWNER_ID or sender_id == context.bot.id:
        return
    try:
        member = await context.bot.get_chat_member(chat_id=ADMIN_GROUP_ID, user_id=sender_id)
        if member.status in ("administrator", "creator"):
            return
    except Exception:
        pass
    try:
        await msg.delete()
        print(f"[GUARD] Deleted message from {sender_id} (non-admin) in topic {ADMIN_TOPIC_ID}")
    except Exception as e:
        print(f"[GUARD] Failed to delete: {e}")


async def topicid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    thread_id = msg.message_thread_id
    chat = msg.chat
    if thread_id:
        await msg.reply_text(
            f"ℹ️ *Topic info*\n"
            f"Group Chat ID: `{chat.id}`\n"
            f"Topic Thread ID: `{thread_id}`\n\n"
            f"Set these as:\n`ADMIN_GROUP_ID = {chat.id}`\n`ADMIN_TOPIC_ID = {thread_id}`",
            parse_mode="Markdown",
        )
    else:
        await msg.reply_text(
            f"ℹ️ *Chat info*\n"
            f"Chat ID: `{chat.id}`\n\n"
            f"⚠️ This doesn't appear to be inside a topic/thread.\n"
            f"Run this command from within the specific topic you want bets posted to.",
            parse_mode="Markdown",
        )


async def testpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != OWNER_ID:
        return
    current_chat_id = update.message.chat.id
    results = [f"📍 This chat ID: `{current_chat_id}`", f"ANNOUNCE_GROUP_ID = `{ANNOUNCE_GROUP_ID}`", f"ANNOUNCE_TOPIC_ID = `{ANNOUNCE_TOPIC_ID}`", ""]
    try:
        chat = await context.bot.get_chat(ANNOUNCE_GROUP_ID)
        results.append(f"✅ get_chat OK: {chat.title} (type={chat.type})")
    except Exception as e:
        results.append(f"❌ get_chat failed: {e}")
    try:
        await context.bot.send_message(chat_id=ANNOUNCE_GROUP_ID, text="✅ Test post — no topic")
        results.append("✅ send_message (no topic): OK")
    except Exception as e:
        results.append(f"❌ send_message (no topic): {e}")
    try:
        await context.bot.send_message(chat_id=ANNOUNCE_GROUP_ID, message_thread_id=ANNOUNCE_TOPIC_ID, text="✅ Test post — with topic")
        results.append("✅ send_message (with topic): OK")
    except Exception as e:
        results.append(f"❌ send_message (with topic): {e}")
    await update.message.reply_text("\n".join(results))


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    await update.message.reply_text(
        f"🪪 Your Telegram ID: `{user.id}`\n"
        f"🔑 Bot's OWNER\\_ID: `{OWNER_ID}`\n"
        f"✅ Match: `{user.id == OWNER_ID}`",
        parse_mode="Markdown",
    )


async def pendingbets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ This command is restricted to the bot owner.")
        return

    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, username, amount, odds, description, status, taker_username, created_at "
        "FROM bets WHERE status IN ('matched','pending_confirm','disputed') ORDER BY id DESC"
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("✅ No pending bets to resolve right now.")
        return

    status_icons = {"matched": "🤝", "pending_confirm": "⏳", "disputed": "⚔️"}
    text = "📋 *Pending Bets — Force-Resolvable*\n\n"
    for row in rows:
        bid, poster_id, poster_name, amount, odds, desc, status, taker_name, created_at = row
        icon = status_icons.get(status, "❓")
        short_desc = desc[:40] + ("…" if len(desc) > 40 else "")
        date = created_at[:10] if created_at else "?"
        text += (
            f"{icon} *Bet #{bid}* — ${amount} @ {odds}\n"
            f"  📌 {short_desc}\n"
            f"  👤 @{poster_name} vs @{taker_name or '(no taker)'}\n"
            f"  Status: `{status}` | Created: {date}\n"
            f"  👉 `/forceresolve {bid} poster` or `/forceresolve {bid} taker`\n\n"
        )
    await update.message.reply_text(text, parse_mode="Markdown")


async def forceresolve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ This command is restricted to the bot owner.")
        return

    if len(context.args) != 2 or context.args[1] not in ("poster", "taker", "cancel"):
        await update.message.reply_text(
            "📋 *Usage:* `/forceresolve <bet_id> <poster|taker|cancel>`\n\n"
            "Examples:\n"
            "`/forceresolve 12 poster` — declares the poster as winner\n"
            "`/forceresolve 12 taker` — declares the taker as winner\n"
            "`/forceresolve 12 cancel` — cancels the bet, no winner\n\n"
            "Only works on bets with status: matched, pending\\_confirm, or disputed.",
            parse_mode="Markdown",
        )
        return

    try:
        bet_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Bet ID must be a number.")
        return

    winner_side = context.args[1]

    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, username, amount, description, status, taker_id, taker_username, created_at, poster_vote, taker_vote, cancel_initiator, chat_id, odds FROM bets WHERE id=%s",
        (bet_id,),
    )
    bet = c.fetchone()

    if not bet:
        await update.message.reply_text(f"Bet #{bet_id} not found.")
        conn.close()
        return

    status = bet[5]
    if status not in ("matched", "pending_confirm", "disputed"):
        await update.message.reply_text(f"⚠️ Bet #{bet_id} has status *{status}* and cannot be force-resolved.", parse_mode="Markdown")
        conn.close()
        return

    poster_id, poster_name = bet[1], bet[2]
    taker_id, taker_name = bet[6], bet[7]
    amount = bet[3]
    bet_chat_id = bet[12]

    # ── Cancel path ───────────────────────────────────────────────────────────
    if winner_side == "cancel":
        c.execute(
            "UPDATE bets SET status='cancelled', poster_vote=NULL, taker_vote=NULL WHERE id=%s",
            (bet_id,),
        )
        conn.commit()
        conn.close()
        cancel_text = (
            f"🚫 *Bet #{bet_id} Cancelled by Admin* (${amount})\n"
            f"_{bet[4]}_\n\n"
            f"The bet has been voided. No winner declared.\n"
            f"_(Cancelled by bot owner)_"
        )
        await update.message.reply_text(cancel_text, parse_mode="Markdown")
        if taker_id:
            await notify_user(context, poster_id, poster_name, bet_chat_id, cancel_text)
            await notify_user(context, taker_id, taker_name, bet_chat_id, cancel_text)
        else:
            await notify_user(context, poster_id, poster_name, bet_chat_id, cancel_text)
        return

    # ── Settle path ───────────────────────────────────────────────────────────
    if not taker_id:
        await update.message.reply_text(f"Bet #{bet_id} has no taker yet.")
        conn.close()
        return

    result_text = do_settle_bet(conn, c, bet, winner_side)
    c.execute("UPDATE bets SET status='settled', poster_vote=NULL, taker_vote=NULL WHERE id=%s", (bet_id,))
    conn.commit()
    conn.close()

    winner_name = poster_name if winner_side == "poster" else taker_name
    loser_name = taker_name if winner_side == "poster" else poster_name
    force_text = (
        f"⚖️ *Admin ruling on Bet #{bet_id}* (${amount})\n"
        f"✅ @{winner_name} wins | ❌ @{loser_name} loses\n"
        f"_(Resolved by bot owner)_"
    )
    await update.message.reply_text(force_text, parse_mode="Markdown")
    await notify_user(context, poster_id, poster_name, bet_chat_id, force_text)
    await notify_user(context, taker_id, taker_name, bet_chat_id, force_text)
    winner_id_r = poster_id if winner_side == "poster" else taker_id
    loser_name_r = taker_name if winner_side == "poster" else poster_name
    await send_review_prompt(context, winner_id_r, loser_name_r, bet_id, winner_side)


# ── Button handler ────────────────────────────────────────────────────────────


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    def fetch_bet(c, bet_id):
        c.execute(
            "SELECT id, user_id, username, amount, description, status, taker_id, taker_username, created_at, poster_vote, taker_vote, cancel_initiator, chat_id, odds FROM bets WHERE id=%s",
            (bet_id,),
        )
        return c.fetchone()

    # ── Giveaway entry ────────────────────────────────────────────────────────
    if data.startswith("gaenter_"):
        giveaway_id = int(data.split("_")[1])
        user = query.from_user
        username = user.username
        first_name = user.first_name

        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM giveaways WHERE id=%s", (giveaway_id,))
        giveaway = c.fetchone()

        if not giveaway:
            await query.answer("This giveaway doesn't exist.", show_alert=True)
            conn.close()
            return
        if giveaway[8] != "active":
            await query.answer("This giveaway has already ended.", show_alert=True)
            conn.close()
            return
        if datetime.now() > giveaway[6]:
            await query.answer("This giveaway has expired.", show_alert=True)
            conn.close()
            return

        require_member = giveaway[5]
        if require_member:
            is_member = await ga_check_membership(context.bot, user.id, require_member)
            if not is_member:
                await query.answer("You must be a member of the required group to enter!", show_alert=True)
                conn.close()
                return

        try:
            c.execute(
                "INSERT INTO ga_entries (giveaway_id, user_id, username, first_name) VALUES (%s,%s,%s,%s)",
                (giveaway_id, user.id, username, first_name),
            )
            conn.commit()
            c.execute("SELECT COUNT(*) FROM ga_entries WHERE giveaway_id=%s", (giveaway_id,))
            entry_count = c.fetchone()[0]
            conn.close()
            new_text = ga_build_text(giveaway, entry_count)
            try:
                await query.edit_message_text(new_text, parse_mode="Markdown", reply_markup=ga_entry_keyboard(giveaway_id))
            except Exception:
                pass
            await query.answer(f"🎟 You're entered! Good luck! ({entry_count} total entries)")
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            c.execute("SELECT COUNT(*) FROM ga_entries WHERE giveaway_id=%s", (giveaway_id,))
            entry_count = c.fetchone()[0]
            conn.close()
            await query.answer(f"You're already entered! ({entry_count} entries so far)", show_alert=True)
        except Exception as e:
            conn.close()
            await query.answer("Something went wrong. Please try again.", show_alert=True)
            print(f"[ga entry] Error: {e}")
        return

    # ── Payment reviews ───────────────────────────────────────────────────────
    if data.startswith("reviewcancel_"):
        await query.edit_message_text("Review cancelled.")
        return

    if data.startswith("review_") and not data.startswith("reviewconfirm_"):
        parts = data.split("_")
        bet_id, winner_side, rating = int(parts[1]), parts[2], parts[3]
        labels = {"fast": "✅ Paid within 1 hour", "slow": "⏰ Paid within 12 hours", "nopay": "❌ Did not pay"}
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT username, taker_username FROM bets WHERE id=%s", (bet_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            await query.edit_message_text("Bet not found.")
            return
        poster_name, taker_name = row
        loser_name = taker_name if winner_side == "poster" else poster_name
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, confirm", callback_data=f"reviewconfirm_{bet_id}_{winner_side}_{rating}")],
            [InlineKeyboardButton("🔙 Cancel", callback_data=f"reviewcancel_{bet_id}")],
        ])
        await query.edit_message_text(
            f"Confirm rating *@{loser_name}* as:\n{labels[rating]}?",
            parse_mode="Markdown",
            reply_markup=confirm_kb,
        )
        return

    if data.startswith("reviewconfirm_"):
        parts = data.split("_")
        bet_id, winner_side, rating = int(parts[1]), parts[2], parts[3]
        reviewer = query.from_user
        reviewer_name = reviewer.username or reviewer.first_name
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT username, taker_username FROM bets WHERE id=%s", (bet_id,))
        row = c.fetchone()
        if not row:
            await query.edit_message_text("Bet not found.")
            conn.close()
            return
        poster_name, taker_name = row
        loser_name = taker_name if winner_side == "poster" else poster_name
        c.execute("SELECT id FROM reviews WHERE bet_id=%s AND reviewer_id=%s", (bet_id, reviewer.id))
        if c.fetchone():
            await query.edit_message_text("You've already submitted a review for this bet.")
            conn.close()
            return
        c.execute(
            "INSERT INTO reviews (bet_id, reviewer_id, reviewer_username, reviewed_username, rating, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (bet_id, reviewer.id, reviewer_name, loser_name, rating, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        conn.close()
        labels = {"fast": "✅ Paid within 1 hour", "slow": "⏰ Paid within 12 hours", "nopay": "❌ Did not pay"}
        await query.edit_message_text(f"⭐ Review saved for *@{loser_name}*: {labels[rating]}", parse_mode="Markdown")
        return

    # ── Searchbet actions (owner only) ────────────────────────────────────────
    if data.startswith("sbcancel_"):
        if query.from_user.id != OWNER_ID:
            await query.answer("⛔ Owner only.", show_alert=True)
            return
        bet_id = int(data.split("_")[1])
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT status FROM bets WHERE id=%s", (bet_id,))
        row = c.fetchone()
        if not row or row[0] == "cancelled":
            await query.answer("Bet not found or already cancelled.", show_alert=True)
            conn.close()
            return
        c.execute("UPDATE bets SET status='cancelled' WHERE id=%s", (bet_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"🚫 Bet #{bet_id} cancelled by owner.")
        return

    if data.startswith("sbreverse_") and not data.startswith("sbreverseconfirm_"):
        if query.from_user.id != OWNER_ID:
            await query.answer("⛔ Owner only.", show_alert=True)
            return
        bet_id = int(data.split("_")[1])
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT username, taker_username, amount FROM bets WHERE id=%s", (bet_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            await query.edit_message_text("Bet not found.")
            return
        poster_name, taker_name, amount = row
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"@{poster_name} won", callback_data=f"sbreverseconfirm_{bet_id}_poster"),
             InlineKeyboardButton(f"@{taker_name} won", callback_data=f"sbreverseconfirm_{bet_id}_taker")],
            [InlineKeyboardButton("🔙 Cancel", callback_data=f"sbcancelaction_{bet_id}")],
        ])
        await query.edit_message_text(
            f"↩️ *Reverse Bet #{bet_id}* (${amount})\nWho was the original winner?",
            parse_mode="Markdown",
            reply_markup=kb,
        )
        return

    if data.startswith("sbreverseconfirm_"):
        if query.from_user.id != OWNER_ID:
            await query.answer("⛔ Owner only.", show_alert=True)
            return
        parts = data.split("_")
        bet_id, winner_side = int(parts[1]), parts[2]
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        if not bet:
            await query.edit_message_text("Bet not found.")
            conn.close()
            return
        poster_id, poster_name = bet[1], bet[2]
        taker_id, taker_name = bet[6], bet[7]
        amount = bet[3]
        winner_id = poster_id if winner_side == "poster" else taker_id
        loser_id = taker_id if winner_side == "poster" else poster_id
        reverse_result(conn, c, winner_id, loser_id, amount)
        c.execute("UPDATE bets SET status='cancelled' WHERE id=%s", (bet_id,))
        conn.commit()
        conn.close()
        winner_name = poster_name if winner_side == "poster" else taker_name
        await query.edit_message_text(
            f"↩️ *Bet #{bet_id} reversed.*\nStats rolled back — @{winner_name}'s win and opponent's loss removed.\nBet marked as cancelled.",
            parse_mode="Markdown",
        )
        return

    if data.startswith("sbclearreviews_"):
        if query.from_user.id != OWNER_ID:
            await query.answer("⛔ Owner only.", show_alert=True)
            return
        bet_id = int(data.split("_")[1])
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("DELETE FROM reviews WHERE bet_id=%s", (bet_id,))
        removed = c.rowcount
        conn.commit()
        conn.close()
        await query.edit_message_text(f"🗑️ {removed} review(s) cleared for Bet #{bet_id}.")
        return

    if data.startswith("sbcancelaction_"):
        await query.edit_message_text("Action cancelled.")
        return

    # ── GCactivebets force-settle (owner only) ────────────────────────────────
    if data.startswith("gcsettle_"):
        if query.from_user.id != OWNER_ID:
            await query.answer("⛔ Only the bot owner can resolve bets.", show_alert=True)
            return
        parts = data.split("_")
        bet_id = int(parts[1])
        winner_side = parts[2]
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        if not bet:
            await query.edit_message_text("Bet not found.")
            conn.close()
            return
        if bet[5] not in ("matched", "pending_confirm", "disputed"):
            await query.answer(f"Bet #{bet_id} cannot be resolved (status: {bet[5]}).", show_alert=True)
            conn.close()
            return
        poster_name, taker_name = bet[2], bet[7]
        amount, bet_chat_id = bet[3], bet[12]
        poster_id, taker_id = bet[1], bet[6]
        do_settle_bet(conn, c, bet, winner_side)
        c.execute("UPDATE bets SET status='settled', poster_vote=NULL, taker_vote=NULL WHERE id=%s", (bet_id,))
        conn.commit()
        conn.close()
        winner_name = poster_name if winner_side == "poster" else taker_name
        loser_name = taker_name if winner_side == "poster" else poster_name
        result = (
            f"⚖️ *Bet #{bet_id} force-resolved*\n"
            f"✅ @{winner_name} wins ${amount} | ❌ @{loser_name} loses\n"
            f"_(Resolved by bot owner)_"
        )
        await query.edit_message_text(result, parse_mode="Markdown")
        await notify_user(context, poster_id, poster_name, bet_chat_id, result)
        await notify_user(context, taker_id, taker_name, bet_chat_id, result)
        winner_id_r = poster_id if winner_side == "poster" else taker_id
        loser_name_r = taker_name if winner_side == "poster" else poster_name
        await send_review_prompt(context, winner_id_r, loser_name_r, bet_id, winner_side)
        return

    # ── Take a bet ────────────────────────────────────────────────────────────
    if data.startswith("take_") and not data.startswith("takenow_"):
        bet_id = int(data.split("_")[1])
        taker = query.from_user
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        conn.close()
        if not bet:
            await query.edit_message_text("Bet not found.")
            return
        if bet[5] != "open":
            await query.answer(f"Bet #{bet_id} is already taken.", show_alert=True)
            return
        if bet[1] == taker.id:
            await query.answer("You can't take your own bet!", show_alert=True)
            return
        amount = bet[3]
        odds = bet[13] if bet[13] else "?"
        desc = bet[4][:50] + ("…" if len(bet[4]) > 50 else "")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"✅ Take at ${amount}", callback_data=f"takenow_{bet_id}")],
            [InlineKeyboardButton("💱 Counter Offer", callback_data=f"counteroffer_{bet_id}")],
        ])
        await query.edit_message_text(
            f"*Bet #{bet_id}* — ${amount} @ {odds}\n📝 {desc}\n\nAccept or send a counter offer?",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    elif data.startswith("takenow_"):
        bet_id = int(data.split("_")[1])
        taker = query.from_user
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        if not bet:
            await query.edit_message_text("Bet not found.")
            conn.close()
            return
        if bet[5] != "open":
            await query.answer(f"Bet #{bet_id} is already taken.", show_alert=True)
            conn.close()
            return
        if bet[1] == taker.id:
            await query.answer("You can't take your own bet!", show_alert=True)
            conn.close()
            return
        taker_name = taker.username or taker.first_name
        poster_id, poster_name = bet[1], bet[2]
        amount, desc, bet_chat_id = bet[3], bet[4], bet[12]
        upsert_user(c, taker.id, taker_name)
        c.execute(
            "UPDATE bets SET status='matched', taker_id=%s, taker_username=%s WHERE id=%s",
            (taker.id, taker_name, bet_id),
        )
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ @{taker_name} took Bet #{bet_id}!\nBoth sides use /settle when the outcome is known.")
        await notify_user(context, poster_id, poster_name, bet_chat_id, f'📣 @{taker_name} just took your Bet #{bet_id} (${amount})!\n"{desc}"\n\nUse /settle when it\'s done.')
        if ANNOUNCE_GROUP_ID and ANNOUNCE_TOPIC_ID:
            odds = bet[13] if bet[13] else "?"
            try:
                await context.bot.send_message(
                    chat_id=ANNOUNCE_GROUP_ID,
                    message_thread_id=ANNOUNCE_TOPIC_ID,
                    text=f"🤝 *Wager Confirmed — Bet #{bet_id}*\n👤 @{poster_name} vs @{taker_name}\n💰 ${amount} @ {odds}\n📝 {desc}",
                    parse_mode="Markdown",
                )
            except Exception as e:
                print(f"[take] Failed to announce Bet #{bet_id}: {e}")

    elif data.startswith("counteroffer_"):
        bet_id = int(data.split("_")[1])
        taker = query.from_user
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        conn.close()
        if not bet or bet[5] != "open":
            await query.answer("This bet is no longer open.", show_alert=True)
            return
        if bet[1] == taker.id:
            await query.answer("You can't counter your own bet!", show_alert=True)
            return
        context.user_data["pending_counter_bet"] = bet_id
        await query.edit_message_text(
            f"💱 *Counter Offer for Bet #{bet_id}*\nOriginal amount: *${bet[3]}*\n\nReply with your proposed amount (numbers only):",
            parse_mode="Markdown",
        )

    elif data.startswith("acceptcounter_"):
        parts = data.split("_")
        bet_id, taker_id, new_amount = int(parts[1]), int(parts[2]), int(parts[3])
        poster = query.from_user
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        if not bet or bet[5] != "open":
            await query.edit_message_text("This bet is no longer available.")
            conn.close()
            return
        if poster.id != bet[1]:
            await query.answer("Only the bet poster can accept counters.", show_alert=True)
            conn.close()
            return
        taker_name_lookup = None
        try:
            taker_chat = await context.bot.get_chat(taker_id)
            taker_name_lookup = taker_chat.username or taker_chat.first_name
        except Exception:
            taker_name_lookup = str(taker_id)
        poster_name = bet[2]
        desc, bet_chat_id = bet[4], bet[12]
        odds = bet[13] if bet[13] else "?"
        upsert_user(c, taker_id, taker_name_lookup)
        c.execute(
            "UPDATE bets SET status='matched', taker_id=%s, taker_username=%s, amount=%s WHERE id=%s",
            (taker_id, taker_name_lookup, new_amount, bet_id),
        )
        conn.commit()
        conn.close()
        await query.edit_message_text(f"✅ Counter accepted! Bet #{bet_id} matched at *${new_amount}*.\nBoth sides use /settle when the outcome is known.", parse_mode="Markdown")
        try:
            await context.bot.send_message(chat_id=taker_id, text=f"🎉 @{poster_name} accepted your counter offer of ${new_amount} on Bet #{bet_id}!\nUse /settle when it's done.")
        except Exception:
            pass
        if ANNOUNCE_GROUP_ID and ANNOUNCE_TOPIC_ID:
            try:
                await context.bot.send_message(
                    chat_id=ANNOUNCE_GROUP_ID,
                    message_thread_id=ANNOUNCE_TOPIC_ID,
                    text=f"🤝 *Wager Confirmed — Bet #{bet_id}* _(counter offer)_\n👤 @{poster_name} vs @{taker_name_lookup}\n💰 ${new_amount} @ {odds}\n📝 {desc}",
                    parse_mode="Markdown",
                )
            except Exception as e:
                print(f"[counter] Failed to announce Bet #{bet_id}: {e}")

    elif data.startswith("declinecounter_"):
        parts = data.split("_")
        bet_id, taker_id = int(parts[1]), int(parts[2])
        poster = query.from_user
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        conn.close()
        if not bet:
            await query.edit_message_text("Bet not found.")
            return
        if poster.id != bet[1]:
            await query.answer("Only the bet poster can decline counters.", show_alert=True)
            return
        await query.edit_message_text(f"❌ Counter offer declined for Bet #{bet_id}. The bet remains open.")
        try:
            await context.bot.send_message(chat_id=taker_id, text=f"❌ @{bet[2]} declined your counter offer on Bet #{bet_id}. The bet is still open.")
        except Exception:
            pass

    # ── Vote on outcome ───────────────────────────────────────────────────────
    elif data.startswith("vote_"):
        parts = data.split("_")
        bet_id = int(parts[1])
        voter_side = parts[2]
        claimed_winner = parts[3]
        voter = query.from_user
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        if not bet:
            await query.edit_message_text("Bet not found.")
            conn.close()
            return
        poster_id, poster_name = bet[1], bet[2]
        taker_id, taker_name = bet[6], bet[7]
        amount, desc = bet[3], bet[4]
        status = bet[5]
        poster_vote, taker_vote = bet[9], bet[10]
        bet_chat_id = bet[12]
        if voter_side == "poster" and voter.id != poster_id:
            await query.answer("Only the bet poster can vote here.", show_alert=True)
            conn.close()
            return
        if voter_side == "taker" and voter.id != taker_id:
            await query.answer("Only the bet taker can vote here.", show_alert=True)
            conn.close()
            return
        if status == "settled":
            await query.answer("This bet is already settled.", show_alert=True)
            conn.close()
            return
        if status == "disputed":
            c.execute("UPDATE bets SET poster_vote=NULL, taker_vote=NULL, status='matched' WHERE id=%s", (bet_id,))
            conn.commit()
            poster_vote, taker_vote = None, None
            status = "matched"
        if voter_side == "poster":
            c.execute("UPDATE bets SET poster_vote=%s, status='pending_confirm' WHERE id=%s", (claimed_winner, bet_id))
            other_id, other_name, other_side = taker_id, taker_name, "taker"
            existing_other_vote = taker_vote
        else:
            c.execute("UPDATE bets SET taker_vote=%s, status='pending_confirm' WHERE id=%s", (claimed_winner, bet_id))
            other_id, other_name, other_side = poster_id, poster_name, "poster"
            existing_other_vote = poster_vote
        conn.commit()
        winner_name = poster_name if claimed_winner == "poster" else taker_name
        loser_name = taker_name if claimed_winner == "poster" else poster_name
        voter_display = poster_name if voter_side == "poster" else taker_name
        other_claimed = "taker" if claimed_winner == "poster" else "poster"
        if existing_other_vote is not None:
            if existing_other_vote == claimed_winner:
                result_text = do_settle_bet(conn, c, bet, claimed_winner)
                c.execute("UPDATE bets SET status='settled' WHERE id=%s", (bet_id,))
                conn.commit()
                conn.close()
                await query.edit_message_text(result_text)
                await notify_user(context, other_id, other_name, bet_chat_id, result_text)
                winner_id_r = poster_id if claimed_winner == "poster" else taker_id
                loser_name_r = taker_name if claimed_winner == "poster" else poster_name
                await send_review_prompt(context, winner_id_r, loser_name_r, bet_id, claimed_winner)
            else:
                c.execute("UPDATE bets SET status='disputed' WHERE id=%s", (bet_id,))
                conn.commit()
                conn.close()
                dispute_text = (
                    f"⚠️ *Dispute on Bet #{bet_id}* (${amount})\n"
                    f"Both sides reported different outcomes.\n"
                    f"Talk it out and use /settle again to re-submit."
                )
                await query.edit_message_text(dispute_text, parse_mode="Markdown")
                await notify_user(context, other_id, other_name, bet_chat_id, dispute_text)
            return
        conn.close()
        await query.edit_message_text(
            f"⏳ Vote recorded for Bet #{bet_id}.\nYou said *@{winner_name}* won. Waiting for @{other_name} to confirm.",
            parse_mode="Markdown",
        )
        confirm_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(f"✅ Confirm — @{winner_name} won", callback_data=f"vote_{bet_id}_{other_side}_{claimed_winner}"),
                InlineKeyboardButton(f"❌ Dispute — @{loser_name} won", callback_data=f"vote_{bet_id}_{other_side}_{other_claimed}"),
            ]
        ])
        await notify_user(
            context, other_id, other_name, bet_chat_id,
            f"⚖️ *Confirmation needed — Bet #{bet_id}* (${amount})\n\"{desc}\"\n\n@{voter_display} says *@{winner_name}* won. Do you agree?",
            keyboard=confirm_kb,
        )

    # ── Cancel open bet (instant) ─────────────────────────────────────────────
    elif data.startswith("cancelopen_"):
        bet_id = int(data.split("_")[1])
        actor = query.from_user
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("SELECT user_id, status FROM bets WHERE id=%s", (bet_id,))
        row = c.fetchone()
        if not row:
            await query.edit_message_text("Bet not found.")
            conn.close()
            return
        if row[0] != actor.id:
            await query.answer("Only the bet poster can cancel an open bet.", show_alert=True)
            conn.close()
            return
        if row[1] != "open":
            await query.answer("This bet is no longer open.", show_alert=True)
            conn.close()
            return
        c.execute("UPDATE bets SET status='cancelled' WHERE id=%s", (bet_id,))
        conn.commit()
        conn.close()
        await query.edit_message_text(f"🚫 Bet #{bet_id} cancelled.")

    # ── Request mutual cancel ─────────────────────────────────────────────────
    elif data.startswith("cancelreq_"):
        bet_id = int(data.split("_")[1])
        actor = query.from_user
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        conn.close()
        if not bet:
            await query.edit_message_text("Bet not found.")
            return
        poster_id, poster_name = bet[1], bet[2]
        taker_id, taker_name = bet[6], bet[7]
        amount, desc, bet_chat_id = bet[3], bet[4], bet[12]
        if actor.id not in (poster_id, taker_id):
            await query.answer("You're not part of this bet.", show_alert=True)
            return
        if bet[5] in ("cancelled", "settled"):
            await query.answer("This bet is already closed.", show_alert=True)
            return
        if actor.id == poster_id:
            other_id, other_name, initiator_side = taker_id, taker_name, "poster"
        else:
            other_id, other_name, initiator_side = poster_id, poster_name, "taker"
        conn = get_db_conn()
        c = conn.cursor()
        c.execute("UPDATE bets SET status='pending_cancel', cancel_initiator=%s WHERE id=%s", (initiator_side, bet_id))
        conn.commit()
        conn.close()
        actor_name = actor.username or actor.first_name
        await query.edit_message_text(f"🔄 Cancel request sent for Bet #{bet_id}.\nWaiting for @{other_name} to agree.")
        confirm_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, cancel it", callback_data=f"cancelconfirm_{bet_id}"),
             InlineKeyboardButton("❌ No, keep it", callback_data=f"canceldeny_{bet_id}")]
        ])
        await notify_user(
            context, other_id, other_name, bet_chat_id,
            f"🔄 *@{actor_name} wants to cancel Bet #{bet_id}* (${amount})\n\"{desc}\"\n\nAgree to cancel? No stats recorded.",
            keyboard=confirm_kb,
        )

    # ── Confirm cancel ────────────────────────────────────────────────────────
    elif data.startswith("cancelconfirm_"):
        bet_id = int(data.split("_")[1])
        actor = query.from_user
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        if not bet:
            await query.edit_message_text("Bet not found.")
            conn.close()
            return
        poster_id, poster_name = bet[1], bet[2]
        taker_id, taker_name = bet[6], bet[7]
        cancel_initiator = bet[11]
        bet_chat_id = bet[12]
        if actor.id not in (poster_id, taker_id):
            await query.answer("You're not part of this bet.", show_alert=True)
            conn.close()
            return
        if cancel_initiator == "poster" and actor.id == poster_id:
            await query.answer("You already requested the cancel. Waiting for the other party.", show_alert=True)
            conn.close()
            return
        if cancel_initiator == "taker" and actor.id == taker_id:
            await query.answer("You already requested the cancel. Waiting for the other party.", show_alert=True)
            conn.close()
            return
        c.execute("UPDATE bets SET status='cancelled' WHERE id=%s", (bet_id,))
        conn.commit()
        conn.close()
        cancel_text = f"🚫 Bet #{bet_id} mutually cancelled. No stats recorded."
        await query.edit_message_text(cancel_text)
        other_id = poster_id if actor.id == taker_id else taker_id
        other_name = poster_name if actor.id == taker_id else taker_name
        await notify_user(context, other_id, other_name, bet_chat_id, cancel_text)

    # ── Deny cancel ───────────────────────────────────────────────────────────
    elif data.startswith("canceldeny_"):
        bet_id = int(data.split("_")[1])
        actor = query.from_user
        conn = get_db_conn()
        c = conn.cursor()
        bet = fetch_bet(c, bet_id)
        if not bet:
            await query.edit_message_text("Bet not found.")
            conn.close()
            return
        poster_id, poster_name = bet[1], bet[2]
        taker_id, taker_name = bet[6], bet[7]
        bet_chat_id = bet[12]
        c.execute("UPDATE bets SET status='matched', cancel_initiator=NULL WHERE id=%s", (bet_id,))
        conn.commit()
        conn.close()
        actor_name = actor.username or actor.first_name
        deny_text = f"❌ @{actor_name} declined the cancel for Bet #{bet_id}. The bet is still on."
        await query.edit_message_text(deny_text)
        other_id = poster_id if actor.id == taker_id else taker_id
        other_name = poster_name if actor.id == taker_id else taker_name
        await notify_user(context, other_id, other_name, bet_chat_id, deny_text)


# ── Counter offer amount capture ───────────────────────────────────────────────


async def handle_counter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bet_id = context.user_data.get("pending_counter_bet")
    if not bet_id:
        return

    text = update.message.text.strip().lstrip("$").replace(",", "")
    if not text.isdigit():
        await update.message.reply_text("Please enter a valid whole number for the counter amount.")
        return

    new_amount = int(text)
    if new_amount <= 0:
        await update.message.reply_text("Amount must be greater than 0.")
        return

    taker = update.message.from_user
    taker_name = taker.username or taker.first_name

    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, user_id, username, amount, description, status, taker_id, taker_username, created_at, poster_vote, taker_vote, cancel_initiator, chat_id, odds FROM bets WHERE id=%s",
        (bet_id,),
    )
    bet = c.fetchone()
    conn.close()

    if not bet or bet[5] != "open":
        await update.message.reply_text(f"Bet #{bet_id} is no longer open.")
        context.user_data.pop("pending_counter_bet", None)
        return

    poster_id, poster_name = bet[1], bet[2]
    original_amount = bet[3]
    desc = bet[4][:50] + ("…" if len(bet[4]) > 50 else "")

    context.user_data.pop("pending_counter_bet", None)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Accept ${new_amount}", callback_data=f"acceptcounter_{bet_id}_{taker.id}_{new_amount}")],
        [InlineKeyboardButton("❌ Decline", callback_data=f"declinecounter_{bet_id}_{taker.id}")],
    ])

    try:
        await context.bot.send_message(
            chat_id=poster_id,
            text=(
                f"💱 *Counter Offer on Bet #{bet_id}*\n"
                f"@{taker_name} proposes *${new_amount}* (original: ${original_amount})\n"
                f"📝 {desc}"
            ),
            parse_mode="Markdown",
            reply_markup=kb,
        )
        await update.message.reply_text(
            f"✅ Counter offer of *${new_amount}* sent to @{poster_name} for Bet #{bet_id}.\nWaiting for their response.",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Couldn't reach @{poster_name} — make sure they've started the bot first.")
        print(f"[counter] Failed to DM poster: {e}")


# ── Activity tracker ──────────────────────────────────────────────────────────


async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Record every group message so /activegiveaway can pick from recent chatters."""
    msg = update.message or update.edited_message
    if not msg or not msg.from_user:
        return
    user = msg.from_user
    if user.is_bot:
        return
    chat_id = msg.chat.id
    if msg.chat.type not in ("group", "supergroup"):
        return
    try:
        conn = get_db_conn()
        c = conn.cursor()
        c.execute(
            """INSERT INTO chat_activity (chat_id, user_id, username, first_name, last_seen)
               VALUES (%s, %s, %s, %s, NOW())
               ON CONFLICT (chat_id, user_id) DO UPDATE
               SET username=EXCLUDED.username, first_name=EXCLUDED.first_name, last_seen=NOW()""",
            (chat_id, user.id, user.username, user.first_name),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[activity] Error: {e}")


# ── Giveaway helpers ──────────────────────────────────────────────────────────


def ga_fmt_time_left(ends_at: datetime) -> str:
    diff = ends_at - datetime.now()
    if diff.total_seconds() <= 0:
        return "Ended"
    total = int(diff.total_seconds())
    d, rem = divmod(total, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    return " ".join(parts) or "<1m"


def ga_build_text(giveaway, entry_count: int) -> str:
    gid, chat_id, msg_id, prize, num_winners, require_member, ends_at, created_by, status, created_at = giveaway
    time_left = ga_fmt_time_left(ends_at) if status == "active" else "Ended"
    winner_str = f"{num_winners} winner{'s' if num_winners > 1 else ''}"
    req_str = f"\n🔒 Must be a member of the required group" if require_member else ""
    return (
        f"🎉 *GIVEAWAY #{gid}*\n\n"
        f"🏆 *Prize:* {prize}\n"
        f"👥 *Winners:* {winner_str}\n"
        f"⏰ *Time left:* {time_left}\n"
        f"📊 *Entries:* {entry_count}"
        f"{req_str}\n\n"
        f"Click the button below to enter!"
    )


def ga_entry_keyboard(giveaway_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎟 Enter Giveaway", callback_data=f"gaenter_{giveaway_id}")
    ]])


async def ga_check_membership(bot, user_id: int, chat_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator", "restricted")
    except Exception:
        return False


async def ga_do_draw(context, giveaway_id: int, chat_id: int, reroll: bool = False):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM giveaways WHERE id=%s", (giveaway_id,))
    giveaway = c.fetchone()
    if not giveaway:
        conn.close()
        return

    num_winners = giveaway[4]
    prize = giveaway[3]

    c.execute(
        "SELECT user_id, username, first_name FROM ga_entries WHERE giveaway_id=%s ORDER BY RANDOM() LIMIT %s",
        (giveaway_id, num_winners),
    )
    chosen = c.fetchall()

    if not chosen:
        c.execute("UPDATE giveaways SET status='ended' WHERE id=%s", (giveaway_id,))
        conn.commit()
        conn.close()
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"😢 *Giveaway #{giveaway_id}* ended with no entries.",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    if reroll:
        c.execute("DELETE FROM ga_winners WHERE giveaway_id=%s", (giveaway_id,))

    for user_id, username, first_name in chosen:
        c.execute(
            "INSERT INTO ga_winners (giveaway_id, user_id, username, first_name) VALUES (%s,%s,%s,%s)",
            (giveaway_id, user_id, username, first_name),
        )

    c.execute("UPDATE giveaways SET status='ended' WHERE id=%s", (giveaway_id,))
    conn.commit()
    conn.close()

    mentions = []
    for user_id, username, first_name in chosen:
        if username:
            mentions.append(f"@{username}")
        else:
            mentions.append(f"[{first_name}](tg://user?id={user_id})")

    winner_list = "\n".join(f"🏆 {m}" for m in mentions)
    prefix = "🔄 *Reroll — " if reroll else "🎉 *"
    result_text = (
        f"{prefix}Giveaway #{giveaway_id} Winner{'s' if len(chosen) > 1 else ''}!*\n\n"
        f"🎁 Prize: *{prize}*\n\n"
        f"{winner_list}\n\n"
        f"Congratulations! 🎊"
    )
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=result_text,
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"[ga] Failed to announce winners: {e}")


async def ga_schedule_end(context, giveaway_id: int, chat_id: int, delay_seconds: float):
    await asyncio.sleep(delay_seconds)
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT status FROM giveaways WHERE id=%s", (giveaway_id,))
    row = c.fetchone()
    conn.close()
    if not row or row[0] != "active":
        return
    await ga_do_draw(context, giveaway_id, chat_id)


# ── Giveaway commands ─────────────────────────────────────────────────────────


async def newgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ Only the bot owner can create giveaways.")
        return

    usage = (
        "📋 *Usage:* `/newgiveaway <minutes> <winners> <prize>`\n\n"
        "To require group membership add `require:<chat_id>`:\n"
        "`/newgiveaway 60 1 require:-1001234567 $50 gift card`\n\n"
        "Examples:\n"
        "`/newgiveaway 30 1 PS5 Controller`\n"
        "`/newgiveaway 1440 3 $100 prize pool`"
    )

    if len(context.args) < 3:
        await update.message.reply_text(usage, parse_mode="Markdown")
        return

    try:
        minutes = int(context.args[0])
        if minutes < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ First argument must be a positive number of minutes.")
        return

    try:
        num_winners = int(context.args[1])
        if num_winners < 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Second argument must be a positive number of winners.")
        return

    require_member = None
    filtered_args = []
    for arg in context.args[2:]:
        if arg.lower().startswith("require:"):
            try:
                require_member = int(arg.split(":", 1)[1])
            except ValueError:
                await update.message.reply_text("⚠️ Invalid chat ID in require: field.")
                return
        else:
            filtered_args.append(arg)

    prize = " ".join(filtered_args).strip()
    if not prize:
        await update.message.reply_text("⚠️ Please provide a prize description.")
        return

    ends_at = datetime.now() + timedelta(minutes=minutes)
    # Post to the giveaway topic if configured, otherwise current chat
    target_chat = ANNOUNCE_GROUP_ID if (ANNOUNCE_GROUP_ID and GIVEAWAY_TOPIC_ID) else update.effective_chat.id
    target_topic = GIVEAWAY_TOPIC_ID if (ANNOUNCE_GROUP_ID and GIVEAWAY_TOPIC_ID) else None

    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO giveaways (chat_id, prize, num_winners, require_member, ends_at, created_by) "
        "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
        (target_chat, prize, num_winners, require_member, ends_at, user.id),
    )
    giveaway_id = c.fetchone()[0]
    conn.commit()
    c.execute("SELECT * FROM giveaways WHERE id=%s", (giveaway_id,))
    giveaway = c.fetchone()
    conn.close()

    text = ga_build_text(giveaway, 0)
    kb = ga_entry_keyboard(giveaway_id)

    send_kwargs = {"chat_id": target_chat, "text": text, "parse_mode": "Markdown", "reply_markup": kb}
    if target_topic:
        send_kwargs["message_thread_id"] = target_topic

    try:
        msg = await context.bot.send_message(**send_kwargs)
        if target_topic:
            await update.message.reply_text(f"✅ Giveaway #{giveaway_id} posted to the giveaway topic!")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Failed to post to giveaway topic: {e}\nPosting here instead.")
        msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)

    conn = get_db_conn()
    c = conn.cursor()
    c.execute("UPDATE giveaways SET message_id=%s WHERE id=%s", (msg.message_id, giveaway_id))
    conn.commit()
    conn.close()

    delay = (ends_at - datetime.now()).total_seconds()
    asyncio.create_task(ga_schedule_end(context, giveaway_id, target_chat, delay))
    print(f"[GA] Created #{giveaway_id} | prize={prize} | winners={num_winners} | ends={ends_at} | topic={target_topic}")


async def endgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ Only the bot owner can end giveaways.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /endgiveaway <id>")
        return

    giveaway_id = int(context.args[0])
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT status, chat_id FROM giveaways WHERE id=%s", (giveaway_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        await update.message.reply_text(f"Giveaway #{giveaway_id} not found.")
        return
    if row[0] != "active":
        await update.message.reply_text(f"Giveaway #{giveaway_id} is already ended.")
        return

    conn = get_db_conn()
    c = conn.cursor()
    c.execute("UPDATE giveaways SET ends_at=NOW() WHERE id=%s", (giveaway_id,))
    conn.commit()
    conn.close()
    await ga_do_draw(context, giveaway_id, row[1])


async def rerollgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ Only the bot owner can reroll giveaways.")
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /reroll <id>")
        return

    giveaway_id = int(context.args[0])
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM giveaways WHERE id=%s", (giveaway_id,))
    row = c.fetchone()
    conn.close()

    if not row:
        await update.message.reply_text(f"Giveaway #{giveaway_id} not found.")
        return
    await ga_do_draw(context, giveaway_id, row[0], reroll=True)


async def giveawayinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /giveawayinfo <id>")
        return

    giveaway_id = int(context.args[0])
    conn = get_db_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM giveaways WHERE id=%s", (giveaway_id,))
    giveaway = c.fetchone()
    if not giveaway:
        await update.message.reply_text(f"Giveaway #{giveaway_id} not found.")
        conn.close()
        return

    c.execute("SELECT COUNT(*) FROM ga_entries WHERE giveaway_id=%s", (giveaway_id,))
    entry_count = c.fetchone()[0]
    c.execute("SELECT username, first_name FROM ga_winners WHERE giveaway_id=%s", (giveaway_id,))
    winner_rows = c.fetchall()
    conn.close()

    text = ga_build_text(giveaway, entry_count)
    if winner_rows:
        winner_list = "\n".join(
            f"🏆 @{w[0]}" if w[0] else f"🏆 {w[1]}" for w in winner_rows
        )
        text += f"\n\n*Winners:*\n{winner_list}"

    kb = ga_entry_keyboard(giveaway_id) if giveaway[8] == "active" else None
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)


async def activegiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pick a random winner from users who have been active in this chat."""
    user = update.message.from_user
    if user.id != OWNER_ID:
        await update.message.reply_text("⛔ Only the bot owner can run this.")
        return

    usage = (
        "📋 *Usage:* `/giveaway <hours> <prize>`\n\n"
        "Announces a 30-minute countdown, then picks a random winner from anyone who chatted in the last X hours.\n\n"
        "Examples:\n"
        "`/giveaway 24 $50 cash` — eligible: active in last 24 hours\n"
        "`/giveaway 1 Nike shirt` — eligible: active in last 1 hour"
    )

    if len(context.args) < 2:
        await update.message.reply_text(usage, parse_mode="Markdown")
        return

    try:
        hours = float(context.args[0])
        if hours <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ First argument must be a positive number of hours.")
        return

    prize = " ".join(context.args[1:]).strip()
    if not prize:
        await update.message.reply_text("⚠️ Please provide a prize.")
        return

    chat_id = update.effective_chat.id
    hours_str = f"{hours:g} hour{'s' if hours != 1 else ''}"

    # Announce the countdown
    await update.message.reply_text(
        f"🎉 *Active Member Giveaway Starting!*\n\n"
        f"🏆 *Prize:* {prize}\n"
        f"👥 *Eligible:* Anyone who has chatted in the last {hours_str}\n\n"
        f"⏳ Winner will be drawn in *30 minutes*!\n"
        f"Keep chatting to stay eligible!",
        parse_mode="Markdown",
    )

    async def draw_after_delay():
        await asyncio.sleep(30 * 60)

        conn = get_db_conn()
        c = conn.cursor()
        c.execute(
            """SELECT user_id, username, first_name FROM chat_activity
               WHERE chat_id=%s AND last_seen >= NOW() - (%s * INTERVAL '1 hour')""",
            (chat_id, hours),
        )
        eligible = c.fetchall()
        conn.close()

        if not eligible:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"😢 *Active Member Giveaway* — no eligible users found for the *{prize}* prize.",
                    parse_mode="Markdown",
                )
            except Exception:
                pass
            return

        import random
        winner = random.choice(eligible)
        winner_id, winner_username, winner_first = winner

        if winner_username:
            mention = f"@{winner_username}"
        else:
            mention = f"[{winner_first}](tg://user?id={winner_id})"

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🎊 *Active Member Giveaway — Winner Drawn!*\n\n"
                    f"🏆 *Prize:* {prize}\n"
                    f"👥 *Pool:* {len(eligible)} eligible member{'s' if len(eligible) != 1 else ''} (active last {hours_str})\n\n"
                    f"🎉 *Winner:* {mention}\n\n"
                    f"Congratulations!"
                ),
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"[activegiveaway] Failed to announce winner: {e}")

    asyncio.create_task(draw_after_delay())


async def activegiveaways(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = get_db_conn()
    c = conn.cursor()
    c.execute(
        "SELECT g.id, g.prize, g.num_winners, g.ends_at, COUNT(e.id) "
        "FROM giveaways g LEFT JOIN ga_entries e ON e.giveaway_id=g.id "
        "WHERE g.status='active' GROUP BY g.id ORDER BY g.ends_at ASC LIMIT 10"
    )
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No active giveaways right now.")
        return

    text = "🎉 *Active Giveaways*\n\n"
    keyboard = []
    for gid, prize, num_winners, ends_at, entry_count in rows:
        time_left = ga_fmt_time_left(ends_at)
        winner_str = f"{num_winners}W"
        text += f"*#{gid}* — {prize} | {winner_str} | ⏰ {time_left} | 📊 {entry_count} entries\n"
        keyboard.append([InlineKeyboardButton(f"🎟 Enter #{gid}", callback_data=f"gaenter_{gid}")])

    await update.message.reply_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ── Main ──────────────────────────────────────────────────────────────────────


async def delete_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
    chat_id = msg.chat.id
    members = [m.full_name for m in (msg.new_chat_members or [])]
    print(f"[JOIN] chat={chat_id} new_members={members}")
    try:
        await msg.delete()
        print(f"[JOIN] Deleted join message in chat={chat_id}")
    except Exception as e:
        print(f"[JOIN] Failed to delete in chat={chat_id}: {e}")


def main():
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("postbet", postbet_start)],
        states={
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, postbet_amount)],
            ODDS: [MessageHandler(filters.TEXT & ~filters.COMMAND, postbet_odds)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, postbet_description)],
        },
        fallbacks=[],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("openbets", openbets))
    app.add_handler(CommandHandler("mybets", mybets))
    app.add_handler(CommandHandler("settle", settle))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("rep", stats))
    app.add_handler(CommandHandler("gcactivebets", activebets))
    app.add_handler(CommandHandler("adddebt", adddebt))
    app.add_handler(CommandHandler("cleardebt", cleardebt))
    app.add_handler(CommandHandler("topicid", topicid))
    app.add_handler(CommandHandler("testpost", testpost))
    app.add_handler(CommandHandler("searchbet", searchbet))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("pendingbets", pendingbets))
    app.add_handler(CommandHandler("forceresolve", forceresolve))
    app.add_handler(CommandHandler("newgiveaway", newgiveaway))
    app.add_handler(CommandHandler("endgiveaway", endgiveaway))
    app.add_handler(CommandHandler("reroll", rerollgiveaway))
    app.add_handler(CommandHandler("giveawayinfo", giveawayinfo))
    app.add_handler(CommandHandler("giveaways", activegiveaways))
    app.add_handler(CommandHandler("giveaway", activegiveaway))
    app.add_handler(
        MessageHandler(
            filters.Chat(ADMIN_GROUP_ID) if ADMIN_GROUP_ID else filters.ALL,
            admin_topic_guard,
        ),
        group=1,
    )
    app.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.GROUPS, track_activity),
        group=3,
    )
    app.add_handler(conv)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, handle_counter_amount),
        group=2,
    )
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, delete_join_message),
        group=4,
    )

    print(f"Bot Running (PostgreSQL) | ADMIN_GROUP_ID={ADMIN_GROUP_ID} ADMIN_TOPIC_ID={ADMIN_TOPIC_ID} | ANNOUNCE_GROUP_ID={ANNOUNCE_GROUP_ID} ANNOUNCE_TOPIC_ID={ANNOUNCE_TOPIC_ID}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
