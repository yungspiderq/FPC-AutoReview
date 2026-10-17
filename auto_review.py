"""
AutoReview — автоматические отзывы покупателям (плагин для FunPay Cardinal v0.1.17+)

🌟 После успешной выдачи товара автоматически оставляет покупателю отзыв с оценкой
через штатный метод FunPay `send_review`. Отзывы повышают доверие к магазину и
стимулируют покупателей отвечать взаимностью.

Возможности:
 • Авто-отзыв через настраиваемую задержку после выдачи (по умолчанию 30 минут) —
   когда покупатель успел проверить товар.
 • Оценка 1-5 (по умолчанию 5) и шаблон текста с переменными
   $username (ник), $lot (лот), $order (номер заказа).
 • Чёрный список покупателей, которым отзыв НЕ оставляется.
 • Очередь и история отзывов хранятся на диске — переживают рестарт Кардинала.
 • Каждому заказу — не более одного отзыва. Ошибки ретраятся (до 3 попыток),
   «протухшие» записи (>7 дней) чистятся.
 • 🧪 Кнопка «Тест» — показать, как будет выглядеть отзыв.
 • Отзывы отправляются штатным методом FunPay (видны в карточке заказа).

Команды Telegram:
 • /review — открыть меню плагина.

Установка: положить этот файл в папку plugins/ и перезапустить Кардинал.
Автор: yungspider
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import TYPE_CHECKING

from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B, CallbackQuery, Message
from telebot.apihelper import ApiTelegramException

try:
    from tg_bot import CBT, static_keyboards as skb, utils as tg_utils
except Exception:
    CBT = None
    skb = None
    tg_utils = None

if TYPE_CHECKING:
    from cardinal import Cardinal

logger = logging.getLogger("FPC.AutoReview")

# ══════════════════════════════ МЕТАДАННЫЕ ПЛАГИНА ══════════════════════════════
NAME = "AutoReview (Automatic Buyer Reviews)"
VERSION = "1.0.0"
DESCRIPTION = ("Автоматически оставляет покупателям отзыв с оценкой через N минут после "
               "автовыдачи. Шаблон и оценка настраиваются, чёрный список покупателей, "
               "очередь переживает рестарт. Команда: /review")
CREDITS = "yungspider"
UUID = "3576944d-24d7-4e54-91f1-db55766a0dfe"
SETTINGS_PAGE = True

# ══════════════════════════════ НАСТРОЙКИ ПЛАГИНА ═══════════════════════════════
PLUGIN_DIR = os.path.join("storage", "plugins", "auto_review")
SETTINGS_PATH = os.path.join(PLUGIN_DIR, "settings.json")

DEFAULT_TEMPLATE = ("Спасибо за покупку лота «$lot», $username! Всё на высшем уровне — "
                    "моментальная оплата, отличный покупатель. Рекомендую! ⭐")

DEFAULT_SETTINGS = {
    "enabled": True,
    "delay_minutes": 30,      # задержка после выдачи
    "rating": 5,              # оценка 1-5
    "template": DEFAULT_TEMPLATE,
    "blacklist": [],          # список ников (в нижнем регистре) без авто-отзыва
    "pending": {},            # {order_id: {"ts","username","lot","buyer_id","tries"}}
    "reviewed": {},           # {order_id: {"ts","username","rating"}} — история (ограничена)
}
DELAY_RANGE = (1, 4320)        # минут (1 мин — 3 суток)
RATING_RANGE = (1, 5)
PRUNE_AFTER = 7 * 86400        # «протухшие» записи старше 7 дней
MAX_TRIES = 3
LOOP_SLEEP = 20
HISTORY_LIMIT = 500            # максимум записей в истории отзывов

# ══════════════════════════════ CALLBACK-КОДЫ ═══════════════════════════════════
CB_HOME = "ar:home"
CB_TOG = "ar:tog"
CB_DELAY = "ar:delay"
CB_RATING = "ar:rating"
CB_TMPL = "ar:tmpl"
CB_BL = "ar:bl"
CB_TEST = "ar:test"
CB_NOOP = "ar:noop"
ST_DELAY = "ar:st:delay"
ST_RATING = "ar:st:rating"
ST_TMPL = "ar:st:tmpl"
ST_BL = "ar:st:bl"

_stop_event = threading.Event()
_settings_lock = threading.Lock()
_send_lock = threading.Lock()
_cardinal: "Cardinal | None" = None


# ══════════════════════════════ УТИЛИТЫ ═════════════════════════════════════════
def _clamp(value, min_v, max_v):
    return max(min_v, min(max_v, value))


def _load_settings() -> dict:
    with _settings_lock:
        data = {}
        if os.path.exists(SETTINGS_PATH):
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                logger.error("AutoReview: не удалось прочитать настройки, использую значения по умолчанию.")
                logger.debug("TRACEBACK", exc_info=True)
                data = {}
        settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in settings})
        settings["enabled"] = bool(settings["enabled"])
        try:
            settings["delay_minutes"] = _clamp(int(settings["delay_minutes"]), *DELAY_RANGE)
        except (TypeError, ValueError):
            settings["delay_minutes"] = DEFAULT_SETTINGS["delay_minutes"]
        try:
            settings["rating"] = _clamp(int(settings["rating"]), *RATING_RANGE)
        except (TypeError, ValueError):
            settings["rating"] = DEFAULT_SETTINGS["rating"]
        if not isinstance(settings["template"], str) or not settings["template"].strip():
            settings["template"] = DEFAULT_TEMPLATE
        if not isinstance(settings["blacklist"], list):
            settings["blacklist"] = []
        settings["blacklist"] = [str(x).strip().lower() for x in settings["blacklist"] if str(x).strip()]
        if not isinstance(settings["pending"], dict):
            settings["pending"] = {}
        if not isinstance(settings["reviewed"], dict):
            settings["reviewed"] = {}
        return settings


def _save_settings(settings: dict) -> None:
    with _settings_lock:
        try:
            os.makedirs(PLUGIN_DIR, exist_ok=True)
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("AutoReview: не удалось сохранить настройки.")
            logger.debug("TRACEBACK", exc_info=True)


def _short(text: str, limit: int = 40) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escape(text) -> str:
    if tg_utils:
        return tg_utils.escape(str(text))
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plugin_enabled(crd: "Cardinal") -> bool:
    try:
        return crd.plugins[UUID].enabled
    except Exception:
        return False


def _fill_template(template: str, username: str, lot: str, order_id: str) -> str:
    return (template.replace("$username", str(username or "друг"))
            .replace("$lot", str(lot or "товар"))
            .replace("$order", str(order_id or "")))


def _is_blacklisted(settings: dict, username: str, buyer_id=None) -> bool:
    bl = set(settings.get("blacklist") or [])
    if not bl:
        return False
    if str(username or "").strip().lower() in bl:
        return True
    if buyer_id is not None and str(buyer_id).strip().lower() in bl:
        return True
    return False


# ══════════════════════════════ ОЧЕРЕДЬ ОТЗЫВОВ ══════════════════════════════════
def post_delivery_handler(c: "Cardinal", e, *args) -> None:
    """BIND_TO_POST_DELIVERY: ставит заказ в очередь на авто-отзыв после выдачи."""
    try:
        if not _plugin_enabled(c):
            return
        settings = _load_settings()
        if not settings["enabled"]:
            return
        if not getattr(e, "delivered", False):
            return
        order = getattr(e, "order", None)
        if order is None:
            return
        oid = str(order.id)
        if oid in settings["pending"] or oid in settings["reviewed"]:
            return
        username = str(getattr(order, "buyer_username", "") or "")
        buyer_id = getattr(order, "buyer_id", None)
        if _is_blacklisted(settings, username, buyer_id):
            logger.info(f"AutoReview: покупатель {username or buyer_id} в чёрном списке — отзыв пропущен (#{oid}).")
            return
        settings["pending"][oid] = {
            "ts": time.time(),
            "username": username,
            "lot": str(getattr(order, "description", "") or ""),
            "buyer_id": buyer_id,
            "tries": 0,
        }
        _save_settings(settings)
        logger.debug(f"AutoReview: заказ #{oid} поставлен в очередь на отзыв.")
    except Exception:
        logger.error("AutoReview: ошибка в обработчике выдачи.")
        logger.debug("TRACEBACK", exc_info=True)


def _review_tick(crd: "Cardinal", now_ts: float | None = None) -> list:
    """Один проход очереди: оставляет созревшие отзывы. Возвращает список order_id с успешным отзывом."""
    settings = _load_settings()
    now_ts = now_ts if now_ts is not None else time.time()
    delay_s = settings["delay_minutes"] * 60
    rating = settings["rating"]
    done_ids = []
    changed = False
    for oid, entry in list(settings["pending"].items()):
        if _stop_event.is_set():
            break
        try:
            ts = float(entry.get("ts", 0))
        except (TypeError, ValueError):
            ts = 0.0
        age = now_ts - ts
        if age > PRUNE_AFTER:
            del settings["pending"][oid]
            changed = True
            logger.info(f"AutoReview: запись #{oid} устарела (>7 дней) — удалена без отправки.")
            continue
        if age < delay_s:
            continue
        # повторная проверка чёрного списка (мог измениться, пока заказ ждал в очереди)
        if _is_blacklisted(settings, entry.get("username"), entry.get("buyer_id")):
            del settings["pending"][oid]
            changed = True
            logger.info(f"AutoReview: #{oid} — покупатель в чёрном списке, отзыв отменён.")
            continue
        text = _fill_template(settings["template"], entry.get("username"), entry.get("lot"), oid)
        ok = False
        with _send_lock:
            try:
                crd.account.send_review(oid, text, rating)
                ok = True
            except Exception:
                logger.error(f"AutoReview: не удалось оставить отзыв по заказу #{oid}.")
                logger.debug("TRACEBACK", exc_info=True)
        if ok:
            del settings["pending"][oid]
            settings["reviewed"][oid] = {"ts": now_ts, "username": entry.get("username"), "rating": rating}
            # ограничиваем историю
            if len(settings["reviewed"]) > HISTORY_LIMIT:
                oldest = sorted(settings["reviewed"].items(), key=lambda kv: float(kv[1].get("ts", 0)))
                for k, _ in oldest[:len(settings["reviewed"]) - HISTORY_LIMIT]:
                    settings["reviewed"].pop(k, None)
            done_ids.append(oid)
            logger.info(f"AutoReview: отзыв по заказу #{oid} отправлен "
                        f"(покупатель {entry.get('username')}, оценка {rating}).")
        else:
            entry["tries"] = int(entry.get("tries", 0)) + 1
            if entry["tries"] >= MAX_TRIES:
                del settings["pending"][oid]
                logger.error(f"AutoReview: отзыв по #{oid} отброшен после {MAX_TRIES} попыток.")
        changed = True
        time.sleep(1)
    if changed:
        _save_settings(settings)
    return done_ids


def _review_loop(crd: "Cardinal") -> None:
    logger.info("AutoReview: очередь отзывов запущена.")
    while not _stop_event.wait(LOOP_SLEEP):
        try:
            if not _plugin_enabled(crd) or not crd.account.is_initiated:
                continue
            if not _load_settings()["enabled"]:
                continue
            _review_tick(crd)
        except Exception:
            logger.error("AutoReview: ошибка в цикле очереди.")
            logger.debug("TRACEBACK", exc_info=True)


# ══════════════════════════ TELEGRAM-ИНТЕРФЕЙС ══════════════════════════════════
def _home_text(crd: "Cardinal") -> str:
    settings = _load_settings()
    state = "ВКЛ 🟢" if settings["enabled"] else "ВЫКЛ 🔴"
    stars = "⭐" * int(settings["rating"])
    return (f"🌟 <b>{_escape(NAME)}</b> <i>v{VERSION}</i>\n\n"
            f"После выдачи покупатель автоматически получит от вас отзыв с оценкой.\n\n"
            f"Авто-отзывы: <b>{state}</b>\n"
            f"⏱ Задержка после выдачи: <b>{settings['delay_minutes']}</b> мин.\n"
            f"🔥 Оценка: <b>{settings['rating']}</b> ({stars})\n"
            f"📮 В очереди сейчас: <b>{len(settings['pending'])}</b>\n"
            f"✅ Оставлено отзывов: <b>{len(settings['reviewed'])}</b>\n"
            f"⛔ В чёрном списке: <b>{len(settings['blacklist'])}</b>\n\n"
            f"<b>Шаблон</b> (переменные: <code>$username</code>, <code>$lot</code>, "
            f"<code>$order</code>):\n<code>{_escape(_short(settings['template'], 300))}</code>")


def _home_kb() -> K:
    settings = _load_settings()
    kb = K(row_width=2)
    kb.add(B(f"{'🟢 Включено' if settings['enabled'] else '🔴 Выключено'}", None, CB_TOG))
    kb.add(B(f"⏱ Задержка: {settings['delay_minutes']} мин.", None, CB_DELAY))
    kb.add(B(f"🔥 Оценка: {settings['rating']} ⭐", None, CB_RATING))
    kb.add(B("📝 Текст отзыва", None, CB_TMPL))
    kb.add(B("⛔ Чёрный список", None, CB_BL))
    kb.add(B("🧪 Тест (предпросмотр)", None, CB_TEST))
    kb.add(B("🔄 Обновить меню", None, CB_HOME))
    if CBT:
        kb.add(B("🔙 Назад к плагину", None, f"{CBT.EDIT_PLUGIN}:{UUID}:0"))
    return kb


def _answer(bot, c: CallbackQuery, text: str | None = None, alert: bool = False) -> None:
    try:
        bot.answer_callback_query(c.id, text, show_alert=alert)
    except Exception:
        pass


def _edit_or_send(bot, c: CallbackQuery, text: str, kb: K | None) -> None:
    try:
        bot.edit_message_text(text, c.message.chat.id, c.message.id, parse_mode="HTML", reply_markup=kb)
    except ApiTelegramException as e:
        if "message is not modified" not in str(e):
            try:
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                logger.debug("TRACEBACK", exc_info=True)
    except Exception:
        try:
            bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)


def init(crd: "Cardinal", *args) -> None:
    """Инициализация плагина (BIND_TO_PRE_INIT)."""
    global _cardinal
    _cardinal = crd
    _load_settings()

    tg = crd.telegram
    if tg is not None:
        bot = tg.bot

        def open_home(c: CallbackQuery) -> None:
            _answer(bot, c)
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def toggle_enabled(c: CallbackQuery) -> None:
            settings = _load_settings()
            settings["enabled"] = not settings["enabled"]
            _save_settings(settings)
            _answer(bot, c, f"🌟 Авто-отзывы: {'ВКЛ' if settings['enabled'] else 'ВЫКЛ'}")
            _edit_or_send(bot, c, _home_text(crd), _home_kb())

        def act_delay(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"⏱ Введите задержку авто-отзыва после выдачи в минутах "
                f"(<code>{DELAY_RANGE[0]}</code> - <code>{DELAY_RANGE[1]}</code>).\n\n"
                f"Текущее значение: <b>{s['delay_minutes']}</b> мин.",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_DELAY)
            _answer(bot, c)

        def act_rating(c: CallbackQuery) -> None:
            s = _load_settings()
            result = bot.send_message(
                c.message.chat.id,
                f"🔥 Введите оценку для авто-отзыва (<code>{RATING_RANGE[0]}</code> - "
                f"<code>{RATING_RANGE[1]}</code>).\n\nТекущая оценка: <b>{s['rating']}</b> ⭐",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_RATING)
            _answer(bot, c)

        def act_template(c: CallbackQuery) -> None:
            result = bot.send_message(
                c.message.chat.id,
                "📝 Отправьте новый текст отзыва.\n\n"
                "Переменные:\n<code>$username</code> — ник покупателя\n"
                "<code>$lot</code> — название лота\n<code>$order</code> — номер заказа",
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_TMPL)
            _answer(bot, c)

        def act_blacklist(c: CallbackQuery) -> None:
            result = bot.send_message(
                c.message.chat.id,
                "⛔ Введите ник покупателя (или его ID), которому НЕ нужно оставлять отзыв.\n"
                "Отправьте <code>-ник</code> (с минусом), чтобы удалить его из списка.\n"
                "Текущий список:\n<code>" +
                (_escape(", ".join(_load_settings()["blacklist"]) or "пусто") + "</code>"),
                parse_mode="HTML", reply_markup=skb.CLEAR_STATE_BTN() if skb else None)
            tg.set_state(c.message.chat.id, result.id, c.from_user.id, ST_BL)
            _answer(bot, c)

        def send_test(c: CallbackQuery) -> None:
            settings = _load_settings()
            sample = _fill_template(settings["template"], "Иван", "Аккаунт Steam с играми", "TEST1234")
            try:
                bot.send_message(c.message.chat.id,
                                 f"🧪 <i>Так будет выглядеть отзыв (оценка {settings['rating']} ⭐):</i>\n\n"
                                 f"{_escape(sample)}", parse_mode="HTML")
                _answer(bot, c, "🧪 Предпросмотр отправлен выше.")
            except Exception:
                _answer(bot, c, "Не удалось отправить предпросмотр.", alert=True)

        def _state_eq(m: Message, state: str) -> bool:
            try:
                return tg.check_state(m.chat.id, m.from_user.id, state)
            except Exception:
                return False

        def edit_delay(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            try:
                value = _clamp(int(str(m.text).strip()), *DELAY_RANGE)
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число минут. Попробуйте еще раз.")
                return
            settings = _load_settings()
            settings["delay_minutes"] = value
            _save_settings(settings)
            bot.reply_to(m, f"✅ Задержка авто-отзыва: <b>{value}</b> мин.", parse_mode="HTML")

        def edit_rating(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            try:
                value = _clamp(int(str(m.text).strip()), *RATING_RANGE)
            except (ValueError, AttributeError):
                bot.reply_to(m, "❌ Нужно ввести число от 1 до 5. Попробуйте еще раз.")
                return
            settings = _load_settings()
            settings["rating"] = value
            _save_settings(settings)
            bot.reply_to(m, f"✅ Оценка авто-отзыва: <b>{value}</b> ⭐", parse_mode="HTML")

        def edit_template(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            text = str(m.text or "").strip()
            if not text:
                bot.reply_to(m, "❌ Пустой текст не сохранен.")
                return
            settings = _load_settings()
            settings["template"] = text[:1500]
            _save_settings(settings)
            bot.reply_to(m, "📝 Текст отзыва сохранен.", parse_mode="HTML")

        def edit_blacklist(m: Message) -> None:
            tg.clear_state(m.chat.id, m.from_user.id, True)
            raw = str(m.text or "").strip()
            if not raw:
                bot.reply_to(m, "❌ Пустое значение не сохранено.")
                return
            settings = _load_settings()
            bl = list(settings["blacklist"])
            if raw.startswith("-"):
                target = raw[1:].strip().lower()
                if target in bl:
                    bl.remove(target)
                    bot.reply_to(m, f"✅ <code>{_escape(target)}</code> удалён из чёрного списка.",
                                 parse_mode="HTML")
                else:
                    bot.reply_to(m, f"ℹ️ <code>{_escape(target)}</code> в чёрном списке не найден.",
                                 parse_mode="HTML")
            else:
                target = raw.lower()
                if target not in bl:
                    bl.append(target)
                bot.reply_to(m, f"⛔ <code>{_escape(target)}</code> добавлен в чёрный список.",
                             parse_mode="HTML")
            settings["blacklist"] = bl
            _save_settings(settings)

        def cmd_open(m: Message) -> None:
            bot.send_message(m.chat.id, _home_text(crd), parse_mode="HTML", reply_markup=_home_kb())

        def noop(c: CallbackQuery) -> None:
            _answer(bot, c)

        tg.cbq_handler(open_home, lambda c: c.data.startswith(f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        tg.cbq_handler(open_home, lambda c: c.data == CB_HOME)
        tg.cbq_handler(toggle_enabled, lambda c: c.data == CB_TOG)
        tg.cbq_handler(act_delay, lambda c: c.data == CB_DELAY)
        tg.cbq_handler(act_rating, lambda c: c.data == CB_RATING)
        tg.cbq_handler(act_template, lambda c: c.data == CB_TMPL)
        tg.cbq_handler(act_blacklist, lambda c: c.data == CB_BL)
        tg.cbq_handler(send_test, lambda c: c.data == CB_TEST)
        tg.cbq_handler(noop, lambda c: c.data == CB_NOOP)
        tg.msg_handler(edit_delay, func=lambda m: _state_eq(m, ST_DELAY))
        tg.msg_handler(edit_rating, func=lambda m: _state_eq(m, ST_RATING))
        tg.msg_handler(edit_template, func=lambda m: _state_eq(m, ST_TMPL))
        tg.msg_handler(edit_blacklist, func=lambda m: _state_eq(m, ST_BL))
        tg.msg_handler(cmd_open, commands=["review"])

        try:
            crd.add_telegram_commands(UUID, [("review", "Автоматические отзывы покупателям.", True)])
        except Exception:
            logger.debug("TRACEBACK", exc_info=True)

        logger.info(f"AutoReview v{VERSION}: Telegram-меню зарегистрировано (команда /review).")
    else:
        logger.info(f"AutoReview v{VERSION}: Telegram выключен — очередь работает в фоне.")

    threading.Thread(target=_review_loop, args=(crd,), daemon=True).start()


def on_delete(crd: "Cardinal", c=None) -> None:
    """Вызывается при удалении плагина через ПУ (BIND_TO_DELETE)."""
    _stop_event.set()


# ══════════════════════════ ПРИВЯЗКА К СОБЫТИЯМ КАРДИНАЛА ═══════════════════════
BIND_TO_PRE_INIT = [init]
BIND_TO_POST_DELIVERY = [post_delivery_handler]
BIND_TO_DELETE = on_delete
