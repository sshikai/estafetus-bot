import os
import re
import time
import random
import sqlite3
import threading

import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

# ===== НАСТРОЙКИ =====
VK_TOKEN = os.environ.get("VK_TOKEN", "").strip()
ADMINS = [int(x) for x in re.split(r"[,\s]+", os.environ.get("ADMINS", "")) if x.strip().isdigit()]
OWNER_ID = 479753606

ARMOR_TIME = int(os.environ.get("ARMOR_TIME", "35")) * 60
WAREHOUSE_TIME = int(os.environ.get("WAREHOUSE_TIME", "50")) * 60
ARMOR_WAIT = int(os.environ.get("ARMOR_WAIT", "50")) * 60
WAREHOUSE_WAIT = int(os.environ.get("WAREHOUSE_WAIT", "150")) * 60
FALSE_ALARM_TIME = int(os.environ.get("FALSE_ALARM_TIME", "10")) * 60

# ===== БАЗА ДАННЫХ =====
DATA_DIR = "/app/data" if os.path.isdir("/app/data") else os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(DATA_DIR, "bot.db")

CONN = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
CONN.row_factory = sqlite3.Row
DB_LOCK = threading.Lock()

VK = None
NAME_CACHE = {}
OWNER_CACHE = {}


def init_db():
    with DB_LOCK:
        CONN.execute("""CREATE TABLE IF NOT EXISTS estafeta (
            type TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'inactive',
            current_holder INTEGER,
            started_at INTEGER,
            waiting_until INTEGER,
            pending_confirm INTEGER DEFAULT 0)""")
        CONN.execute("""CREATE TABLE IF NOT EXISTS fullers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT,
            user_id INTEGER)""")
        CONN.execute("""CREATE TABLE IF NOT EXISTS penalties (
            user_id INTEGER PRIMARY KEY,
            cnt INTEGER NOT NULL DEFAULT 0)""")
        CONN.execute("""CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT)""")
        CONN.execute("""CREATE TABLE IF NOT EXISTS log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            type TEXT,
            taken_at INTEGER,
            released_at INTEGER,
            reason TEXT)""")
        CONN.execute("INSERT OR IGNORE INTO estafeta(type,status) VALUES('armor','inactive')")
        CONN.execute("INSERT OR IGNORE INTO estafeta(type,status) VALUES('warehouse','inactive')")
        CONN.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('armor_enabled','0')")
        CONN.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('warehouse_enabled','0')")
        CONN.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('armor_index','0')")
        CONN.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('warehouse_index','0')")
        CONN.commit()


def get_setting(key, default=""):
    with DB_LOCK:
        row = CONN.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with DB_LOCK:
        CONN.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        CONN.commit()


def chat_peer():
    try:
        return int(get_setting("chat_peer_id", "0") or 0)
    except Exception:
        return 0


def get_extra_admins():
    try:
        return [int(x) for x in get_setting("extra_admins", "").split(",") if x.strip().isdigit()]
    except Exception:
        return []


def set_extra_admins(lst):
    set_setting("extra_admins", ",".join(str(x) for x in lst))


def chat_owner(peer):
    if VK is None:
        return 0
    if peer in OWNER_CACHE:
        return OWNER_CACHE[peer]
    oid = 0
    try:
        r = VK.messages.getConversationsById(peer_ids=peer)
        items = r.get("items", [])
        if items:
            cs = items[0].get("chat_settings") or {}
            oid = int(cs.get("owner_id", 0) or 0)
    except Exception as e:
        print("owner error:", e)
    OWNER_CACHE[peer] = oid
    return oid


def is_admin_user(sender, peer):
    if sender <= 0:
        return False
    if sender == OWNER_ID:
        return True
    if sender in ADMINS:
        return True
    if sender in get_extra_admins():
        return True
    if chat_owner(peer) == sender:
        return True
    return False


def is_owner_user(sender, peer):
    if sender <= 0:
        return False
    if sender == OWNER_ID:
        return True
    return chat_owner(peer) == sender


def get_fullers(t):
    with DB_LOCK:
        rows = CONN.execute("SELECT user_id FROM fullers WHERE type=? ORDER BY id", (t,)).fetchall()
        return [r["user_id"] for r in rows]


def add_fuller(t, user_id):
    with DB_LOCK:
        CONN.execute("INSERT INTO fullers(type,user_id) VALUES(?,?)", (t, user_id))
        CONN.commit()


def remove_fuller(t, user_id):
    with DB_LOCK:
        CONN.execute("DELETE FROM fullers WHERE type=? AND user_id=?", (t, user_id))
        CONN.commit()


def get_estafeta(t):
    with DB_LOCK:
        row = CONN.execute("SELECT * FROM estafeta WHERE type=?", (t,)).fetchone()
        return dict(row) if row else None


def update_estafeta(t, **kwargs):
    with DB_LOCK:
        fields = ", ".join(f"{k}=?" for k in kwargs.keys())
        values = list(kwargs.values()) + [t]
        CONN.execute(f"UPDATE estafeta SET {fields} WHERE type=?", values)
        CONN.commit()


def open_log(user_id, t):
    with DB_LOCK:
        CONN.execute("INSERT INTO log(user_id, type, taken_at, released_at, reason) VALUES(?,?,?,NULL,'')",
                     (user_id, t, int(time.time())))
        CONN.commit()


def close_log(t, reason):
    with DB_LOCK:
        CONN.execute("UPDATE log SET released_at=?, reason=? WHERE type=? AND released_at IS NULL",
                     (int(time.time()), reason, t))
        CONN.commit()


def get_user_log(user_id):
    with DB_LOCK:
        rows = [dict(r) for r in CONN.execute(
            "SELECT * FROM log WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,)).fetchall()]
    if not rows:
        return f"{mention(user_id)} — лог пуст."
    lines = [f"Лог {mention(user_id)} (последние 20):"]
    for r in rows:
        what = "броня" if r["type"] == "armor" else "склад"
        if r["released_at"]:
            mins = int((r["released_at"] - r["taken_at"]) // 60)
            lines.append(f"• {what}, держал {mins} мин. ({r['reason']})")
        else:
            mins = int((time.time() - r["taken_at"]) // 60)
            lines.append(f"• {what}, держит сейчас {mins} мин.")
    return "\n".join(lines)


def clear_log_all():
    with DB_LOCK:
        CONN.execute("DELETE FROM log")
        CONN.commit()


def change_penalty(user_id, delta):
    with DB_LOCK:
        row = CONN.execute("SELECT cnt FROM penalties WHERE user_id=?", (user_id,)).fetchone()
        cur = row["cnt"] if row else 0
        new = max(0, cur + delta)
        if row:
            CONN.execute("UPDATE penalties SET cnt=? WHERE user_id=?", (new, user_id))
        else:
            CONN.execute("INSERT INTO penalties(user_id,cnt) VALUES(?,?)", (user_id, new))
        CONN.commit()
        return new


def clear_penalties_all():
    with DB_LOCK:
        CONN.execute("DELETE FROM penalties")
        CONN.commit()


def clear_penalty_user(user_id):
    with DB_LOCK:
        CONN.execute("DELETE FROM penalties WHERE user_id=?", (user_id,))
        CONN.commit()


def all_penalties():
    with DB_LOCK:
        return [dict(r) for r in CONN.execute(
            "SELECT user_id, cnt FROM penalties WHERE cnt>0 ORDER BY cnt DESC, user_id ASC").fetchall()]


def get_penalty(user_id):
    with DB_LOCK:
        row = CONN.execute("SELECT cnt FROM penalties WHERE user_id=?", (user_id,)).fetchone()
        return row["cnt"] if row else 0


def send(peer, text):
    if VK is None or not peer:
        return
    try:
        VK.messages.send(peer_id=peer, message=text, random_id=random.getrandbits(31))
    except Exception as e:
        print("send error:", e)


def mention(user_id):
    name = NAME_CACHE.get(user_id)
    if not name:
        name = "игрок"
        try:
            r = VK.users.get(user_ids=user_id)
            if r:
                name = (r[0].get("first_name", "") + " " + r[0].get("last_name", "")).strip() or "игрок"
        except Exception:
            pass
        NAME_CACHE[user_id] = name
    return f"[id{user_id}|{name}]"


def norm(s):
    s = s.strip().lower()
    s = s.rstrip(".,;:!?")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def extract_targets(text, reply_from):
    ids = []
    for m in re.finditer(r"\[id(\d+)\|", text, re.I):
        ids.append(int(m.group(1)))
    for m in re.finditer(r"[@\*]id(\d+)", text, re.I):
        ids.append(int(m.group(1)))
    for m in re.finditer(r"\b(\d{5,})\b", text):
        ids.append(int(m.group(1)))
    seen = set()
    result = []
    for v in ids:
        if v not in seen:
            seen.add(v)
            result.append(v)
    if not result and reply_from and reply_from > 0:
        result = [reply_from]
    return result


def next_fuller(t):
    fullers = get_fullers(t)
    if not fullers:
        return None
    idx_key = f"{t}_index"
    idx = int(get_setting(idx_key, "0"))
    if idx >= len(fullers):
        idx = 0
    user_id = fullers[idx]
    set_setting(idx_key, str(idx + 1))
    return user_id


def start_estafeta(t, peer):
    fullers = get_fullers(t)
    if not fullers:
        send(peer, f"⚠️ Нет фуллеров для {'брони' if t == 'armor' else 'склада'}. Добавьте через +фуллер.")
        return
    
    user_id = next_fuller(t)
    if not user_id:
        return
    
    mins = ARMOR_TIME // 60 if t == "armor" else WAREHOUSE_TIME // 60
    what = "броню" if t == "armor" else "склад"
    
    update_estafeta(t, status="held", current_holder=user_id, started_at=int(time.time()), waiting_until=0, pending_confirm=0)
    open_log(user_id, t)
    
    text = (f"{mention(user_id)}, твоя очередь фуллить {what}! 🎯\n"
            f"У тебя есть {mins} минут🕰️ чтобы успеть зафуллить.\n"
            f"Когда закончишь напиши !зафуллил. "
            f"Если желаешь отказаться напиши !пропускаю (+штраф).\n"
            f"Если {what[:-1]} полная напиши !полный и бот перейдет в режим ожидания "
            f"(за ложное использование штраф 300к❗).")
    send(peer, text)


def finish_estafeta(t, peer, reason="success"):
    e = get_estafeta(t)
    if not e or e["status"] != "held":
        return
    
    holder = e["current_holder"]
    close_log(t, reason)
    
    if reason == "timeout":
        send(peer, f"❌{mention(holder)} не зафуллил за отведенное время. Ему выдается штраф.❌")
        change_penalty(holder, 1)
    elif reason == "skip":
        send(peer, f"{mention(holder)} пропустил эстафету 😾 и получил +1 штраф❗")
        change_penalty(holder, 1)
    elif reason == "success":
        send(peer, f"{mention(holder)} Молодец🙂")
    elif reason == "false_alarm":
        send(peer, f"{mention(holder)} сказал что фулл полный, но это оказалось ложью. +1 штраф❗")
        change_penalty(holder, 1)
    
    update_estafeta(t, status="inactive", current_holder=None, started_at=None, waiting_until=0, pending_confirm=0)
    
    time.sleep(2)
    
    enabled = get_setting(f"{t}_enabled", "0") == "1"
    if enabled:
        start_estafeta(t, peer)


def enter_waiting(t, peer):
    wait_mins = ARMOR_WAIT // 60 if t == "armor" else WAREHOUSE_WAIT // 60
    e = get_estafeta(t)
    
    if e and e["status"] == "held" and e["current_holder"]:
        holder = e["current_holder"]
        elapsed = time.time() - e["started_at"]
        if elapsed < FALSE_ALARM_TIME:
            update_estafeta(t, pending_confirm=holder)
            send(peer, f"Вы точно зафуллили? Напишите !Да или !Нет")
            return
    
    close_log(t, "режим ожидания")
    wait_until = int(time.time()) + (ARMOR_WAIT if t == "armor" else WAREHOUSE_WAIT)
    update_estafeta(t, status="waiting", current_holder=None, started_at=None, waiting_until=wait_until, pending_confirm=0)
    send(peer, f"Принял! Отдыхаю {wait_mins} минут😴")


def confirm_waiting(t, peer, confirmed):
    e = get_estafeta(t)
    if not e or e["pending_confirm"] == 0:
        return
    
    holder = e["pending_confirm"]
    close_log(t, "режим ожидания")
    
    if not confirmed:
        change_penalty(holder, 1)
        send(peer, f"{mention(holder)} передумал и получил +1 штраф❗")
    
    wait_mins = ARMOR_WAIT // 60 if t == "armor" else WAREHOUSE_WAIT // 60
    wait_until = int(time.time()) + (ARMOR_WAIT if t == "armor" else WAREHOUSE_WAIT)
    update_estafeta(t, status="waiting", current_holder=None, started_at=None, waiting_until=wait_until, pending_confirm=0)
    send(peer, f"Принял! Отдыхаю {wait_mins} минут😴")


def fullers_text():
    armor = get_fullers("armor")
    warehouse = get_fullers("warehouse")
    
    lines = ["📋 Списки фуллеров:\n"]
    lines.append("🛡️ Фуллеры брони:")
    if armor:
        lines.append(", ".join(mention(u) for u in armor))
    else:
        lines.append("(пусто)")
    
    lines.append("\n📦 Фуллеры склада:")
    if warehouse:
        lines.append(", ".join(mention(u) for u in warehouse))
    else:
        lines.append("(пусто)")
    
    return "\n".join(lines)


def admins_text(peer):
    lines = ["👥 Администраторы:"]
    lines.append("👑 Создатель: " + mention(OWNER_ID))
    owner = chat_owner(peer)
    if owner and owner != OWNER_ID:
        lines.append("👑 Владелец чата: " + mention(owner))
    others = []
    for a in ADMINS:
        if a != OWNER_ID and a != owner and a not in others:
            others.append(a)
    for a in get_extra_admins():
        if a != OWNER_ID and a != owner and a not in others:
            others.append(a)
    if others:
        lines.append("🛡 Админы: " + ", ".join(mention(a) for a in others))
    return "\n".join(lines)


def status_text():
    out = []
    for t, label in (("armor", "броне"), ("warehouse", "складу")):
        e = get_estafeta(t)
        enabled = get_setting(f"{t}_enabled", "0") == "1"
        
        if not enabled:
            out.append(f"Эстафета по {label} выключена.")
            continue
        
        if e["status"] == "inactive":
            out.append(f"Эстафета по {label} не активна.")
        elif e["status"] == "waiting":
            remain = int((e["waiting_until"] - time.time()) // 60)
            if remain > 0:
                out.append(f"Эстафета по {label} в режиме ожидания, осталось {remain} мин.")
            else:
                out.append(f"Эстафета по {label} скоро возобновится.")
        elif e["status"] == "held":
            elapsed = int((time.time() - e["started_at"]) // 60)
            limit = ARMOR_TIME // 60 if t == "armor" else WAREHOUSE_TIME // 60
            remain = limit - elapsed
            m = mention(e["current_holder"])
            if remain > 0:
                out.append(f"Эстафета по {label} у {m}, прошло {elapsed} мин., осталось {remain} мин.")
            else:
                out.append(f"Эстафета по {label} у {m}, время вышло ({elapsed} мин.).")
    
    return "\n".join(out)


def penalties_text(target=None):
    if target:
        return f"{mention(target)} — штрафов: {get_penalty(target)}."
    rows = all_penalties()
    if not rows:
        return "Штрафов нет."
    lines = []
    for i, r in enumerate(rows):
        lines.append(f"{i + 1}. {mention(r['user_id'])} — {r['cnt']}")
    return "Штрафы:\n" + "\n".join(lines)


def help_text(admin, owner):
    t = ("📋 **Команды бота:**\n"
         "!статус — текущий статус эстафет 📊\n"
         "!штрафы — список штрафов 💰\n"
         "!фуллеры — списки фуллеров 👥\n"
         "!админы — кто является администратором 👑\n"
         "!помощь — список команд ℹ️\n\n"
         "🎮 **Команды эстафеты:**\n"
         "!зафуллил — передать эстафету следующему ✅\n"
         "!пропускаю — пропустить эстафету (+штраф) ⏭️\n"
         "!полный — перейти в режим ожидания 😴")
    
    if admin:
        t += ("\n\n🛠 **Админ-команды:**\n"
              "!стоп броня — выключить эстафету брони 🛑\n"
              "!старт броня — включить эстафету брони ▶️\n"
              "!стоп склад — выключить эстафету склада 🛑\n"
              "!старт склад — включить эстафету склада ▶️\n"
              "!лог @ — лог конкретного человека 📝\n"
              "!очистить штрафы @ или всем — очистить штрафы 🧹\n"
              "!штраф всем — штраф всем фуллерам ⚡\n"
              "+штраф @ — добавить штраф (можно несколько) ➕\n"
              "-штраф @ — снять штраф (можно несколько) ➖\n"
              "!ложный @ — 10 штрафов сразу 🔥\n"
              "!освободить броню/склад — продолжить или выйти из ожидания ⏩\n"
              "!очистить лог — очистить весь лог 🗑️\n"
              "+фуллер б/с @ — добавить фуллера ➕\n"
              "-фуллер б/с @ — удалить фуллера ➖")
    
    if owner:
        t += ("\n\n👑 **Команды владельца:**\n"
              "!назначить @ — выдать права админа\n"
              "!снять @ — снять права админа\n"
              "!привязать — привязать бота к чату\n"
              "!сбросить привязку — сбросить привязку")
    
    return t


def timer_loop(peer):
    while True:
        try:
            if peer and VK is not None:
                now = time.time()
                
                for t in ("armor", "warehouse"):
                    enabled = get_setting(f"{t}_enabled", "0") == "1"
                    if not enabled:
                        continue
                    
                    e = get_estafeta(t)
                    if not e:
                        continue
                    
                    if e["status"] == "held":
                        limit = ARMOR_TIME if t == "armor" else WAREHOUSE_TIME
                        if (now - e["started_at"]) >= limit:
                            finish_estafeta(t, peer, "timeout")
                    
                    elif e["status"] == "waiting":
                        if now >= e["waiting_until"]:
                            update_estafeta(t, status="inactive", waiting_until=0)
                            start_estafeta(t, peer)
        
        except Exception as e:
            print("timer error:", e)
        time.sleep(15)


def handle_message(peer, sender, text, reply_from):
    bound = chat_peer()
    first = norm(text.split("\n")[0])
    if not first:
        return
    
    if first == "фон":
        send(peer, "Тан⛲")
        return
    
    cmd = first.split(" ", 1)[0]

    if first in ("!сбросить привязку", "!сбросить_привязку") and is_admin_user(sender, peer):
        set_setting("chat_peer_id", "0")
        send(peer, "Привязка сброшена. Теперь напишите !привязать в нужном чате.")
        return

    if not bound:
        if is_admin_user(sender, peer) and cmd in ("!привязать",):
            set_setting("chat_peer_id", str(peer))
            send(peer, "Чат привязан. Теперь бот работает в этом чате.")
        return

    if peer != bound:
        return

    admin = is_admin_user(sender, peer)
    owner = is_owner_user(sender, peer)

    def deny():
        send(peer, "Это команда для администраторов.")

    def deny_owner():
        send(peer, "Это команда для создателя бота или владельца чата.")

    if cmd == "!помощь":
        send(peer, help_text(admin, owner)); return

    if cmd == "!админы":
        send(peer, admins_text(peer)); return

    if cmd == "!фуллеры":
        send(peer, fullers_text()); return

    if cmd == "!статус":
        send(peer, status_text()); return

    if cmd in ("!штрафы", "!штраф"):
        targets = extract_targets(text, reply_from)
        send(peer, penalties_text(targets[0] if targets else None)); return

    if cmd in ("!зафуллил", "!зафулил"):
        for t in ("armor", "warehouse"):
            e = get_estafeta(t)
            if e and e["status"] == "held" and e["current_holder"] == sender:
                finish_estafeta(t, peer, "success")
                return
        send(peer, "Сейчас нет активной эстафеты на вас.")
        return

    if cmd == "!пропускаю":
        for t in ("armor", "warehouse"):
            e = get_estafeta(t)
            if e and e["status"] == "held" and e["current_holder"] == sender:
                finish_estafeta(t, peer, "skip")
                return
        send(peer, "Сейчас нет активной эстафеты на вас.")
        return

    if cmd == "!полный":
        for t in ("armor", "warehouse"):
            e = get_estafeta(t)
            if e and e["status"] == "held" and e["current_holder"] == sender:
                enter_waiting(t, peer)
                return
        send(peer, "Сейчас нет активной эстафеты на вас.")
        return

    if first == "!да":
        for t in ("armor", "warehouse"):
            e = get_estafeta(t)
            if e and e["pending_confirm"] == sender:
                confirm_waiting(t, peer, True)
                return
        return

    if first == "!нет":
        for t in ("armor", "warehouse"):
            e = get_estafeta(t)
            if e and e["pending_confirm"] == sender:
                confirm_waiting(t, peer, False)
                return
        return

    if cmd == "!привязать":
        if not owner: return deny_owner()
        set_setting("chat_peer_id", str(peer))
        send(peer, "Чат перепривязан."); return

    if cmd == "!стоп":
        if not admin: return deny()
        if "броня" in first or "броню" in first:
            set_setting("armor_enabled", "0")
            e = get_estafeta("armor")
            if e and e["status"] == "held":
                finish_estafeta("armor", peer, "admin_stop")
            else:
                update_estafeta("armor", status="inactive")
            send(peer, "Эстафета брони выключена.")
        elif "склад" in first:
            set_setting("warehouse_enabled", "0")
            e = get_estafeta("warehouse")
            if e and e["status"] == "held":
                finish_estafeta("warehouse", peer, "admin_stop")
            else:
                update_estafeta("warehouse", status="inactive")
            send(peer, "Эстафета склада выключена.")
        return

    if cmd == "!старт":
        if not admin: return deny()
        if "броня" in first or "броню" in first:
            set_setting("armor_enabled", "1")
            send(peer, "Эстафета брони включена.")
            start_estafeta("armor", peer)
        elif "склад" in first:
            set_setting("warehouse_enabled", "1")
            send(peer, "Эстафета склада включена.")
            start_estafeta("warehouse", peer)
        return

    if cmd == "!лог":
        if not admin: return deny()
        targets = extract_targets(text, reply_from)
        if not targets:
            send(peer, "Укажите игрока: !лог @юзер")
            return
        send(peer, get_user_log(targets[0]))
        return

    if first.startswith("!очистить штрафы") or first.startswith("!очистить_штрафы"):
        if not admin: return deny()
        if "всем" in first or "всём" in first:
            clear_penalties_all()
            send(peer, "Все штрафы очищены.")
        else:
            targets = extract_targets(text, reply_from)
            if targets:
                for t in targets:
                    clear_penalty_user(t)
                send(peer, "Штрафы очищены у указанных игроков.")
            else:
                clear_penalties_all()
                send(peer, "Все штрафы очищены.")
        return

    if first.startswith("!штраф всем") or first.startswith("!штраф_всем"):
        if not admin: return deny()
        fullers = get_fullers("armor") + get_fullers("warehouse")
        if not fullers:
            send(peer, "Нет фуллеров для выдачи штрафов.")
            return
        lines = []
        for uid in set(fullers):
            cnt = change_penalty(uid, 1)
            lines.append(f"{mention(uid)} — всего: {cnt}")
        send(peer, "Штраф +1 выдан всем фуллерам:\n" + "\n".join(lines))
        return

    if first.startswith("+штраф"):
        if not admin: return deny()
        targets = extract_targets(text, reply_from)
        if not targets:
            send(peer, "Укажите игроков.")
            return
        lines = []
        for t in targets:
            cnt = change_penalty(t, 1)
            lines.append(f"{mention(t)} — всего: {cnt}")
        send(peer, "Штраф +1 выдан:\n" + "\n".join(lines))
        return

    if first.startswith("-штраф"):
        if not admin: return deny()
        targets = extract_targets(text, reply_from)
        if not targets:
            send(peer, "Укажите игроков.")
            return
        lines = []
        for t in targets:
            cnt = change_penalty(t, -1)
            lines.append(f"{mention(t)} — всего: {cnt}")
        send(peer, "Штраф -1 снят:\n" + "\n".join(lines))
        return

    if first.startswith("!ложный"):
        if not admin: return deny()
        targets = extract_targets(text, reply_from)
        if not targets:
            send(peer, "Укажите игрока.")
            return
        for t in targets:
            change_penalty(t, 10)
        send(peer, f"Выдано по 10 штрафов: {', '.join(mention(t) for t in targets)}")
        return

    if first.startswith("!освободить"):
        if not admin: return deny()
        if "броня" in first or "броню" in first:
            e = get_estafeta("armor")
            if e and e["status"] == "held":
                finish_estafeta("armor", peer, "admin_free")
            elif e and e["status"] == "waiting":
                update_estafeta("armor", status="inactive", waiting_until=0)
                start_estafeta("armor", peer)
            else:
                start_estafeta("armor", peer)
            send(peer, "Эстафета брони продолжена.")
        elif "склад" in first:
            e = get_estafeta("warehouse")
            if e and e["status"] == "held":
                finish_estafeta("warehouse", peer, "admin_free")
            elif e and e["status"] == "waiting":
                update_estafeta("warehouse", status="inactive", waiting_until=0)
                start_estafeta("warehouse", peer)
            else:
                start_estafeta("warehouse", peer)
            send(peer, "Эстафета склада продолжена.")
        return

    if first.startswith("!очистить лог") or first.startswith("!очистить_лог"):
        if not admin: return deny()
        clear_log_all()
        send(peer, "Лог очищен.")
        return

    if first.startswith("+фуллер"):
        if not admin: return deny()
        targets = extract_targets(text, reply_from)
        if not targets:
            send(peer, "Укажите игрока.")
            return
        t = "armor" if "б" in first else "warehouse"
        existing = get_fullers(t)
        added = [u for u in targets if u not in existing]
        for u in added:
            add_fuller(t, u)
        if added:
            what = "брони" if t == "armor" else "склада"
            send(peer, f"Добавлены фуллеры {what}: {', '.join(mention(u) for u in added)}")
        else:
            send(peer, "Эти игроки уже в списке.")
        return

    if first.startswith("-фуллер"):
        if not admin: return deny()
        targets = extract_targets(text, reply_from)
        if not targets:
            send(peer, "Укажите игрока.")
            return
        t = "armor" if "б" in first else "warehouse"
        existing = get_fullers(t)
        removed = [u for u in targets if u in existing]
        for u in removed:
            remove_fuller(t, u)
        if removed:
            what = "брони" if t == "armor" else "склада"
            send(peer, f"Удалены фуллеры {what}: {', '.join(mention(u) for u in removed)}")
        else:
            send(peer, "Этих игроков нет в списке.")
        return

    if first.startswith("!назначить"):
        if not owner: return deny_owner()
        targets = extract_targets(text, reply_from)
        if not targets:
            send(peer, "Укажите игрока.")
            return
        extra = get_extra_admins()
        added = [t for t in targets if t != OWNER_ID and t not in ADMINS and t != chat_owner(peer) and t not in extra]
        for t in added:
            extra.append(t)
        set_extra_admins(extra)
        if added:
            send(peer, "Назначены админами: " + ", ".join(mention(x) for x in added))
        else:
            send(peer, "Эти игроки уже администраторы.")
        return

    if first.startswith("!снять"):
        if not owner: return deny_owner()
        targets = extract_targets(text, reply_from)
        if not targets:
            send(peer, "Укажите игрока.")
            return
        extra = get_extra_admins()
        removed = []
        protected = 0
        for t in targets:
            if t == OWNER_ID or t in ADMINS or t == chat_owner(peer):
                protected += 1
                continue
            if t in extra:
                extra.remove(t)
                removed.append(t)
        set_extra_admins(extra)
        parts = []
        if removed:
            parts.append("Сняты права: " + ", ".join(mention(x) for x in removed))
        if protected:
            parts.append("Нельзя снять права с создателя, владельца или базовых админов.")
        if not parts:
            parts.append("У этих игроков нет прав.")
        send(peer, "\n".join(parts))
        return

    if cmd.startswith("!"):
        send(peer, "Неизвестная команда. Список команд: !помощь")


def main():
    global VK
    print("=== Bot starting ===")
    init_db()
    
    if not VK_TOKEN:
        print("ERROR: не задана переменная окружения VK_TOKEN!")
    while not VK_TOKEN:
        time.sleep(60)
    
    while True:
        try:
            session = vk_api.VkApi(token=VK_TOKEN)
            VK = session.get_api()
            group_id = VK.groups.getById()[0]["id"]
            longpoll = VkBotLongPoll(session, group_id)
            print("Bot started, group id:", group_id)
            
            peer = chat_peer()
            if peer:
                threading.Thread(target=timer_loop, args=(peer,), daemon=True).start()
            
            for event in longpoll.listen():
                if event.type != VkBotEventType.MESSAGE_NEW:
                    continue
                try:
                    obj = event.obj
                    msg = obj.get("message", obj) if isinstance(obj, dict) else {}
                    peer = int(msg.get("peer_id", 0) or 0)
                    sender = int(msg.get("from_id", 0) or 0)
                    txt = (msg.get("text") or "").strip()
                    reply = msg.get("reply_message") or {}
                    reply_from = int(reply.get("from_id", 0) or 0) if isinstance(reply, dict) else 0
                    if peer > 0 and sender > 0 and txt:
                        handle_message(peer, sender, txt, reply_from)
                except Exception as e:
                    print("message error:", e)
        except Exception as e:
            print("longpoll error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
