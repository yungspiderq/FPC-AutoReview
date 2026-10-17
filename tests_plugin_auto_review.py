"""
Функциональные тесты плагина auto_review.py на моках.
Запуск: cd FPC && python3 tests_plugin_auto_review.py
"""
import importlib.util
import json
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, ".")
sys.path.append("plugins")

spec = importlib.util.spec_from_file_location("plugins.auto_review", "plugins/auto_review.py")
plugin = importlib.util.module_from_spec(spec)
sys.modules["plugins.auto_review"] = plugin
spec.loader.exec_module(plugin)

plugin._review_loop = lambda crd: None  # поток тестируем через _review_tick

PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("✓ " if cond else "✗ FAIL: ") + name)


def reset_settings(**kwargs):
    os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
    base = json.loads(json.dumps(plugin.DEFAULT_SETTINGS))
    base.update(kwargs)
    with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False)


def mk_event(order_id="AAAA1111", delivered=True, chat_id=5555, buyer="buyer1",
             buyer_id=777, desc="Аккаунт Steam"):
    order = SimpleNamespace(id=order_id, chat_id=chat_id, buyer_username=buyer,
                            buyer_id=buyer_id, description=desc)
    return SimpleNamespace(delivered=delivered, order=order)


# ───────────────────────────── моки ─────────────────────────────
class MockAccount:
    def __init__(self, fail=False):
        self.is_initiated = True
        self.id = 999
        self.reviews = []          # [(order_id, text, rating)]
        self.fail = fail

    def send_review(self, order_id, text, rating=5):
        if self.fail:
            raise Exception("mock: send_review failed")
        self.reviews.append((str(order_id), text, rating))
        return "<html>ok</html>"

    def send_message(self, chat_id, text=None, **kw):
        return SimpleNamespace(id=1)


class MockBot:
    def __init__(self):
        self.answers, self.edits, self.sent, self.replies = [], [], [], []

    def answer_callback_query(self, cb_id, text=None, show_alert=False):
        self.answers.append((cb_id, text, show_alert))

    def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((text, reply_markup))
        return SimpleNamespace(id=message_id)

    def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent) + 100, chat=SimpleNamespace(id=chat_id))

    def reply_to(self, m, text, parse_mode=None):
        self.replies.append(text)

    def delete_message(self, chat_id, message_id):
        pass


def kb_buttons(kb) -> str:
    if kb is None:
        return ""
    try:
        return " | ".join(b.text for row in kb.keyboard for b in row if b.text)
    except Exception:
        return str(kb)


class MockTelegram:
    def __init__(self):
        self.bot = MockBot()
        self.cbq_handlers, self.msg_handlers, self.user_states = [], [], {}
        self.notifications = []

    def cbq_handler(self, handler, func, **kwargs):
        self.cbq_handlers.append((handler, func))

    def msg_handler(self, handler, **kwargs):
        self.msg_handlers.append((handler, kwargs))

    def set_state(self, chat_id, message_id, user_id, state, data=None):
        self.user_states[(chat_id, user_id)] = {"state": state, "mid": message_id, "data": data or {}}

    def get_state(self, chat_id, user_id):
        return self.user_states.get((chat_id, user_id))

    def check_state(self, chat_id, user_id, state):
        return self.user_states.get((chat_id, user_id), {}).get("state") == state

    def clear_state(self, chat_id, user_id, del_msg=False):
        st = self.user_states.pop((chat_id, user_id), None)
        return st.get("mid") if st else None

    def send_notification(self, text, **kwargs):
        self.notifications.append(text)


class MockCardinal:
    def __init__(self, account, telegram):
        self.account = account
        self.telegram = telegram
        self.plugins = {plugin.UUID: SimpleNamespace(enabled=True)}
        self.registered_commands = None

    def add_telegram_commands(self, uuid, commands):
        self.registered_commands = commands


def make_crd(with_tg=True, fail_send=False):
    acc = MockAccount(fail=fail_send)
    tg = MockTelegram() if with_tg else None
    return MockCardinal(acc, tg), acc, tg


# ═══════════════════════ ТЕСТ 1: метаданные ═══════════════════════
from uuid import UUID as _UUID
check("1.1 метаданные плагина", all(hasattr(plugin, f) for f in
      ["NAME", "VERSION", "DESCRIPTION", "CREDITS", "SETTINGS_PAGE", "UUID", "BIND_TO_DELETE"]))
check("1.2 UUID валиден", str(_UUID(plugin.UUID, version=4)) == plugin.UUID)
check("1.3 автор yungspider, название на английском",
      plugin.CREDITS == "yungspider" and plugin.NAME.isascii())
check("1.4 BIND_TO_POST_DELIVERY привязан",
      plugin.BIND_TO_POST_DELIVERY == [plugin.post_delivery_handler])

# ═══════════════════════ ТЕСТ 2: шаблон и чёрный список ═══════════════════════
txt = plugin._fill_template("Спасибо $username за «$lot», заказ #$order", "ivan", "Акк Steam", "AB12")
check("2.1 подстановка переменных", txt == "Спасибо ivan за «Акк Steam», заказ #AB12")
check("2.2 пустой ник → «друг»", "друг" in plugin._fill_template("$username", "", "", ""))

s = {"blacklist": ["vasya", "777"]}
check("2.3 чёрный список по нику", plugin._is_blacklisted(s, "Vasya", 1) is True)
check("2.4 чёрный список по id", plugin._is_blacklisted(s, "other", 777) is True)
check("2.5 не в чёрном списке", plugin._is_blacklisted(s, "petya", 1) is False)
check("2.6 пустой чёрный список", plugin._is_blacklisted({"blacklist": []}, "x", 1) is False)

# ═══════════════════════ ТЕСТ 3: очередь (post_delivery_handler) ═══════════════════════
crd, acc, tg = make_crd(with_tg=False)
reset_settings(delay_minutes=30)
plugin.post_delivery_handler(crd, mk_event())
pending = plugin._load_settings()["pending"]
check("3.1 успешная выдача → заказ в очереди", "AAAA1111" in pending
      and pending["AAAA1111"]["username"] == "buyer1" and pending["AAAA1111"]["buyer_id"] == 777)

plugin.post_delivery_handler(crd, mk_event())  # дубль
check("3.2 повторная выдача того же заказа не дублирует запись",
      len(plugin._load_settings()["pending"]) == 1)

plugin.post_delivery_handler(crd, mk_event(order_id="BB22", delivered=False))
check("3.3 выдача не состоялась → в очередь не ставим",
      "BB22" not in plugin._load_settings()["pending"])

reset_settings(enabled=False)
plugin.post_delivery_handler(crd, mk_event(order_id="CC33"))
check("3.4 enabled=False → очередь не пополняется",
      "CC33" not in plugin._load_settings()["pending"])

reset_settings()
crd.plugins[plugin.UUID].enabled = False
plugin.post_delivery_handler(crd, mk_event(order_id="DD44"))
check("3.5 выключенный плагин не ставит в очередь",
      "DD44" not in plugin._load_settings()["pending"])
crd.plugins[plugin.UUID].enabled = True

plugin.post_delivery_handler(crd, SimpleNamespace(delivered=True))  # без order
check("3.6 событие без заказа не ломает плагин", True)

# чёрный список — не ставим в очередь
reset_settings(blacklist=["buyer1"])
plugin.post_delivery_handler(crd, mk_event(order_id="EE55", buyer="buyer1"))
check("3.7 покупатель из чёрного списка не ставится в очередь",
      "EE55" not in plugin._load_settings()["pending"])

# уже оставлен отзыв — не дублируем
reset_settings(reviewed={"FF66": {"ts": time.time(), "username": "x", "rating": 5}})
plugin.post_delivery_handler(crd, mk_event(order_id="FF66"))
check("3.8 заказ с уже оставленным отзывом не ставится в очередь",
      "FF66" not in plugin._load_settings()["pending"])

# ═══════════════════════ ТЕСТ 4: отправка отзывов (_review_tick) ═══════════════════════
crd, acc, tg = make_crd(with_tg=False)
now = time.time()
reset_settings(delay_minutes=30, rating=5,
               template="Спасибо $username за «$lot», заказ #$order!", pending={
    "ORD1": {"ts": now - 31 * 60, "username": "vasya", "lot": "Акк", "buyer_id": 1, "tries": 0},
    "ORD2": {"ts": now - 10 * 60, "username": "petya", "lot": "Ключ", "buyer_id": 2, "tries": 0},
})
done = plugin._review_tick(crd, now_ts=now)
check("4.1 созрел только отзыв старше задержки", done == ["ORD1"])
check("4.2 отзыв отправлен с оценкой и подстановкой",
      acc.reviews and acc.reviews[0][0] == "ORD1" and acc.reviews[0][2] == 5
      and "vasya" in acc.reviews[0][1] and "ORD1" in acc.reviews[0][1])
st = plugin._load_settings()
check("4.3 отправленное перенесено в историю, неотправленное осталось в очереди",
      "ORD1" not in st["pending"] and "ORD1" in st["reviewed"] and "ORD2" in st["pending"])

# оценка из настроек применяется
acc.reviews.clear()
reset_settings(delay_minutes=1, rating=4, pending={
    "ORD3": {"ts": now - 120, "username": "x", "lot": "y", "buyer_id": 3, "tries": 0}})
plugin._review_tick(crd, now_ts=now)
check("4.4 оценка из настроек (4) передана в send_review",
      acc.reviews and acc.reviews[0][2] == 4)

# ошибки отправки → ретраи, после MAX_TRIES — отбрасывание
crd2, acc2, _ = make_crd(with_tg=False, fail_send=True)
reset_settings(delay_minutes=5, pending={
    "ORD9": {"ts": now - 600, "username": "x", "lot": "y", "buyer_id": 9, "tries": 0}})
for i in range(plugin.MAX_TRIES):
    plugin._review_tick(crd2, now_ts=now + i)
check("4.5 после MAX_TRIES ошибок запись отброшена, отзыв не отправлен",
      "ORD9" not in plugin._load_settings()["pending"] and acc2.reviews == [])

# протухшие записи (>7 дней) удаляются БЕЗ отправки
acc.reviews.clear()
reset_settings(delay_minutes=30, pending={
    "OLD1": {"ts": now - plugin.PRUNE_AFTER - 100, "username": "x", "lot": "y",
             "buyer_id": 1, "tries": 0}})
plugin._review_tick(crd, now_ts=now)
check("4.6 протухшая запись (>7 дней) удаляется без отправки",
      "OLD1" not in plugin._load_settings()["pending"] and acc.reviews == [])

# чёрный список, добавленный ПОСЛЕ постановки в очередь, отменяет отзыв
acc.reviews.clear()
reset_settings(delay_minutes=1, blacklist=["zloy"], pending={
    "ORD7": {"ts": now - 120, "username": "zloy", "lot": "y", "buyer_id": 7, "tries": 0}})
plugin._review_tick(crd, now_ts=now)
check("4.7 покупатель, добавленный в ЧС позже, отзыв не получает",
      "ORD7" not in plugin._load_settings()["pending"] and acc.reviews == [])

# ограничение истории
acc.reviews.clear()
big = {f"H{i}": {"ts": now + i, "username": "x", "lot": "y", "rating": 5}
       for i in range(plugin.HISTORY_LIMIT + 50)}
reset_settings(delay_minutes=1, rating=5, reviewed=big, pending={
    "NEW1": {"ts": now - 120, "username": "x", "lot": "y", "buyer_id": 1, "tries": 0}})
plugin._review_tick(crd, now_ts=now + 100000)
check("4.8 история отзывов ограничена HISTORY_LIMIT",
      len(plugin._load_settings()["reviewed"]) <= plugin.HISTORY_LIMIT
      and "NEW1" in plugin._load_settings()["reviewed"])

# ═══════════════════════ ТЕСТ 5: Telegram UI ═══════════════════════
crd, acc, tg = make_crd(with_tg=True)
plugin.init(crd)
bot = tg.bot
reset_settings()
check("5.1 команда /review зарегистрирована",
      any(kw.get("commands") == ["review"] for _, kw in tg.msg_handlers))
check("5.2 add_telegram_commands вызван", crd.registered_commands
      and crd.registered_commands[0][0] == "review")


def dispatch_cb(data, chat_id=111, msg_id=555, user_id=222):
    c = SimpleNamespace(data=data, id="cb1",
                        message=SimpleNamespace(chat=SimpleNamespace(id=chat_id), id=msg_id),
                        from_user=SimpleNamespace(id=user_id))
    for handler, func in tg.cbq_handlers:
        try:
            matched = func(c)
        except Exception:
            matched = False
        if matched:
            handler(c)
            return handler.__name__
    return None


def dispatch_msg(text, chat_id=111, user_id=222):
    m = SimpleNamespace(chat=SimpleNamespace(id=chat_id), from_user=SimpleNamespace(id=user_id),
                        text=text)
    for h, kw in tg.msg_handlers:
        f = kw.get("func")
        try:
            if f and f(m):
                h(m)
                return h.__name__
        except Exception:
            pass
    return None


def last_text():
    if bot.edits:
        return bot.edits[-1][0]
    return bot.sent[-1][1] if bot.sent else ""


def last_kb():
    return kb_buttons(bot.edits[-1][1]) if bot.edits else ""


name = dispatch_cb(f"47:{plugin.UUID}:0")
check("5.3 страница настроек открывает меню", name == "open_home" and "AutoReview" in last_text())
check("5.4 в меню статус, задержка, оценка", "Задержка после выдачи: <b>30</b>" in last_text()
      and "Оценка: <b>5</b>" in last_text())

name = dispatch_cb(plugin.CB_TOG)
check("5.5 вкл/выкл переключается", name == "toggle_enabled"
      and plugin._load_settings()["enabled"] is False)
dispatch_cb(plugin.CB_TOG)
check("5.6 и обратно", plugin._load_settings()["enabled"] is True)

dispatch_cb(plugin.CB_DELAY)
st = tg.user_states.get((111, 222), {})
check("5.7 кнопка задержки ставит состояние", st.get("state") == plugin.ST_DELAY)
dispatch_msg("45")
check("5.8 задержка сохранена (45 мин)", plugin._load_settings()["delay_minutes"] == 45)
dispatch_cb(plugin.CB_DELAY)
dispatch_msg("0")
check("5.9 задержка зажимается (0 → 1)", plugin._load_settings()["delay_minutes"] == 1)
dispatch_cb(plugin.CB_DELAY)
dispatch_msg("abc")
check("5.10 нечисловой ввод задержки отклоняется", plugin._load_settings()["delay_minutes"] == 1
      and "❌" in bot.replies[-1])

dispatch_cb(plugin.CB_RATING)
check("5.11 кнопка оценки ставит состояние",
      tg.user_states.get((111, 222), {}).get("state") == plugin.ST_RATING)
dispatch_msg("4")
check("5.12 оценка сохранена (4)", plugin._load_settings()["rating"] == 4)
dispatch_cb(plugin.CB_RATING)
dispatch_msg("9")
check("5.13 оценка зажимается (9 → 5)", plugin._load_settings()["rating"] == 5)
dispatch_cb(plugin.CB_RATING)
dispatch_msg("xyz")
check("5.14 нечисловая оценка отклоняется", plugin._load_settings()["rating"] == 5
      and "❌" in bot.replies[-1])

dispatch_cb(plugin.CB_TMPL)
dispatch_msg("Отличный покупатель $username! Заказ #$order. Лот «$lot».")
check("5.15 шаблон сохранен", plugin._load_settings()["template"] ==
      "Отличный покупатель $username! Заказ #$order. Лот «$lot».")
dispatch_cb(plugin.CB_TMPL)
dispatch_msg("   ")
check("5.16 пустой шаблон отклоняется", "$username" in plugin._load_settings()["template"])

dispatch_cb(plugin.CB_BL)
dispatch_msg("ZloyPirate")
check("5.17 ник добавлен в чёрный список (в нижнем регистре)",
      "zloypirate" in plugin._load_settings()["blacklist"])
dispatch_cb(plugin.CB_BL)
dispatch_msg("-zloypirate")
check("5.18 ник удалён из чёрного списка",
      "zloypirate" not in plugin._load_settings()["blacklist"])
dispatch_cb(plugin.CB_BL)
dispatch_msg("-nesuchestvuet")
check("5.19 удаление несуществующего не ломает", "❌" not in bot.replies[-1])

bot.sent.clear()
name = dispatch_cb(plugin.CB_TEST)
check("5.20 «Тест» присылает предпросмотр отзыва",
      name == "send_test" and any("Так будет выглядеть отзыв" in t for _, t in bot.sent)
      and any("Иван" in t for _, t in bot.sent))
dispatch_cb(plugin.CB_NOOP)
check("5.21 noop отвечает", True)

# ═══════════════════════ ТЕСТ 6: нормализация настроек ═══════════════════════
os.makedirs(plugin.PLUGIN_DIR, exist_ok=True)
with open(plugin.SETTINGS_PATH, "w", encoding="utf-8") as f:
    json.dump({"enabled": 1, "delay_minutes": "abc", "rating": "x", "template": "  ",
               "blacklist": "notalist", "pending": None, "reviewed": None}, f)
s = plugin._load_settings()
check("6.1 битые значения нормализуются", s["enabled"] is True and s["delay_minutes"] == 30
      and s["rating"] == 5 and s["template"] == plugin.DEFAULT_TEMPLATE
      and s["blacklist"] == [] and s["pending"] == {} and s["reviewed"] == {})
with open(plugin.SETTINGS_PATH, "w") as f:
    f.write("{broken")
s = plugin._load_settings()
check("6.2 битый JSON → дефолты", s["delay_minutes"] == 30 and s["rating"] == 5 and s["pending"] == {})
reset_settings()

print(f"\n{'=' * 60}\nПройдено: {len(PASS)}  Провалено: {len(FAIL)}")
if FAIL:
    print("Проваленные тесты:")
    for f_ in FAIL:
        print("  ✗", f_)
    sys.exit(1)
print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ ✅")
