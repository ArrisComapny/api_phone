"""
Маршрутизация входящих SMS: определение маркетплейса и конкретного магазина.

Схема:  номер + текст SMS → detect_marketplace → detect_shop → get_sms_recipients → отправка

Одного номера для маршрутизации недостаточно — на одном номере могут быть кабинеты
разных маркетплейсов, поэтому площадка определяется по тексту/отправителю,
а магазин — по совпадению названия из таблицы markets с текстом.

Как расширять: добавить запись в MARKETPLACE_RULES (ключевые слова и/или регулярки).
Порядок правил важен — побеждает первое совпадение, поэтому более специфичные выше.
"""
import re

# Канонические имена площадок — совпадают со значениями markets.marketplace и phone_message.marketplace
OZON = 'Ozon'
WB = 'WB'
YANDEX = 'Yandex'
MVIDEO = 'МВидео'

# Площадка -> ключ площадки в employees.role ('head ozon', 'manager ozon', ...)
MARKETPLACE_KEY = {
    OZON: 'ozon',
    WB: 'wb',
    YANDEX: 'yandex',
    MVIDEO: 'mvideo',
}


def marketplace_roles(marketplace: str) -> list[str]:
    """Роли, получающие SMS площадки (кроме admin с receive_sms): ['head ozon', 'manager ozon']."""
    key = MARKETPLACE_KEY.get(marketplace)
    if key is None:
        return []
    return [f'head {key}', f'manager {key}']


# Площадка -> подпись ролей для логов
MARKETPLACE_ROLES = {mp: ' / '.join(marketplace_roles(mp)) for mp in MARKETPLACE_KEY}

# Правила распознавания площадки.
#   keywords — подстроки, ищутся без учёта регистра (надёжны для брендов);
#   patterns — регулярки с границами слов для коротких/двусмысленных токенов
#              (иначе "wb" нашлось бы внутри случайного слова, а "маркет" — в "маркетплейс").
MARKETPLACE_RULES = [
    {
        'marketplace': WB,
        'keywords': ['wildberries', 'вайлдберриз'],
        'patterns': [r'\bwb\b', r'\bвб\b'],
    },
    {
        'marketplace': OZON,
        'keywords': ['ozon', 'озон'],
        'patterns': [],
    },
    {
        'marketplace': MVIDEO,
        'keywords': ['m.video', 'mvideo', 'мвидео', 'м.видео'],
        'patterns': [],
    },
    {
        # Yandex последним: "market/маркет" двусмысленны, пусть сначала отработают явные бренды
        'marketplace': YANDEX,
        'keywords': ['yandex', 'яндекс'],
        'patterns': [r'\bmarket\b', r'\bмаркет\b'],
    },
]


def _rule_matches(rule: dict, source: str) -> bool:
    if any(k in source for k in rule['keywords']):
        return True
    return any(re.search(p, source, re.IGNORECASE) for p in rule['patterns'])


def detect_marketplace(message_text: str = '', sender: str = '') -> str | None:
    """
    Определяет маркетплейс по тексту SMS и отправителю.
    Сначала проверяется отправитель (он надёжнее: 'Wildberries', 'OZON.ru'),
    затем текст. Возвращает каноническое имя (см. MARKETPLACE_KEY) или None, если не распознано.
    """
    for source in ((sender or '').lower(), (message_text or '').lower()):
        if not source:
            continue
        for rule in MARKETPLACE_RULES:
            if _rule_matches(rule, source):
                return rule['marketplace']
    return None


def resolve_marketplace(candidates: list[str], message_text: str = '', sender: str = '') -> str | None:
    """
    Выбирает площадку для marketplace-номера.
    candidates — площадки, на которые зарегистрирован номер (из markets, см. get_marketplaces_by_number).
      одна   → она и есть;
      несколько → уточняем по отправителю/тексту: если детект даёт одну из кандидатов — она;
      иначе None — площадку не различить, решение принимает вызывающий (без угадывания).
    """
    cands = [c for c in candidates if c]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    detected = detect_marketplace(message_text=message_text, sender=sender)
    return detected if detected in cands else None


def detect_shop(message_text: str, shops: list[str]) -> str | None:
    """
    Определяет конкретный магазин среди кандидатов `shops` (названия из markets
    для этого номера и площадки — см. DbConnection.get_shops_for_phone).

    - один кандидат → он и есть магазин;
    - несколько → ищем название магазина в тексте SMS; если совпало ровно одно — оно;
    - иначе None: магазин не определён однозначно. Наверх это уходит как «не определён»,
      и сообщение получают все менеджеры площадки с доступом к номеру — никогда «случайный».
    """
    if not shops:
        return None
    if len(shops) == 1:
        return shops[0]

    text = (message_text or '').lower()
    matched = [s for s in shops if s and s.lower() in text]
    if len(matched) == 1:
        return matched[0]
    return None
