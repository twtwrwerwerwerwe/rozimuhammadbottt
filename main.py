import json
import logging
import re
import asyncio
import time
import unicodedata
from contextlib import suppress
from functools import lru_cache
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

TELETHON_IMPORT_ERROR: Optional[ModuleNotFoundError] = None
try:
    from telethon import TelegramClient, events
    from telethon.errors import RPCError
    from telethon.tl.custom.message import Message
except ModuleNotFoundError as exc:
    TELETHON_IMPORT_ERROR = exc
    TelegramClient = Any  # type: ignore[assignment]
    events = None  # type: ignore[assignment]

    class RPCError(Exception):
        pass

    Message = Any  # type: ignore[assignment]

try:
    from admin_panel import register_admin_handlers as register_admin_panel_handlers
except Exception:
    register_admin_panel_handlers = None

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
MAX_TELEGRAM_TEXT = 4096
FILTER_RULESET_VERSION = "2026-03-04.01"
DUPLICATE_WINDOW_SECONDS = 15 * 60
DUPLICATE_TEXT_ONLY_WINDOW_SECONDS = 2 * 60
DUPLICATE_CACHE_LIMIT = 5000
BOT_CACHE_LIMIT = 5000

DEFAULT_CONFIG: Dict[str, Any] = {
    "API_ID": 32924078,
    "API_HASH": "5fb624acedb64de522eff541a4b6d7f5",
    "SESSION_NAME": "userbot_session",
    "RELAY_ENABLED": True,
    "SOURCE_GROUPS": [-1003869005189],
    "DRIVER_GROUP": -1003725289081,
    "BOT_TOKEN": "8788935544:AAGSzhBJWdDFjOh7WeZtRFsDnBns9fQVmh0",
    "ADMIN_IDS": [8638810719],
    "ADMIN_BOT_SESSION": "https://t.me/rozimuhammadakramjonovBot_bot",
    "AD_BLOCK_USER_IDS": [],
    "AD_BLOCK_REFS": [],
    "BLOCKED_CHAT_IDS": [],
}

CONFIG: Dict[str, Any] = {}
RUNTIME_STATS: Dict[str, int] = {
    "received": 0,
    "forwarded": 0,
    "filtered": 0,
    "errors": 0,
    "reviewed": 0,
    "tokens_total": 0,
    "tokens_filtered": 0,
    "tokens_forwarded": 0,
    "tokens_reviewed": 0,
}
RECENT_MESSAGE_CACHE: Dict[str, float] = {}
RECENT_TEXT_ONLY_CACHE: Dict[str, float] = {}
BOT_SENDER_CACHE: Dict[int, bool] = {}
TOKEN_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z0-9]+)?", re.UNICODE)
TOKEN_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _stat_inc(key: str, delta: int = 1) -> None:
    RUNTIME_STATS[key] = int(RUNTIME_STATS.get(key, 0)) + int(delta)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _usage_field(usage: Any, key: str) -> int:
    if usage is None:
        return 0
    if isinstance(usage, dict):
        return _to_int(usage.get(key))
    return _to_int(getattr(usage, key, 0))


def _first_positive(values: List[int]) -> int:
    for value in values:
        if value > 0:
            return value
    return 0


def estimate_text_token_count(text: str) -> int:
    raw = (text or "").strip()
    if not raw:
        return 0
    char_based = max(1, (len(raw) + 3) // 4)
    words = TOKEN_WORD_RE.findall(raw)
    word_based = len(words)
    punct_bonus = len(TOKEN_PUNCT_RE.findall(raw)) // 3
    return max(char_based, word_based + punct_bonus)


def calculate_ai_token_usage(
    prompt_text: str = "",
    completion_text: str = "",
    system_text: str = "",
    usage: Any = None,
) -> Dict[str, Any]:
    prompt_tokens = _first_positive([
        _usage_field(usage, "prompt_tokens"),
        _usage_field(usage, "input_tokens"),
    ])
    completion_tokens = _first_positive([
        _usage_field(usage, "completion_tokens"),
        _usage_field(usage, "output_tokens"),
    ])
    total_tokens = _usage_field(usage, "total_tokens")

    has_provider_usage = prompt_tokens > 0 or completion_tokens > 0 or total_tokens > 0

    if prompt_tokens <= 0:
        prompt_tokens = estimate_text_token_count(system_text) + estimate_text_token_count(prompt_text)
    if completion_tokens <= 0:
        completion_tokens = estimate_text_token_count(completion_text)
    if total_tokens <= 0:
        total_tokens = prompt_tokens + completion_tokens

    return {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(total_tokens),
        "estimated": not has_provider_usage,
    }

PASSENGER_TAXI_MARKERS = [
    "taksi",
    "taxi",
    "cab",
    "ride",
    "uber",
    "yandex",
    "yandex taxi",
    "yandextaxi",
    "taksi kerak",
    "taxi kerak",
    "taksi kerka",
    "taxi kerka",
    "mashina kerak",
    "mashina kerka",
    "mashina kerek",
    "taksi bormi",
    "taxi bormi",
    "menga taksi",
    "bizga taksi",
    "taksiga",
    "taxiga",
    "taksi chaqir",
    "taxi chaqir",
    "taksi yubor",
    "taxi yubor",
    "mijozman",
    "mijozmiz",
    "yo'lovchi",
    "yolovchi",
    "yo lovchi",
    "mijoz",
    "passajir",
    "pasajir",
    "passazhir",
    "passenger",
    "buyurtma taksi",
    "buyurtma taxi",
    "zakaz taxi",
    "taxi zakaz",
    "taxi order",
    "transfer",
    "trip",
    "poezdka",
]

PASSENGER_DIRECT_PHRASES = [
    "menga taksi kerak",
    "bizga taksi kerak",
    "hozir taksi kerak",
    "zudlik bilan taksi",
    "tezda taksi",
    "kim bor",
    "kim bormi",
    "kim olib ketadi",
    "kim olib boradi",
    "olib keting",
    "olib ketinglar",
    "olib boring",
    "olib bering",
    "taksi chaqiring",
    "taxi chaqiring",
    "taksi yuboring",
    "taxi yuboring",
    "yo'lovchi kerak",
    "passajir kerak",
    "mijoz kerak",
    "mashina kerak",
    "mashina kerka",
    "taxi kerka",
    "taksi kerka",
    "airportga taksi",
    "aeroportga taksi",
    "vokzalga taksi",
    "manzilga taksi",
]

PASSENGER_REQUEST_MARKERS = [
    "kerak",
    "kerka",
    "kerek",
    "kere",
    "kerak edi",
    "kerakman",
    "kerakmiz",
    "zarur",
    "zudlik bilan",
    "tezroq",
    "tezzroq",
    "hozir kerak",
    "hozir ketish",
    "bormi",
    "bormi?",
    "kim bor",
    "kim bormi",
    "kim oladi",
    "kim olib ketadi",
    "kim olib boradi",
    "olib keting",
    "olib boring",
    "olib bering",
    "olib qoying",
    "olib qo'ying",
    "olib ketvoring",
    "olib borvoring",
    "chaqiring",
    "chaqirib bering",
    "yuboring",
    "tezroq yuboring",
    "qayerga",
    "qayerdan",
    "qayerga boradi",
    "yo'nalish",
    "yonalish",
    "manzil",
    "adres",
    "location",
    "lokatsiya",
    "locatsiya",
    "ketaman",
    "ketamiz",
    "boraman",                      
    "boramiz",
    "jo'nayman",
    "jo'naymiz",
    "jonayman",
    "jonaymiz",
    "chiqaman",
    "chiqamiz",
    "yo'lga chiqamiz",
    "yolga chiqamiz",
    "yetkazib qo'ying",
    "yetkazib bering",
    "narx qancha",
    "narxi qancha",
    "qancha bo'ladi",
    "yo'lda qoldim",
    "yo'lda qoldik",
    "yordam kerak",
    "kutib turibman",
    "transfer kerak",
    "buyurtma kerak",
    "otpravka",
    "podvezite",
    "podvezti",
    "nujen",
    "nujna",
    "nujno",
    "nado",
    "srochno",
    "srochna",
]

PASSENGER_TIME_MARKERS = [
    "soat",
    "bugun",
    "ertaga",
    "indin",
    "hozir",
    "hozircha",
    "ertalab",
    "sahar",
    "tong",
    "kunduzi",
    "kechqurun",
    "kechasi",
    "kechki",
    "kechga",
    "tun",
    "tunda",
    "dushanba",
    "seshanba",
    "chorshanba",
    "payshanba",
    "juma",
    "shanba",
    "yakshanba",
]

PASSENGER_LOCATION_MARKERS = [
    "dan",
    "ga",
    "aeroport",
    "airport",
    "vokzal",
    "metro",
    "bekat",
    "terminal",
    "bozor",
    "mahalla",
    "rayon",
    "tuman",
    "kocha",
    "ko'chasi",
    "kochasi",
    "chorraha",
    "post",
    "uy",
    "dom",
    "kvartal",
    "massiv",
    "stansiya",
    "station",
    "trassa",
    "yo'l",
    "yul",
]

DRIVER_MARKERS = [
    "olaman",
    "olib ketaman",
    "olib ketamiz",
    "olib ketvoraman",
    "olib boraman",
    "olib boramiz",
    "olib borvoraman",
    "yuramiz olamiz",
    "olamiz va yuramiz",
    "yuramiz va olamiz",
    "bo'shman",
    "boshman",
    "bo'shmiz",
    "boshmiz",
    "haydovchi",
    "haydovchiman",
    "haydovchimiz",
    "taksist",
    "taksistman",
    "taksistmiz",
    "xizmat",
    "dostavka",
    "mashinam bor",
    "mashina bor",
    "voditel",
    "voditelman",
    "svoboden",
    "svobodniy",
    "zakaz olaman",
    "zakaz olamiz",
    "yo'lovchi olaman",
    "yo'lovchi olamiz",
    "mijoz olaman",
    "mijoz olamiz",
    "zakaz bormi",
    "zakaz bor",
    "zakazga chiqaman",
    "bering zakaz",
    "vizov olaman",
    "vizov olamiz",
    "qayerdan olaman",
    "qayerdan olamiz",
    "pitak",
    "lineyka",
    "arendaga mashina",
    "mashina ijaraga",
    "jentra bor",
    "nexia bor",
    "lacetti bor",
    "lasetti bor",
    "spark bor",
    "damas bor",
    "cobalt bor",
    "malibu bor",
    "matiz bor",
    "prius bor",
    "kaptiva bor",
    "tracker bor",
    "onix bor",
    "narx kelishamiz",
    "narx kelishiladi",
    "xizmat korsataman",
    "xizmat ko'rsataman",
]

STRONG_DRIVER_MARKERS = [
    "yo'lovchi olaman",
    "yo'lovchi olamiz",
    "zakaz olaman",
    "zakaz olamiz",
    "haydovchiman",
    "haydovchimiz",
    "taksistman",
    "taksistmiz",
    "mashinam bor",
    "zakazga chiqaman",
    "bering zakaz",
    "vizov olaman",
    "vizov olamiz",
    "olib ketamiz",
    "olib boramiz",
    "yuramiz olamiz",
    "olamiz va yuramiz",
    "yuramiz va olamiz",
]
STRONG_DRIVER_FUZZY_MARKERS = [x for x in STRONG_DRIVER_MARKERS if x not in {"mashinam bor"}]

DRIVER_SELF_OFFER_MARKERS = [
    "olib ketaman",
    "olib ketamiz",
    "olib boraman",
    "olib boramiz",
    "yo'lovchi olaman",
    "yo'lovchi olamiz",
    "mijoz olaman",
    "mijoz olamiz",
    "zakaz olaman",
    "zakaz olamiz",
    "vizov olaman",
    "vizov olamiz",
    "haydovchiman",
    "haydovchimiz",
    "taksistman",
    "taksistmiz",
    "voditelman",
    "voditelmiz",
    "yuramiz olamiz",
    "olamiz va yuramiz",
    "yuramiz va olamiz",
    "yuramiz",
    "po'chta olamiz"
]

SPAM_MARKERS = [
    "sotiladi",
    "sotaman",
    "sotib oling",
    "arenda",
    "ijara",
    "ijaraga beriladi",
    "ijaraga beraman",
    "arendaga beriladi",
    "arendaga beraman",
    "vakansiya",
    "ish bor",
    "ish kerak",
    "ishga taklif",
    "ishga marhamat",
    "obuna",
    "obuna bo'ling",
    "subscribe",
    "kanal",
    "kanalga o'ting",
    "kanalimiz",
    "aksiya",
    "skidka",
    "chegirma",
    "kredit",
    "qarz",
    "kurs",
    "trening",
    "reklama",
    "reklama beraman",
    "reklama xizmati",
    "promo",
    "bonus",
    "invest",
    "crypto",
]
HOTEL_AD_CONTEXT_MARKERS = [
    "arzon",
    "24/7",
    "kruglosutoch",
    "kishi boshiga",
    "kishilik",
    "obshiy",
    "alohida",
    "xona",
    "xonalar",
    "manzil",
    "muljal",
    "mo'ljal",
    "sutka",
    "sutkalik",
    "kunlik",
    "aeroportga yaqin",
]
NAKRUTKA_AD_MARKERS = [
    "nakrutka",
    "nakrutkachi",
    "obunachi",
    "obuna oshirish",
    "obuna kopaytir",
    "subscriber",
    "followers",
    "follower",
    "podpischik",
    "podpis",
    "layk",
    "like",
    "reaksiya",
    "prosmotr",
    "view",
    "views",
    "raskrutka",
    "smm",
]
NAKRUTKA_PROMO_MARKERS = [
    "xizmat",
    "narx",
    "arzon",
    "aksiya",
    "chegirma",
    "paket",
    "garantiya",
    "jonli",
    "real",
    "tez",
    "tezkor",
    "bonus",
    "reklama",
]
EASY_EARN_SCAM_MARKERS = [
    "trebuyutsya",
    "trebuetsya",
    "mutka",
    "mutku",
    "bez obmana",
    "kazhdodnevno",
    "zarabotok",
    "podrabotka",
    "rad pomoch",
]

WORD_NUMBERS = {
    "bitta": "1",
    "bittamiz": "1",
    "ikta": "2",
    "ikkita": "2",
    "uchta": "3",
    "tortta": "4",
    "to'rtta": "4",
    "beshta": "5",
    "oltita": "6",
    "yettita": "7",
    "sakkizta": "8",
    "toqqizta": "9",
    "to'qqizta": "9",
    "onta": "10",
    "o'nta": "10",
    "ikki": "2",
    "uch": "3",
    "tort": "4",
    "to'rt": "4",
    "besh": "5",
    "olti": "6",
    "yetti": "7",
    "sakkiz": "8",
    "toqqiz": "9",
    "to'qqiz": "9",
    "on": "10",
}

APOSTROPHE_CLASS = r"['`\u2019\u02bb\u02bc\u2018]"
APOSTROPHE_NORMALIZE_RE = re.compile(r"[`\u2019\u02bb\u02bc\u2018]")
APOSTROPHE_STRIP_RE = re.compile(r"['`\u2019\u02bb\u02bc\u2018]")
ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")
EMOJI_ORD_RANGES = (
    (0x2600, 0x27BF),
    (0x1F300, 0x1FAFF),
)
CYRILLIC_TRANSLIT_TABLE = {
    0x0430: "a",
    0x0431: "b",
    0x0432: "v",
    0x0433: "g",
    0x0434: "d",
    0x0435: "e",
    0x0451: "yo",
    0x0436: "j",
    0x0437: "z",
    0x0438: "i",
    0x0439: "y",
    0x043a: "k",
    0x043b: "l",
    0x043c: "m",
    0x043d: "n",
    0x043e: "o",
    0x043f: "p",
    0x0440: "r",
    0x0441: "s",
    0x0442: "t",
    0x0443: "u",
    0x0444: "f",
    0x0445: "x",
    0x0446: "ts",
    0x0447: "ch",
    0x0448: "sh",
    0x0449: "sh",
    0x044a: "",
    0x044b: "y",
    0x044c: "",
    0x044d: "e",
    0x044e: "yu",
    0x044f: "ya",
    0x045e: "o'",
    0x049b: "q",
    0x0493: "g'",
    0x04b3: "h",
    0x045f: "j",
}
YOLOVCHI_TOKEN = rf"yo(?:\s|{APOSTROPHE_CLASS})?lovchi"
OZIM_TOKEN = rf"o(?:{APOSTROPHE_CLASS})?zim"
TORT_TOKEN = rf"to(?:{APOSTROPHE_CLASS})?rt"
QOY_TOKEN = rf"qo(?:{APOSTROPHE_CLASS})?y"
OTIRIB_TOKEN = rf"o(?:{APOSTROPHE_CLASS})?tirib"
POCHTA_TOKEN = rf"(?:p(?:o|u){{1,3}}(?:\s|{APOSTROPHE_CLASS}|[^a-z0-9\s])?chta|p(?:o|u){{1,3}}(?:\s|{APOSTROPHE_CLASS}|[^a-z0-9\s])?chat{{1,2}}a|p(?:o|u){{1,3}}cta|pochta|pocta|puchta|pushta|pochata|pochatta)"
RU_PASSAZHIR = r"(?:\u043f\u0430\u0441\u0441\u0430\u0436\u0438\u0440|passazhir)"
PEOPLE_UNIT_PATTERN = rf"(?:kishi|odam|{YOLOVCHI_TOKEN}|passajir|pasajir|{RU_PASSAZHIR}|nafar|kishimiz|odammiz)"
PAX_WORDS_RE = re.compile(
    rf"\b(?:odam|kishi|{YOLOVCHI_TOKEN}|passajir|pasajir|{RU_PASSAZHIR}|biz|men|{OZIM_TOKEN}|oila(?:m)?|bola|farzand)\b"
)
PAX_COUNT_DIGIT_RE = re.compile(
    rf"(?:\b\d+\s*(?:ta|nafar)\s*(?:odam|kishi|{YOLOVCHI_TOKEN}|passajir|pasajir|{RU_PASSAZHIR})?\b|\b\d+\s*(?:odam|kishi|{YOLOVCHI_TOKEN}|passajir|pasajir|{RU_PASSAZHIR})\b)"
)
PAX_COUNT_WORD_RE = re.compile(
    rf"\b(?:bitta|ikki|uch|{TORT_TOKEN}|besh|olti)\b(?:\s*(?:ta|nafar)?\s*(?:odam|kishi|{YOLOVCHI_TOKEN}))?"
)
PAX_INTENT_RE = re.compile(
    rf"\b(?:odam\s*bor|{YOLOVCHI_TOKEN}\s*bor|ket\w*|bor\w*|chiqar\w*|olib\s*(?:ket|{QOY_TOKEN})\w*|chiqib\s*ket\w*|mindir\w*|{OTIRIB_TOKEN}\s*ket\w*|poedem|poehali)\b"
)
PAX_LUGGAGE_RE = re.compile(
    r"\b(?:bagaj|bagazh|sumka|ryukzak|chemodan|kolyaska|stroller|aravacha)\b"
)
PAX_SPECIAL_RE = re.compile(
    r"\b(?:bola|chaqaloq|kreslo|baby\s*seat|ayol|qiz|keks\w*|nogiron|wheel\s*chair|aravacha|mushuk|it|pet)\b"
)
CARGO_NEGATIVE_RE = re.compile(rf"\b(?:{POCHTA_TOKEN}|posilka|dostavka|yetkaz\w*|kargo|hujjat)\b")
HARD_BLOCK_KAM_RE = re.compile(r"\b(?:kam|kamroq|kamida)\b|\b(?:odam|kishi|joy)\s*kam\b|\b(?:odam|kishi|joy)kam\b")
HARD_BLOCK_CARGO_RE = re.compile(rf"\b(?:{POCHTA_TOKEN}|posilka|dostavka|kargo|yuk|hujjat)\b")
CARGO_REQUEST_WORD_RE = re.compile(rf"\b(?:{POCHTA_TOKEN}|posilka|dostavka|kargo|yuk|hujjat|bagaj|bagazh|bagachka|bagajka)\b")
CARGO_REQUEST_INTENT_RE = re.compile(
    r"\b(?:bor|bormi|bor\s*mi|kerak|kerka|kerek|kere|ket\w*|jon\w*|jo(?:'|`)?n\w*|chiq\w*|yetkaz\w*|olib\s*(?:ket|bor)\w*|ber\w*|kim\s*bor|kim\s*bormi)\b"
)
NEGATIVE_REQUEST_RE = re.compile(
    r"\b(?:kerakmas|keremas|kerekmas|kerak\s+emas|kerek\s+emas|kere\s+emas|nado\s+net)\b"
)
VEHICLE_WORD_RE = re.compile(r"\b(?:mashina|mashna|taksi|taxi|avto)\b")
BASIC_WORD_NUMBER_MAP = {"bitta": "1", "ikki": "2", "uch": "3", "tort": "4", "besh": "5", "olti": "6"}
DRIVER_SELF_OFFER_RE = re.compile(
    rf"\b(?:olaman|olib\s*(?:ket|bor)\w*(?:man|miz)|haydovchi(?:man|miz)?|taksist(?:man|miz)?|voditel(?:man|miz)?|zakaz\s*olaman|zakaz\s*olamiz|vizov\s*olaman|vizov\s*olamiz|svoboden|svobodniy)\b"
)
DRIVER_QUESTION_OFFER_RE = re.compile(
    r"\b(?:qayerdan|qayerga)\b[^\n]{0,32}\b(?:taksi|taxi|mashina)\b[^\n]{0,16}\b(?:kerakmi|kerekmi|keremi|bormi)\b"
    r"|\b(?:taksi|taxi|mashina)\s*(?:kerakmi|kerekmi|keremi)\b"
)
DRIVER_CLEAR_INTENT_RE = re.compile(
    rf"\b(?:yuramiz\s+olamiz|olib\s*(?:ket|bor)\w*miz|zakaz\s*olamiz|yo(?:\s|{APOSTROPHE_CLASS})?lovchi\s*olamiz|mijoz\s*olamiz)\b"
)
DRIVER_OLAMIZ_YURAMIZ_RE = re.compile(
    rf"\b(?:olamiz(?:\s+va)?\s*yuramiz|yuramiz(?:\s+va)?\s*olamiz)\b"
)
DRIVER_FORCE_TOKEN_RE = re.compile(r"\b(?:olamiz|yuramiz)\b")
DRIVER_BERIN_CHIQIB_RE = re.compile(
    rf"\b(?:berin|bering)\b[^\n]{{0,28}}\b(?:chi(?:q|k)[^a-z0-9]{{0,2}}ib\s*ket\w*miz|ketyap\w*miz|chiq\w*miz)\b"
)
DRIVER_FORCE_BASE_TOKENS = ("olamiz", "yuramiz")
SEAT_NEEDED_RE = re.compile(
    rf"\b(?:\d{{1,2}}\s*(?:ta|nafar)?\s*)?(?:odam|kishi|{YOLOVCHI_TOKEN}|passajir|pasajir|{RU_PASSAZHIR})\s*kam\b"
)
EXPLICIT_DRIVER_IDENTITY_RE = re.compile(
    rf"\b(?:haydovchi(?:man|miz)?|taksist(?:man|miz)?|voditel(?:man|miz)?|zakaz\s*ol(?:aman|amiz)|yo(?:\s|{APOSTROPHE_CLASS})?lovchi\s*ol(?:aman|amiz)|mijoz\s*ol(?:aman|amiz)|vizov\s*ol(?:aman|amiz)|olib\s*(?:ket|bor)\w*(?:man|miz)|bo(?:{APOSTROPHE_CLASS})?sh(?:man|miz)|xizmat\s*ko(?:{APOSTROPHE_CLASS})?rsat\w*)\b"
)
GROUP_TRIP_RE = re.compile(
    rf"\b(?:yuramiz|ketamiz|boramiz|jo(?:{APOSTROPHE_CLASS})?naymiz|jonaymiz|chiqamiz)\b"
)
DRIVER_ORDER_AVAILABLE_RE = re.compile(r"\b(?:zakaz|vizov)\s*bor(?:mi)?\b")
DRIVER_ORDER_WORD_RE = re.compile(r"\b(?:zakaz|vizov)\b")
DRIVER_CAR_BOR_RE = re.compile(
    r"\b(?:mashina|mashinam|avto|jentra|nexia|lacetti|lasetti|spark|damas|cobalt|koblt|kobalt|malibu|matiz|prius|kaptiva|tracker|onix)\b[^\n]{0,24}\bbor(?:man|miz)?\b"
)
DRIVER_CAR_WORD_RE = re.compile(
    r"\b(?:avto|jentra|nexia|lacetti|lasetti|spark|damas|cobalt|koblt|kobalt|malibu|matiz|prius|kaptiva|tracker|onix)\b"
)
DRIVER_SEAT_AVAILABLE_RE = re.compile(
    rf"\b(?:\d{{1,2}}\s*(?:ta|nafar)?\s*)?(?:joy|o(?:{APOSTROPHE_CLASS})?rin|odam|kishi|{YOLOVCHI_TOKEN}|passajir|pasajir|passazhir)\s*bor\b"
)
DRIVER_SEAT_COUNT_RE = re.compile(
    rf"\b\d{{1,2}}\s*(?:ta|nafar)?\s*(?:joy|o(?:{APOSTROPHE_CLASS})?rin|odam|kishi|{YOLOVCHI_TOKEN}|passajir|pasajir|passazhir)\b"
)
DRIVER_NUMERIC_SEAT_OFFER_RE = re.compile(
    rf"\b\d{{1,2}}\s*(?:ta|nafar)?\s*(?:joy|o(?:{APOSTROPHE_CLASS})?rin|orin|mesta?)\s*bor(?:mi)?\b"
)
DRIVER_BAGAJ_BOR_RE = re.compile(
    r"\b(?:bagaj|bagazh|bagajka|bagachka)\s*bor(?:mi)?\b|\bbor(?:mi)?\s*(?:bagaj|bagazh|bagajka|bagachka)\b"
)
DRIVER_NEEDS_PASSENGER_RE = re.compile(
    rf"\b(?:{YOLOVCHI_TOKEN}|passajir|pasajir|passazhir|mijoz)\s*(?:kerak|kerka|kerek|kere|bor)\b"
)
DRIVER_CARGO_OFFER_RE = re.compile(
    rf"\b(?:{POCHTA_TOKEN}|posilka|dostavka|kargo|yuk|hujjat)\b[^\n]{{0,28}}\b(?:ol(?:aman|amiz)|yetkaz\w*|ber\w*|olib\s*(?:ket|bor)\w*)\b|\b(?:ol(?:aman|amiz)|yetkaz\w*|ber\w*|olib\s*(?:ket|bor)\w*)\b[^\n]{{0,28}}\b(?:{POCHTA_TOKEN}|posilka|dostavka|kargo|yuk|hujjat)\b"
)
DRIVER_STYLE_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
DRIVER_STYLE_EMOJI_BANNER_RE = re.compile(r"(?:[\U0001F300-\U0001FAFF\u2600-\u27BF]\s*){5,}")
DRIVER_STYLE_KEYWORDS = [
    "yuramiz",
    "odam kam",
    "po'chta olamiz",
    "pochta olamiz",
    "pochata olamiz",
    "pochatta olamiz",
    "zakaz",
    "vizov",
    "avto",
    "kobalt",
    "koblt",
    "nexia",
    "jentra",
    "haydovchi",
    "taksist",
    "yo'lovchi ol",
    "mijoz ol",
]
PAX_SHORT_TRIP_INTENT_RE = re.compile(
    rf"\b(?:ketaman|ketamiz|boraman|boramiz|chiqaraman|chiqaramiz|jo(?:{APOSTROPHE_CLASS})?nayman|jo(?:{APOSTROPHE_CLASS})?naymiz|jonayman|jonaymiz|chiqaman|chiqamiz)\b"
)
PAX_SHORT_TRIP_INTENT_MARKERS = [
    "ketaman",
    "ketamiz",
    "boraman",
    "boramiz",
    "chiqaraman",
    "chiqaramiz",
    "jo'nayman",
    "jo'naymiz",
    "jonayman",
    "jonaymiz",
    "chiqaman",
    "chiqamiz",
]
PAX_VEHICLE_NEED_RE = re.compile(
    r"\b(?:mashina|mashna|taksi|taxi|avto)\b.*\b(?:kerak|kerka|kerek|kere|kk)\b|\b(?:kerak|kerka|kerek|kere|kk)\b.*\b(?:mashina|mashna|taksi|taxi|avto)\b"
)
FAST_PASSENGER_QUESTION_RE = re.compile(r"\b(?:kim\s*bor|kim\s*bormi|bor\s*mi|bormi|olib\s*ketad\w*|olib\s*borad\w*)\b")
FUZZY_PASSENGER_CORE_MARKERS = [
    "ketaman",
    "ketamiz",
    "boraman",
    "boramiz",
    "chiqaman",
    "chiqamiz",
    "kerak",
    "kerka",
    "kerek",
    "kere",
    "taksi",
    "taxi",
    "mashina",
    "mashna",
    "avto",
]
FUZZY_DRIVER_CORE_MARKERS = [
    "olaman",
    "olib ketaman",
    "olib boraman",
    "zakaz olaman",
    "vizov olaman",
    "haydovchiman",
    "taksistman",
    "voditelman",
    "yo'lovchi olaman",
]
FUZZY_PASSENGER_CRITICAL_MARKERS = [
    "taksi",
    "taxi",
    "kerak",
    "kerka",
    "kerek",
    "mashina",
]
FUZZY_DRIVER_CRITICAL_MARKERS = [
    "zakaz olaman",
    "vizov olaman",
    "haydovchiman",
    "taksistman",
    "yo'lovchi olaman",
]
FUZZY_TRIP_CRITICAL_MARKERS = [
    "ketaman",
    "boraman",
    "chiqaman",
]
DRIVER_SERVICE_AD_RE = re.compile(
    r"\b(?:taxi|taksi)\b.*\bxizmat\w*\b|\bxizmat\w*\b.*\b(?:taxi|taksi)\b"
)
DRIVER_PRICE_OFFER_RE = re.compile(
    r"\bnarx\b[^\n]{0,24}\b\d+\s*(?:k|ming|mln|million|so['`’ʻʼ‘]?m|sum|uzs)?\s*(?:dan|gacha)?\b"
)
DEST_SUFFIX_PATTERN = r"(?:ga|ka|qa|kka)"
CARGO_HINT_BASE_TOKENS = (
    "pochta",
    "pochata",
    "pochatta",
    "pocta",
    "puchta",
    "pushta",
    "posta",
    "posilka",
    "dostavka",
    "kargo",
    "hujjat",
    "yuk",
)
TME_LINK_RE = re.compile(r"(?i)\b(?:https?://)?(?:www\.)?t\.me/([A-Za-z0-9_+/.-]+)")
SERVICE_GATE_KEYWORD_RE = re.compile(
    r"\b(?:guruh(?:da|ga)?\s*yozish\s*uchun|yozish\s*uchun\s*avval|odam\s*qo'?sh(?:ing|ishingiz|ganingizdan)|holatni\s*tekshir(?:ing|ish)|tugmasini\s*bos(?:ing|ib)|a(?:'|`)?zo\s*qo'?sh(?:ing|ishingiz)|group\s*rules?|verification|verify)\b"
)
SERVICE_GATE_CONTEXT_RE = re.compile(
    r"\b(?:kechirasiz|diqqat|ogohlantirish|warning|captcha|tasdiqlang|tekshirish)\b"
)
SERVICE_AD_KEYWORD_RE = re.compile(
    r"\b(?:odam\s*qo'?sh\w*|guruhlar?\s*uchun|guruxlar?\s*uchun|jivoy\s*ayollar|jonli\s*odamlar?|obunachi)\b"
)
HOTEL_AD_KEYWORD_RE = re.compile(
    r"\b(?:mehmonxona|mexmonxona|gostinitsa|gostinica|otel|hotel|hostel|xostel)\b"
)
HOTEL_AD_PRICE_RE = re.compile(
    r"\b\d{2,6}\s*(?:k|ming|sum|uzs|so['`’ʻʼ‘]?m)\b|\b\d{2,6}\s*-\s*\d{2,6}\b"
)
EASY_EARN_PRICE_RE = re.compile(
    r"\b\d{1,3}(?:[.,]\d{3})?(?:[.,]\d+)?\s*(?:k|kk|ming|sum|uzs|so['`’ʻʼ‘]?m)\b"
)
NAKRUTKA_STRONG_RE = re.compile(
    r"\b(?:nakrutka|nakrutkachi|podpischik\w*|obunachi|obuna\s*(?:oshir|kopaytir|ko['`’ʻʼ‘]?paytir)\w*|kanal\s*(?:nakrutka|raskrutka)|subscriber\s*(?:boost|increase))\b"
)
PHONE_SEP_PATTERN = r"[\s\-().,/_\u2010-\u2015\u2212]*"
PHONE_PATTERNS = (
    re.compile(rf"(?:\+?998{PHONE_SEP_PATTERN}\d{{2}}{PHONE_SEP_PATTERN}\d{{3}}{PHONE_SEP_PATTERN}\d{{2}}{PHONE_SEP_PATTERN}\d{{2}})"),
    re.compile(rf"(?:\+?\d(?:{PHONE_SEP_PATTERN}\d){{8,14}})"),
)
PHONE_KEYWORD_RE = re.compile(r"\b(?:tel|telefon|raqam|nomer|номер|phone|kontakt|aloqa)\b")
BLOCK_REF_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{3,})(?![A-Za-z0-9_])")
BLOCK_REF_TME_RE = re.compile(r"(?i)(?:https?://)?(?:www\.)?t\.me/([A-Za-z0-9_]{3,})(?:/\d+)?")
GENERIC_LINK_RE = re.compile(r"(?i)\b(?:https?://|www\.|t\.me/)\S+")
CARGO_BOR_RE = re.compile(
    rf"\b(?:{POCHTA_TOKEN}|posilka|dostavka|kargo|yuk|hujjat)\b[^\n]{{0,20}}\bbor(?:man|miz|mi)?\b|\bbor(?:man|miz|mi)?\b[^\n]{{0,20}}\b(?:{POCHTA_TOKEN}|posilka|dostavka|kargo|yuk|hujjat)\b"
)
OLAMAN_WORD_RE = re.compile(r"\bolaman\b")


def deep_copy(obj: Any) -> Any:
    return json.loads(json.dumps(obj))


def load_json(path: Path, default_value: Any) -> Any:
    if not path.exists():
        data = deep_copy(default_value)
        save_json(path, data)
        return data
    try:
        with path.open("r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        logging.exception("config read error: %s", path)
        return deep_copy(default_value)


def save_json(path: Path, obj: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
    return default


def normalize_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    cfg = deep_copy(DEFAULT_CONFIG)
    try:
        cfg["API_ID"] = int(raw.get("API_ID", cfg["API_ID"]))
    except Exception:
        cfg["API_ID"] = 0
    cfg["API_HASH"] = str(raw.get("API_HASH", cfg["API_HASH"])).strip()
    cfg["SESSION_NAME"] = str(raw.get("SESSION_NAME", cfg["SESSION_NAME"])).strip() or cfg["SESSION_NAME"]

    source = raw.get("SOURCE_GROUPS", raw.get("IN_GROUPS", []))
    if isinstance(source, list):
        cfg["SOURCE_GROUPS"] = [int(x) for x in source if str(x).strip()]

    driver = raw.get("DRIVER_GROUP", raw.get("OUT_GROUP", 0))
    try:
        cfg["DRIVER_GROUP"] = int(driver)
    except Exception:
        cfg["DRIVER_GROUP"] = 0

    cfg["BOT_TOKEN"] = str(raw.get("BOT_TOKEN", cfg["BOT_TOKEN"])).strip()
    cfg["ADMIN_BOT_SESSION"] = (
        str(raw.get("ADMIN_BOT_SESSION", cfg["ADMIN_BOT_SESSION"])).strip() or cfg["ADMIN_BOT_SESSION"]
    )
    cfg["RELAY_ENABLED"] = _to_bool(raw.get("RELAY_ENABLED", cfg["RELAY_ENABLED"]), default=True)
    admins = raw.get("ADMIN_IDS", [])
    if isinstance(admins, list):
        parsed_admins: List[int] = []
        for x in admins:
            try:
                xid = int(x)
            except Exception:
                continue
            if xid not in parsed_admins:
                parsed_admins.append(xid)
        cfg["ADMIN_IDS"] = parsed_admins
    blocked_users = raw.get("AD_BLOCK_USER_IDS", raw.get("BLOCKED_USER_IDS", []))
    if isinstance(blocked_users, list):
        parsed_blocked: List[int] = []
        for x in blocked_users:
            try:
                xid = int(x)
            except Exception:
                continue
            if xid <= 0:
                continue
            if xid not in parsed_blocked:
                parsed_blocked.append(xid)
        cfg["AD_BLOCK_USER_IDS"] = parsed_blocked
    blocked_refs = raw.get("AD_BLOCK_REFS", raw.get("AD_BLOCK_USER_REFS", []))
    if isinstance(blocked_refs, list):
        parsed_refs: List[str] = []
        for x in blocked_refs:
            ref = _normalize_ad_block_ref(str(x))
            if not ref:
                continue
            if ref not in parsed_refs:
                parsed_refs.append(ref)
        cfg["AD_BLOCK_REFS"] = parsed_refs
    blocked_chats = raw.get("BLOCKED_CHAT_IDS", raw.get("BLOCK_CHAT_IDS", []))
    if isinstance(blocked_chats, list):
        parsed_blocked_chats: List[int] = []
        for x in blocked_chats:
            try:
                xid = int(x)
            except Exception:
                continue
            if xid == 0:
                continue
            if xid not in parsed_blocked_chats:
                parsed_blocked_chats.append(xid)
        cfg["BLOCKED_CHAT_IDS"] = parsed_blocked_chats

    return cfg


def load_runtime() -> None:
    global CONFIG
    raw = load_json(CONFIG_PATH, DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        raw = deep_copy(DEFAULT_CONFIG)
    normalized = normalize_config(raw)
    CONFIG = normalized
    if normalized != raw:
        save_runtime()


def save_runtime() -> None:
    save_json(CONFIG_PATH, CONFIG)


def source_groups() -> List[int]:
    return [int(x) for x in CONFIG.get("SOURCE_GROUPS", [])]


def set_source_groups(groups: List[int]) -> None:
    CONFIG["SOURCE_GROUPS"] = [int(x) for x in groups]
    save_runtime()


def add_source_group(chat_id: int) -> bool:
    groups = source_groups()
    cid = int(chat_id)
    if cid in groups:
        return False
    groups.append(cid)
    set_source_groups(groups)
    return True


def remove_source_group(chat_id: int) -> bool:
    groups = source_groups()
    cid = int(chat_id)
    if cid not in groups:
        return False
    groups = [x for x in groups if x != cid]
    set_source_groups(groups)
    return True


def driver_group() -> int:
    return int(CONFIG.get("DRIVER_GROUP", 0))


def set_driver_group(chat_id: int) -> None:
    CONFIG["DRIVER_GROUP"] = int(chat_id)
    save_runtime()


def api_id() -> int:
    return int(CONFIG.get("API_ID", 0))


def api_hash() -> str:
    return str(CONFIG.get("API_HASH", "")).strip()


def session_name() -> str:
    val = str(CONFIG.get("SESSION_NAME", "userbot_session")).strip()
    return val or "userbot_session"


def bot_token() -> str:
    return str(CONFIG.get("BOT_TOKEN", "")).strip()


def admin_ids() -> List[int]:
    return [int(x) for x in CONFIG.get("ADMIN_IDS", [])]


def _normalize_ad_block_ref(value: str) -> Optional[str]:
    raw = (value or "").strip().strip("<>").strip(",:;.")
    if not raw:
        return None
    token = raw.split("?", 1)[0].split("#", 1)[0].strip()
    lowered = token.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        m = re.match(r"(?i)^https?://(?:www\.)?t\.me/(.+)$", token)
        if not m:
            return None
        token = m.group(1).strip("/")
    elif lowered.startswith("t.me/"):
        token = token[5:].strip("/")

    if "/" in token:
        token = token.split("/", 1)[0].strip()
    if token.startswith("@"):
        token = token[1:].strip()
    if not re.fullmatch(r"[A-Za-z0-9_]{3,}", token):
        return None
    return f"@{token.lower()}"


def ad_block_refs() -> List[str]:
    raw = CONFIG.get("AD_BLOCK_REFS", [])
    if not isinstance(raw, list):
        return []
    parsed: List[str] = []
    for x in raw:
        ref = _normalize_ad_block_ref(str(x))
        if not ref:
            continue
        if ref not in parsed:
            parsed.append(ref)
    return parsed


def add_ad_block_ref(ref_value: str) -> bool:
    ref = _normalize_ad_block_ref(ref_value)
    if not ref:
        return False
    refs = ad_block_refs()
    if ref in refs:
        return False
    refs.append(ref)
    CONFIG["AD_BLOCK_REFS"] = refs
    save_runtime()
    return True


def remove_ad_block_ref(ref_value: str) -> bool:
    ref = _normalize_ad_block_ref(ref_value)
    if not ref:
        return False
    refs = ad_block_refs()
    if ref not in refs:
        return False
    refs = [x for x in refs if x != ref]
    CONFIG["AD_BLOCK_REFS"] = refs
    save_runtime()
    return True


def ad_block_user_ids() -> List[int]:
    raw = CONFIG.get("AD_BLOCK_USER_IDS", [])
    if not isinstance(raw, list):
        return []
    parsed: List[int] = []
    for x in raw:
        try:
            xid = int(x)
        except Exception:
            continue
        if xid <= 0:
            continue
        if xid not in parsed:
            parsed.append(xid)
    return parsed


def is_ad_blocked_user(user_id: int) -> bool:
    uid = int(user_id or 0)
    return uid > 0 and uid in ad_block_user_ids()


def add_ad_block_user(user_id: int) -> bool:
    uid = int(user_id or 0)
    if uid <= 0:
        return False
    blocked = ad_block_user_ids()
    if uid in blocked:
        return False
    blocked.append(uid)
    CONFIG["AD_BLOCK_USER_IDS"] = blocked
    save_runtime()
    return True


def extract_sender_user_id(message: Any) -> int:
    from_id = getattr(message, "from_id", None)
    if from_id is not None:
        cls_name = from_id.__class__.__name__.lower()
        if "peeruser" in cls_name:
            uid = int(getattr(from_id, "user_id", 0) or 0)
            return uid if uid > 0 else 0
        return 0

    sender = getattr(message, "sender", None)
    if sender is not None:
        cls_name = sender.__class__.__name__.lower()
        if "user" in cls_name:
            uid = int(getattr(sender, "id", 0) or 0)
            return uid if uid > 0 else 0
        return 0

    uid = int(getattr(message, "sender_id", 0) or 0)
    if uid > 0 and not bool(getattr(message, "post", False)):
        return uid
    return 0


def _cache_bot_sender(sender_id: int, is_bot: bool) -> None:
    sid = int(sender_id or 0)
    if sid <= 0:
        return
    BOT_SENDER_CACHE[sid] = bool(is_bot)
    while len(BOT_SENDER_CACHE) > BOT_CACHE_LIMIT:
        oldest_key = next(iter(BOT_SENDER_CACHE))
        BOT_SENDER_CACHE.pop(oldest_key, None)


async def is_bot_message(message: Any) -> bool:
    via_bot_id = int(getattr(message, "via_bot_id", 0) or 0)
    if via_bot_id > 0:
        return True

    sender_id = int(getattr(message, "sender_id", 0) or 0)
    cached = BOT_SENDER_CACHE.get(sender_id) if sender_id > 0 else None
    if cached is True:
        return True

    sender = getattr(message, "sender", None)
    if sender is not None:
        is_bot_sender = bool(getattr(sender, "bot", False))
        resolved_sender_id = int(getattr(sender, "id", 0) or sender_id or 0)
        _cache_bot_sender(resolved_sender_id, is_bot_sender)
        if is_bot_sender:
            return True

    if cached is False or sender_id <= 0:
        return False

    try:
        sender = await asyncio.wait_for(message.get_sender(), timeout=0.35)
    except Exception:
        return False
    is_bot_sender = bool(getattr(sender, "bot", False))
    resolved_sender_id = int(getattr(sender, "id", 0) or sender_id or 0)
    _cache_bot_sender(resolved_sender_id, is_bot_sender)
    return is_bot_sender


def _prune_recent_messages(now_ts: float) -> None:
    expire_before = now_ts - DUPLICATE_WINDOW_SECONDS
    stale = [key for key, seen_ts in RECENT_MESSAGE_CACHE.items() if seen_ts <= expire_before]
    for key in stale:
        RECENT_MESSAGE_CACHE.pop(key, None)

    expire_before_text = now_ts - DUPLICATE_TEXT_ONLY_WINDOW_SECONDS
    stale_text = [key for key, seen_ts in RECENT_TEXT_ONLY_CACHE.items() if seen_ts <= expire_before_text]
    for key in stale_text:
        RECENT_TEXT_ONLY_CACHE.pop(key, None)

    while len(RECENT_MESSAGE_CACHE) > DUPLICATE_CACHE_LIMIT:
        oldest_key = next(iter(RECENT_MESSAGE_CACHE))
        RECENT_MESSAGE_CACHE.pop(oldest_key, None)
    while len(RECENT_TEXT_ONLY_CACHE) > DUPLICATE_CACHE_LIMIT:
        oldest_key = next(iter(RECENT_TEXT_ONLY_CACHE))
        RECENT_TEXT_ONLY_CACHE.pop(oldest_key, None)


def is_duplicate_message(chat_id: int, sender_user_id: int, normalized_text: str) -> bool:
    text = (normalized_text or "").strip()
    cid = int(chat_id or 0)
    if cid == 0 or not text:
        return False
    sender_id = int(sender_user_id or 0)
    if sender_id < 0:
        sender_id = 0
    now_ts = time.monotonic()
    _prune_recent_messages(now_ts)
    fingerprint_sender = f"{cid}|{sender_id}|{text}"
    fingerprint_text_only = f"{cid}|{text}"

    seen_sender = fingerprint_sender in RECENT_MESSAGE_CACHE
    seen_text_only = fingerprint_text_only in RECENT_TEXT_ONLY_CACHE

    RECENT_MESSAGE_CACHE[fingerprint_sender] = now_ts
    RECENT_TEXT_ONLY_CACHE[fingerprint_text_only] = now_ts
    return seen_sender or seen_text_only


def resolve_event_chat_id(event: Any) -> int:
    try:
        chat_id = int(getattr(event, "chat_id", 0) or 0)
    except Exception:
        chat_id = 0
    if chat_id != 0:
        return chat_id
    message = getattr(event, "message", None)
    if message is None:
        return 0
    try:
        return int(getattr(message, "chat_id", 0) or 0)
    except Exception:
        return 0


def remove_ad_block_user(user_id: int) -> bool:
    uid = int(user_id or 0)
    if uid <= 0:
        return False
    blocked = ad_block_user_ids()
    if uid not in blocked:
        return False
    blocked = [x for x in blocked if x != uid]
    CONFIG["AD_BLOCK_USER_IDS"] = blocked
    save_runtime()
    return True


def blocked_chat_ids() -> List[int]:
    raw = CONFIG.get("BLOCKED_CHAT_IDS", [])
    if not isinstance(raw, list):
        return []
    parsed: List[int] = []
    for x in raw:
        try:
            xid = int(x)
        except Exception:
            continue
        if xid == 0:
            continue
        if xid not in parsed:
            parsed.append(xid)
    return parsed


def is_blocked_chat(chat_id: int) -> bool:
    cid = int(chat_id or 0)
    if cid == 0:
        return False
    return any(is_same_chat_id(blocked_cid, cid) for blocked_cid in blocked_chat_ids())


def add_blocked_chat(chat_id: int) -> bool:
    cid = int(chat_id or 0)
    if cid == 0:
        return False
    blocked = blocked_chat_ids()
    if any(is_same_chat_id(existing_cid, cid) for existing_cid in blocked):
        return False
    blocked.append(cid)
    CONFIG["BLOCKED_CHAT_IDS"] = blocked
    save_runtime()
    return True


def remove_blocked_chat(chat_id: int) -> bool:
    cid = int(chat_id or 0)
    if cid == 0:
        return False
    blocked = blocked_chat_ids()
    updated = [existing_cid for existing_cid in blocked if not is_same_chat_id(existing_cid, cid)]
    if len(updated) == len(blocked):
        return False
    CONFIG["BLOCKED_CHAT_IDS"] = updated
    save_runtime()
    return True


def is_admin_user(user_id: int) -> bool:
    uid = int(user_id)
    return uid in admin_ids()


def relay_enabled() -> bool:
    return bool(CONFIG.get("RELAY_ENABLED", True))


def set_relay_enabled(enabled: bool) -> None:
    CONFIG["RELAY_ENABLED"] = bool(enabled)
    save_runtime()


def admin_bot_session_name() -> str:
    val = str(CONFIG.get("ADMIN_BOT_SESSION", "admin_control_bot")).strip()
    return val or "admin_control_bot"


def runtime_stats_snapshot() -> Dict[str, int]:
    return {k: int(v) for k, v in RUNTIME_STATS.items()}


def admin_panel_hooks() -> Dict[str, Any]:
    return {
        "is_admin": is_admin_user,
        "relay_enabled": relay_enabled,
        "set_relay_enabled": set_relay_enabled,
        "source_groups": source_groups,
        "add_source_group": add_source_group,
        "remove_source_group": remove_source_group,
        "driver_group": driver_group,
        "set_driver_group": set_driver_group,
        "blocked_chats": blocked_chat_ids,
        "add_blocked_chat": add_blocked_chat,
        "remove_blocked_chat": remove_blocked_chat,
        "ad_block_user_ids": ad_block_user_ids,
        "add_ad_block_user": add_ad_block_user,
        "remove_ad_block_user": remove_ad_block_user,
        "ad_block_refs": ad_block_refs,
        "add_ad_block_ref": add_ad_block_ref,
        "remove_ad_block_ref": remove_ad_block_ref,
        "estimate_text_tokens": estimate_text_token_count,
        "ai_token_usage": calculate_ai_token_usage,
        "reload_runtime": load_runtime,
        "filter_version": lambda: FILTER_RULESET_VERSION,
        "stats": runtime_stats_snapshot,
    }


def merged_markers(base: List[str], dynamic_key: str) -> List[str]:
    return base


def _transliterate_cyrillic(text: str) -> str:
    return (text or "").translate(CYRILLIC_TRANSLIT_TABLE)


@lru_cache(maxsize=200)
def _normalize_text_cached(raw_text: str) -> str:
    prepared = unicodedata.normalize("NFKC", (raw_text or ""))
    prepared = ZERO_WIDTH_RE.sub("", prepared)
    base = _transliterate_cyrillic(prepared.strip().lower())
    normalized = APOSTROPHE_NORMALIZE_RE.sub("'", base)
    return " ".join(normalized.split())


def normalize_text(text: str) -> str:
    return _normalize_text_cached(text or "")


def has_emoji_by_ord(text: str) -> bool:
    for ch in text or "":
        code = ord(ch)
        for start, end in EMOJI_ORD_RANGES:
            if start <= code <= end:
                return True
    return False


def is_driver_poster_style(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    normalized = normalize_text(raw)
    non_empty_lines = [line for line in raw.splitlines() if line.strip()]
    line_count = len(non_empty_lines)
    emoji_count = len(DRIVER_STYLE_EMOJI_RE.findall(raw))
    emoji_banner = bool(DRIVER_STYLE_EMOJI_BANNER_RE.search(raw))
    style_score = 0
    if line_count >= 4:
        style_score += 1
    if emoji_count >= 4:
        style_score += 1
    if emoji_banner:
        style_score += 1

    keyword_hits = count_substring_hits(normalized, DRIVER_STYLE_KEYWORDS)
    if keyword_hits >= 2 and style_score >= 1:
        return True
    if keyword_hits >= 1 and style_score >= 2:
        return True
    return False


def _normalize_fuzzy_token(token: str) -> str:
    t = normalize_text(token)
    t = re.sub(r"[^a-z0-9\u0400-\u04ff'`-]", "", t)
    t = t.replace("-", "")
    t = APOSTROPHE_STRIP_RE.sub("", t)
    t = re.sub(r"(.)\1{2,}", r"\1", t)
    return t


@lru_cache(maxsize=200)
def _tokenize_for_fuzzy_cached(normalized_text: str) -> tuple[str, ...]:
    base = normalized_text
    base = re.sub(r"[^a-z0-9\u0400-\u04ff'`\s-]", " ", base)
    tokens: List[str] = []
    for raw in base.split():
        t = _normalize_fuzzy_token(raw)
        if len(t) >= 2:
            tokens.append(t)
    return tuple(tokens)


def _tokenize_for_fuzzy(text: str = "", normalized: Optional[str] = None) -> List[str]:
    base = normalized if normalized is not None else normalize_text(text)
    return list(_tokenize_for_fuzzy_cached(base))


def _levenshtein_distance_limited(a: str, b: str, max_dist: int) -> int:
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1

    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        row_min = curr[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            val = min(
                prev[j] + 1,
                curr[j - 1] + 1,
                prev[j - 1] + cost,
            )
            curr.append(val)
            if val < row_min:
                row_min = val
        if row_min > max_dist:
            return max_dist + 1
        prev = curr
    return prev[-1]


@lru_cache(maxsize=4096)
def _is_typo_similar(a: str, b: str) -> bool:
    if a == b:
        return True
    la = len(a)
    lb = len(b)
    if la < 3 or lb < 3:
        return False

    if a[0] != b[0]:
        return False
    if min(la, lb) <= 4 and la > 1 and lb > 1 and a[1] != b[1]:
        return False
    max_len = max(la, lb)
    max_dist = 1 if max_len <= 6 else 2
    return _levenshtein_distance_limited(a, b, max_dist) <= max_dist


@lru_cache(maxsize=512)
def _marker_tokens_for_fuzzy(marker: str) -> tuple[str, ...]:
    marker_tokens = [_normalize_fuzzy_token(x) for x in normalize_text(marker).split()]
    marker_tokens = [x for x in marker_tokens if len(x) >= 2]
    return tuple(marker_tokens)


def _fuzzy_marker_hit(text_tokens: List[str], marker: str) -> bool:
    marker_tokens = list(_marker_tokens_for_fuzzy(marker))
    if not marker_tokens or not text_tokens:
        return False
    if len(marker_tokens) > 4:
        return False

    if len(marker_tokens) == 1:
        target = marker_tokens[0]
        for tok in text_tokens:
            if tok == target or _is_typo_similar(tok, target):
                return True
        return False

    size = len(marker_tokens)
    for i in range(0, len(text_tokens) - size + 1):
        ok = True
        for j in range(size):
            left = text_tokens[i + j]
            right = marker_tokens[j]
            if left != right and not _is_typo_similar(left, right):
                ok = False
                break
        if ok:
            return True
    return False


def count_substring_hits(text: str, markers: List[str], fuzzy: bool = False, tokens: Optional[List[str]] = None) -> int:
    hits = 0
    missing: List[str] = []
    for marker in markers:
        if marker in text:
            hits += 1
        elif fuzzy:
            missing.append(marker)

    if not fuzzy or not missing:
        return hits

    fuzzy_tokens = tokens if tokens is not None else _tokenize_for_fuzzy(normalized=text)
    for marker in missing:
        if _fuzzy_marker_hit(fuzzy_tokens, marker):
            hits += 1
    return hits


def count_word_hits(text: str, markers: List[str], fuzzy: bool = False, tokens: Optional[List[str]] = None) -> int:
    hits = 0
    missing: List[str] = []
    for marker in markers:
        pat = r"(?<!\w)" + re.escape(marker).replace(r"\ ", r"\s+") + r"(?!\w)"
        if re.search(pat, text):
            hits += 1
        elif fuzzy:
            missing.append(marker)
    if fuzzy and missing:
        fuzzy_tokens = tokens if tokens is not None else _tokenize_for_fuzzy(normalized=text)
        for marker in missing:
            if _fuzzy_marker_hit(fuzzy_tokens, marker):
                hits += 1
    return hits


def has_driver_force_token(text: str, tokens: Optional[List[str]] = None) -> bool:
    if DRIVER_FORCE_TOKEN_RE.search(text):
        return True
    fuzzy_tokens = tokens if tokens is not None else _tokenize_for_fuzzy(normalized=text)
    for tok in fuzzy_tokens:
        if len(tok) < 4 or len(tok) > 8:
            continue
        for base in DRIVER_FORCE_BASE_TOKENS:
            if tok == base or _is_typo_similar(tok, base):
                return True
    return False


def has_cargo_hint(text: str, tokens: Optional[List[str]] = None) -> bool:
    if HARD_BLOCK_CARGO_RE.search(text) or DRIVER_CARGO_OFFER_RE.search(text):
        return True
    fuzzy_tokens = tokens if tokens is not None else _tokenize_for_fuzzy(normalized=text)
    for tok in fuzzy_tokens:
        if len(tok) < 4:
            continue
        for base in CARGO_HINT_BASE_TOKENS:
            if tok == base or _is_typo_similar(tok, base):
                return True
    return False


def has_driver_seat_or_baggage_offer(text: str) -> bool:
    return bool(DRIVER_NUMERIC_SEAT_OFFER_RE.search(text) or DRIVER_BAGAJ_BOR_RE.search(text))


def has_route_pattern(text: str) -> bool:
    if re.search(rf"\b[\w'`-]{{2,40}}\s+dan\s+[\w'` -]{{2,40}}\s+{DEST_SUFFIX_PATTERN}\b", text):
        return True
    if re.search(rf"\b[\w'`-]{{2,40}}dan\s+[\w'` -]{{2,40}}{DEST_SUFFIX_PATTERN}\b", text):
        return True
    if re.search(rf"\b[\w'`-]{{2,40}}\s+dan\s+[\w'` -]{{2,40}}{DEST_SUFFIX_PATTERN}\b", text):
        return True
    if re.search(r"\b[\w'` -]{2,40}\s*[-/>]+\s*[\w'` -]{2,40}\b", text):
        return True
    return False


def has_people_pattern(text: str) -> bool:
    if PAX_COUNT_DIGIT_RE.search(text) or PAX_COUNT_WORD_RE.search(text):
        return True
    if re.search(r"\b\d{1,2}\s*ta\b", text):
        return True
    for w in WORD_NUMBERS:
        if w.endswith("ta") or w in {"bitta", "bittamiz", "onta", "o'nta"}:
            if re.search(rf"\b{re.escape(w)}\b", text):
                return True
        else:
            if re.search(rf"\b{re.escape(w)}\s*{PEOPLE_UNIT_PATTERN}\b", text):
                return True
    return False


def is_same_chat_id(config_chat_id: int, actual_chat_id: int) -> bool:
    return actual_chat_id in chat_id_candidates(config_chat_id)


def chat_id_candidates(chat_id: int) -> List[int]:
    s = str(chat_id).strip()
    vals: List[int] = []

    def add(v: int) -> None:
        if v not in vals:
            vals.append(v)

    try:
        add(int(s))
    except Exception:
        return vals

    if s.startswith("-100") and s[4:].isdigit():
        add(-int(s[4:]))
    elif s.startswith("-") and s[1:].isdigit():
        add(int(f"-100{s[1:]}"))
    elif s.isdigit():
        add(-int(s))
        add(int(f"-100{s}"))

    return vals


def is_source_chat(chat_id: int) -> bool:
    return any(is_same_chat_id(cid, chat_id) for cid in source_groups())


def has_route_like_tme_link(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    for m in TME_LINK_RE.finditer(raw):
        ref = (m.group(1) or "").strip().strip("/")
        if not ref:
            continue
        if "/" in ref:
            ref = ref.split("/", 1)[0]
        lowered = ref.lower()
        if lowered.startswith("+") or lowered.startswith("joinchat/"):
            continue
        slug = re.sub(r"[^a-z0-9_-]", "", lowered.replace("-", "_"))
        if not slug:
            continue
        parts = [re.sub(r"\d+", "", p) for p in slug.split("_") if p]
        alpha_parts = [p for p in parts if len(p) >= 3]
        if len(alpha_parts) >= 2:
            return True
    return False


def has_destination_hint(text: str) -> bool:
    return bool(re.search(rf"\b[\w'`-]{{3,45}}{DEST_SUFFIX_PATTERN}\b", text))


def is_service_gate_message(text: str = "", normalized: Optional[str] = None) -> bool:
    t = normalized if normalized is not None else normalize_text(text)
    if not t:
        return False
    if SERVICE_GATE_KEYWORD_RE.search(t):
        return True
    if SERVICE_AD_KEYWORD_RE.search(t):
        return True
    if SERVICE_GATE_CONTEXT_RE.search(t) and ("guruh" in t or "kanal" in t or "odam qo" in t):
        return True
    return False


def is_commercial_ad_message(text: str = "", normalized: Optional[str] = None, tokens: Optional[List[str]] = None) -> bool:
    t = normalized if normalized is not None else normalize_text(text)
    if not t:
        return False
    text_tokens = list(tokens) if tokens is not None else _tokenize_for_fuzzy(normalized=t)

    hotel_keyword = bool(HOTEL_AD_KEYWORD_RE.search(t))
    hotel_context_hits = count_substring_hits(t, HOTEL_AD_CONTEXT_MARKERS, tokens=text_tokens)
    hotel_price = bool(HOTEL_AD_PRICE_RE.search(t))
    if hotel_keyword and ((hotel_context_hits >= 2 and hotel_price) or hotel_context_hits >= 4):
        return True

    if NAKRUTKA_STRONG_RE.search(t):
        return True

    nakrutka_hits = count_substring_hits(t, NAKRUTKA_AD_MARKERS, fuzzy=True, tokens=text_tokens)
    promo_hits = count_substring_hits(t, NAKRUTKA_PROMO_MARKERS, tokens=text_tokens)
    if nakrutka_hits >= 3:
        return True
    if nakrutka_hits >= 2 and promo_hits >= 1:
        return True

    easy_earn_hits = count_substring_hits(t, EASY_EARN_SCAM_MARKERS, fuzzy=True, tokens=text_tokens)
    easy_earn_price = bool(EASY_EARN_PRICE_RE.search(t))
    if easy_earn_hits >= 3:
        return True
    if easy_earn_hits >= 2 and easy_earn_price:
        return True
    return False


def is_cargo_request_message(text: str, normalized: Optional[str] = None, tokens: Optional[List[str]] = None) -> bool:
    t = normalized if normalized is not None else normalize_text(text)
    if not t:
        return False
    if is_service_gate_message(normalized=t):
        return False
    if is_commercial_ad_message(normalized=t, tokens=tokens):
        return False
    if NEGATIVE_REQUEST_RE.search(t):
        return False

    text_tokens = list(tokens) if tokens is not None else _tokenize_for_fuzzy(normalized=t)
    token_count = len(text_tokens)
    if token_count <= 1:
        return False
    if not CARGO_REQUEST_WORD_RE.search(t):
        return False

    # Driver cargo offers should stay blocked.
    if DRIVER_CARGO_OFFER_RE.search(t):
        return False
    if DRIVER_SELF_OFFER_RE.search(t) or DRIVER_QUESTION_OFFER_RE.search(t):
        return False
    if EXPLICIT_DRIVER_IDENTITY_RE.search(t):
        return False
    if has_driver_seat_or_baggage_offer(t):
        return False
    if has_driver_force_token(t, tokens=text_tokens):
        return False

    route_hint = has_route_pattern(t) or bool(re.search(r"\b[\w'`-]+dan\b", t)) or has_destination_hint(t)
    intent_hint = bool(CARGO_REQUEST_INTENT_RE.search(t))
    question_hint = bool(re.search(r"\b(?:bor\s*mi|bormi|kim\s*bor|kim\s*bormi)\b", t))

    if route_hint and intent_hint:
        return True
    if question_hint and token_count <= 10:
        return True
    if route_hint and token_count <= 8:
        return True
    return False


def is_passenger_message(text: str, normalized: Optional[str] = None, tokens: Optional[List[str]] = None) -> bool:
    t = normalized if normalized is not None else normalize_text(text)
    if not t:
        return False
    text_tokens = list(tokens) if tokens is not None else _tokenize_for_fuzzy(normalized=t)
    tme_route_hint = has_route_like_tme_link(text)
    if is_service_gate_message(normalized=t):
        return False
    if is_driver_poster_style(text):
        return False
    if DRIVER_OLAMIZ_YURAMIZ_RE.search(t):
        return False
    if has_driver_force_token(t, tokens=text_tokens):
        return False
    if DRIVER_BERIN_CHIQIB_RE.search(t):
        return False
    if has_driver_seat_or_baggage_offer(t):
        return False
    token_count = len(text_tokens)

    if HARD_BLOCK_KAM_RE.search(t):
        return False

    negative_need = bool(NEGATIVE_REQUEST_RE.search(t))
    if negative_need:
        return False

    taxi_hits = count_substring_hits(
        t, merged_markers(PASSENGER_TAXI_MARKERS, "PASSENGER_TAXI_MARKERS"), tokens=text_tokens
    )
    direct_hits = count_substring_hits(
        t, merged_markers(PASSENGER_DIRECT_PHRASES, "PASSENGER_DIRECT_PHRASES"), tokens=text_tokens
    )
    request_hits = count_substring_hits(
        t, merged_markers(PASSENGER_REQUEST_MARKERS, "PASSENGER_REQUEST_MARKERS"), tokens=text_tokens
    )
    short_trip_exact = bool(PAX_SHORT_TRIP_INTENT_RE.search(t)) or (
        count_substring_hits(t, PAX_SHORT_TRIP_INTENT_MARKERS, tokens=text_tokens) >= 1
    )
    short_trip_fuzzy = 0
    if not short_trip_exact and token_count <= 10:
        short_trip_fuzzy = count_substring_hits(t, FUZZY_TRIP_CRITICAL_MARKERS, fuzzy=True, tokens=text_tokens)
    short_trip_intent = short_trip_exact or short_trip_fuzzy >= 1

    pax_words = bool(PAX_WORDS_RE.search(t))
    pax_count = bool(PAX_COUNT_DIGIT_RE.search(t) or PAX_COUNT_WORD_RE.search(t))
    pax_intent = bool(PAX_INTENT_RE.search(t))
    pax_luggage = bool(PAX_LUGGAGE_RE.search(t))
    pax_special = bool(PAX_SPECIAL_RE.search(t))
    cargo_negative = bool(CARGO_NEGATIVE_RE.search(t) or has_cargo_hint(t, tokens=text_tokens))
    driver_offer = bool(DRIVER_SELF_OFFER_RE.search(t) or DRIVER_QUESTION_OFFER_RE.search(t))
    vehicle_need_hint = bool(PAX_VEHICLE_NEED_RE.search(t))

    driver_hits = count_substring_hits(t, merged_markers(DRIVER_MARKERS, "DRIVER_MARKERS"), tokens=text_tokens)
    driver_typo_hits = 0
    if token_count <= 10 and taxi_hits == 0 and request_hits == 0:
        driver_typo_hits = count_substring_hits(t, FUZZY_DRIVER_CRITICAL_MARKERS, fuzzy=True, tokens=text_tokens)
    spam_hits = count_substring_hits(t, merged_markers(SPAM_MARKERS, "SPAM_MARKERS"), tokens=text_tokens)
    passenger_typo_hits = 0
    if token_count <= 10 and taxi_hits == 0 and request_hits == 0:
        passenger_typo_hits = count_substring_hits(
            t, FUZZY_PASSENGER_CRITICAL_MARKERS, fuzzy=True, tokens=text_tokens
        )
    time_hits = count_substring_hits(t, PASSENGER_TIME_MARKERS, tokens=text_tokens)
    location_hits = count_word_hits(
        t, merged_markers(PASSENGER_LOCATION_MARKERS, "PASSENGER_LOCATION_MARKERS"), tokens=text_tokens
    )

    trip_intent_hint = bool(re.search(r"\b(?:ket\w*|bor\w*|jo'n\w*|jon\w*|yo'lga\s*chiq\w*|yolga\s*chiq\w*|chiqib\s*ket\w*)\b", t))
    destination_only_hint = has_destination_hint(t)
    city_trip_hint = destination_only_hint and trip_intent_hint

    route_hint = has_route_pattern(t) or (" dan " in f" {t} " and " ga " in f" {t} ")
    if not route_hint:
        route_hint = bool(re.search(r"\b[\w'`-]+dan\b", t)) and has_destination_hint(t)
    if not route_hint and city_trip_hint:
        route_hint = True
    vehicle_word = bool(VEHICLE_WORD_RE.search(t))
    order_word = bool(DRIVER_ORDER_WORD_RE.search(t))
    people_hint = has_people_pattern(t)
    phone_hint = bool(re.search(r"(?:\+?998[\s\-()]?\d{2}[\s\-()]?\d{3}[\s\-()]?\d{2}[\s\-()]?\d{2})", t))
    strong_driver = any(x in t for x in STRONG_DRIVER_MARKERS)
    travel_anchor = any(
        (
            taxi_hits >= 1,
            direct_hits >= 1,
            vehicle_need_hint,
            route_hint,
            tme_route_hint,
            people_hint,
            pax_words,
            pax_count,
            pax_intent,
            phone_hint,
            pax_luggage,
            pax_special,
            location_hits >= 1,
        )
    )

    if cargo_negative:
        return False
    if (driver_offer or driver_typo_hits >= 1) and not (direct_hits >= 1 and taxi_hits >= 1 and request_hits >= 1):
        return False
    if (
        order_word
        and taxi_hits == 0
        and direct_hits == 0
        and not vehicle_need_hint
        and not route_hint
        and not people_hint
        and not phone_hint
        and not city_trip_hint
    ):
        return False
    if spam_hits >= 1 and taxi_hits == 0 and direct_hits == 0 and not vehicle_need_hint and not route_hint and not city_trip_hint:
        return False
    if request_hits >= 1 and not travel_anchor and not short_trip_intent and not city_trip_hint:
        return False
    if (
        token_count <= 1
        and taxi_hits == 0
        and direct_hits == 0
        and not vehicle_need_hint
        and not route_hint
        and not people_hint
        and not phone_hint
        and request_hits <= 1
    ):
        return False

    score = 0
    score += taxi_hits * 3
    score += direct_hits * 4
    score += request_hits * 2
    score += min(location_hits, 3)
    score += int(route_hint) * 2
    score += int(city_trip_hint) * 2
    score += int(people_hint) * 2
    score += int(phone_hint)
    score += time_hits
    score += int(pax_words) * 2
    score += int(pax_count) * 2
    score += int(pax_intent) * 2
    score += int(pax_luggage)
    score += int(pax_special)
    score += int(short_trip_intent)
    score += short_trip_fuzzy
    score += int(vehicle_need_hint) * 3
    score += passenger_typo_hits * 2

    score -= driver_hits * 3
    score -= driver_typo_hits * 3
    score -= spam_hits * 2

    if strong_driver and score < 8:
        return False
    if spam_hits >= 2 and score < 8:
        return False
    if driver_hits >= 2 and score < 7:
        return False

    if (pax_words or pax_count) and pax_intent and driver_hits == 0:
        return True
    if vehicle_need_hint and driver_hits == 0 and not driver_offer and spam_hits == 0:
        return True
    if (
        short_trip_intent
        and driver_hits == 0
        and not driver_offer
        and spam_hits == 0
        and not cargo_negative
        and token_count >= 2
        and (route_hint or people_hint or taxi_hits >= 1 or request_hits >= 1 or vehicle_need_hint or phone_hint)
    ):
        return True
    if city_trip_hint and not driver_offer and spam_hits == 0:
        return True
    if tme_route_hint and driver_hits == 0 and not driver_offer and not cargo_negative and spam_hits == 0:
        return True
    if (pax_luggage or pax_special) and (pax_words or pax_count) and (pax_intent or taxi_hits >= 1 or request_hits >= 1):
        return True
    if direct_hits >= 1 and (taxi_hits >= 1 or request_hits >= 1 or route_hint or people_hint or phone_hint):
        return True
    if taxi_hits >= 1 and (request_hits >= 1 or route_hint or people_hint or location_hits >= 1):
        return True
    if destination_only_hint and people_hint and driver_hits == 0 and not driver_offer and not cargo_negative:
        return True
    if destination_only_hint and request_hits >= 1 and (people_hint or taxi_hits >= 1 or vehicle_word):
        return True
    if request_hits >= 2 and (route_hint or people_hint or taxi_hits >= 1):
        return True
    if route_hint and (people_hint or phone_hint) and driver_hits == 0:
        return True
    if score >= 5 and (travel_anchor or short_trip_intent or city_trip_hint):
        return True
    return False


def is_passenger_candidate(text: str, normalized: Optional[str] = None, tokens: Optional[List[str]] = None) -> bool:
    # Recall-first fallback for short/noisy messages that are likely passenger requests.
    t = normalized if normalized is not None else normalize_text(text)
    if not t:
        return False
    if is_service_gate_message(normalized=t):
        return False
    if is_driver_poster_style(text):
        return False
    if has_driver_seat_or_baggage_offer(t):
        return False

    text_tokens = list(tokens) if tokens is not None else _tokenize_for_fuzzy(normalized=t)
    tme_route_hint = has_route_like_tme_link(text)
    short_msg = len(text_tokens) <= 6
    trip_hits = count_substring_hits(t, PAX_SHORT_TRIP_INTENT_MARKERS, tokens=text_tokens)
    if trip_hits == 0 and short_msg:
        trip_hits = count_substring_hits(t, FUZZY_TRIP_CRITICAL_MARKERS, fuzzy=True, tokens=text_tokens)
    taxi_hits = count_substring_hits(
        t, merged_markers(PASSENGER_TAXI_MARKERS, "PASSENGER_TAXI_MARKERS"), tokens=text_tokens
    )
    request_hits = count_substring_hits(
        t, merged_markers(PASSENGER_REQUEST_MARKERS, "PASSENGER_REQUEST_MARKERS"), tokens=text_tokens
    )
    direct_hits = count_substring_hits(
        t, merged_markers(PASSENGER_DIRECT_PHRASES, "PASSENGER_DIRECT_PHRASES"), tokens=text_tokens
    )
    fuzzy_core_hits = (
        count_substring_hits(t, FUZZY_PASSENGER_CRITICAL_MARKERS, fuzzy=True, tokens=text_tokens) if short_msg else 0
    )
    spam_hits = count_substring_hits(t, merged_markers(SPAM_MARKERS, "SPAM_MARKERS"), tokens=text_tokens)
    people_hint = has_people_pattern(t)
    pax_words = bool(PAX_WORDS_RE.search(t))
    destination_hint = bool(re.search(r"\b[\w'`-]{3,45}ga\b", t))
    question_hint = ("?" in t) or bool(re.search(r"\b(?:bormi|bor\s*mi|kim\s*bor|kim\s*bormi)\b", t))
    vehicle_word = bool(VEHICLE_WORD_RE.search(t))
    cargo_negative = bool(CARGO_NEGATIVE_RE.search(t) or has_cargo_hint(t, tokens=text_tokens))
    negative_need = bool(NEGATIVE_REQUEST_RE.search(t))
    driver_offer = bool(DRIVER_SELF_OFFER_RE.search(t) or DRIVER_QUESTION_OFFER_RE.search(t))

    if cargo_negative or driver_offer or negative_need:
        return False
    if spam_hits >= 1 and taxi_hits == 0 and request_hits == 0 and trip_hits == 0 and not destination_hint and not people_hint:
        return False
    if vehicle_word and question_hint:
        return True
    if tme_route_hint and not driver_offer and spam_hits == 0:
        return True

    if taxi_hits >= 1 and request_hits >= 1:
        return True
    if fuzzy_core_hits >= 2 and short_msg and (taxi_hits >= 1 or request_hits >= 1 or trip_hits >= 1 or destination_hint or people_hint or question_hint):
        return True
    if trip_hits >= 1 and (destination_hint or short_msg):
        return True
    if request_hits >= 1 and (pax_words or people_hint or destination_hint):
        return True
    if direct_hits >= 1:
        return True
    return False


def is_fast_passenger_message(text: str, normalized: Optional[str] = None, tokens: Optional[List[str]] = None) -> bool:
    t = normalized if normalized is not None else normalize_text(text)
    if not t:
        return False
    if is_service_gate_message(normalized=t):
        return False
    text_tokens = list(tokens) if tokens is not None else _tokenize_for_fuzzy(normalized=t)
    tme_route_hint = has_route_like_tme_link(text)
    if is_driver_poster_style(text):
        return False
    if has_driver_force_token(t, tokens=text_tokens):
        return False
    if DRIVER_BERIN_CHIQIB_RE.search(t):
        return False

    token_count = len(text_tokens)
    if token_count <= 1 and not VEHICLE_WORD_RE.search(t):
        return False
    if NEGATIVE_REQUEST_RE.search(t):
        return False
    if HARD_BLOCK_KAM_RE.search(t):
        return False
    if has_driver_seat_or_baggage_offer(t):
        return False
    if has_cargo_hint(t, tokens=text_tokens):
        return False

    taxi_hits = count_substring_hits(
        t, merged_markers(PASSENGER_TAXI_MARKERS, "PASSENGER_TAXI_MARKERS"), tokens=text_tokens
    )
    request_hits = count_substring_hits(
        t, merged_markers(PASSENGER_REQUEST_MARKERS, "PASSENGER_REQUEST_MARKERS"), tokens=text_tokens
    )
    direct_hits = count_substring_hits(
        t, merged_markers(PASSENGER_DIRECT_PHRASES, "PASSENGER_DIRECT_PHRASES"), tokens=text_tokens
    )
    trip_hits = count_substring_hits(t, PAX_SHORT_TRIP_INTENT_MARKERS, tokens=text_tokens)
    if trip_hits == 0 and token_count <= 8:
        trip_hits = count_substring_hits(t, FUZZY_TRIP_CRITICAL_MARKERS, fuzzy=True, tokens=text_tokens)
    people_hint = has_people_pattern(t)
    route_hint = has_route_pattern(t) or (
        bool(re.search(r"\b[\w'`-]+dan\b", t)) and has_destination_hint(t)
    )
    destination_hint = has_destination_hint(t)
    question_hint = ("?" in text) or bool(FAST_PASSENGER_QUESTION_RE.search(t))
    vehicle_word = bool(VEHICLE_WORD_RE.search(t))
    vehicle_need_hint = bool(PAX_VEHICLE_NEED_RE.search(t))
    pax_intent = bool(PAX_INTENT_RE.search(t))
    phone_hint = bool(re.search(r"(?:\+?998[\s\-()]?\d{2}[\s\-()]?\d{3}[\s\-()]?\d{2}[\s\-()]?\d{2})", t))
    spam_hits = count_substring_hits(t, merged_markers(SPAM_MARKERS, "SPAM_MARKERS"), tokens=text_tokens)
    explicit_driver = bool(EXPLICIT_DRIVER_IDENTITY_RE.search(t))
    driver_offer = bool(DRIVER_SELF_OFFER_RE.search(t) or DRIVER_QUESTION_OFFER_RE.search(t))

    if (
        spam_hits >= 1
        and taxi_hits == 0
        and request_hits == 0
        and trip_hits == 0
        and not route_hint
        and not people_hint
        and not tme_route_hint
    ):
        return False
    if explicit_driver and taxi_hits == 0:
        return False
    if driver_offer and direct_hits == 0 and not (taxi_hits >= 1 and request_hits >= 1):
        return False

    if direct_hits >= 1:
        return True
    if tme_route_hint and not explicit_driver and not driver_offer and spam_hits == 0:
        return True
    if pax_intent and bool(PAX_WORDS_RE.search(t)) and (route_hint or destination_hint) and not explicit_driver and spam_hits == 0:
        return True
    if taxi_hits >= 1 and (
        request_hits >= 1 or vehicle_need_hint or question_hint or route_hint or people_hint or trip_hits >= 1 or phone_hint
    ):
        return True
    if vehicle_need_hint and (taxi_hits >= 1 or vehicle_word) and (
        destination_hint or route_hint or question_hint or trip_hits >= 1 or people_hint or phone_hint or token_count <= 6
    ):
        return True
    if request_hits >= 1 and vehicle_word and (route_hint or people_hint or destination_hint or question_hint or trip_hits >= 1):
        return True
    if trip_hits >= 1 and (destination_hint or people_hint or route_hint or taxi_hits >= 1 or request_hits >= 1):
        return True
    if route_hint and (people_hint or taxi_hits >= 1 or request_hits >= 1 or vehicle_word):
        return True
    if people_hint and (taxi_hits >= 1 or request_hits >= 1 or trip_hits >= 1 or phone_hint or pax_intent):
        return True
    if phone_hint and (taxi_hits >= 1 or request_hits >= 1 or trip_hits >= 1):
        return True
    return False


def _passenger_signal_snapshot(text: str, normalized: str, tokens: List[str]) -> Dict[str, Any]:
    token_count = len(tokens)
    taxi_hits = count_substring_hits(
        normalized,
        merged_markers(PASSENGER_TAXI_MARKERS, "PASSENGER_TAXI_MARKERS"),
        tokens=tokens,
    )
    request_hits = count_substring_hits(
        normalized,
        merged_markers(PASSENGER_REQUEST_MARKERS, "PASSENGER_REQUEST_MARKERS"),
        tokens=tokens,
    )
    direct_hits = count_substring_hits(
        normalized,
        merged_markers(PASSENGER_DIRECT_PHRASES, "PASSENGER_DIRECT_PHRASES"),
        tokens=tokens,
    )
    trip_hits = count_substring_hits(normalized, PAX_SHORT_TRIP_INTENT_MARKERS, tokens=tokens)
    if trip_hits == 0 and token_count <= 8:
        trip_hits = count_substring_hits(normalized, FUZZY_TRIP_CRITICAL_MARKERS, fuzzy=True, tokens=tokens)

    destination_hint = has_destination_hint(normalized)
    route_hint = has_route_pattern(normalized) or (
        bool(re.search(r"\b[\w'`-]+dan\b", normalized)) and destination_hint
    )
    people_hint = has_people_pattern(normalized)
    question_hint = ("?" in text) or bool(FAST_PASSENGER_QUESTION_RE.search(normalized))
    vehicle_word = bool(VEHICLE_WORD_RE.search(normalized))
    vehicle_need_hint = bool(PAX_VEHICLE_NEED_RE.search(normalized))
    pax_words = bool(PAX_WORDS_RE.search(normalized))
    tme_route_hint = has_route_like_tme_link(text)
    passenger_typo_hits = (
        count_substring_hits(normalized, FUZZY_PASSENGER_CRITICAL_MARKERS, fuzzy=True, tokens=tokens)
        if token_count <= 8
        else 0
    )

    return {
        "token_count": token_count,
        "taxi_hits": taxi_hits,
        "request_hits": request_hits,
        "direct_hits": direct_hits,
        "trip_hits": trip_hits,
        "destination_hint": destination_hint,
        "route_hint": route_hint,
        "people_hint": people_hint,
        "question_hint": question_hint,
        "vehicle_word": vehicle_word,
        "vehicle_need_hint": vehicle_need_hint,
        "pax_words": pax_words,
        "tme_route_hint": tme_route_hint,
        "passenger_typo_hits": passenger_typo_hits,
    }


def _is_passenger_possible(snapshot: Dict[str, Any]) -> bool:
    taxi_hits = int(snapshot.get("taxi_hits", 0))
    request_hits = int(snapshot.get("request_hits", 0))
    direct_hits = int(snapshot.get("direct_hits", 0))
    trip_hits = int(snapshot.get("trip_hits", 0))
    passenger_typo_hits = int(snapshot.get("passenger_typo_hits", 0))
    route_hint = bool(snapshot.get("route_hint", False))
    destination_hint = bool(snapshot.get("destination_hint", False))
    people_hint = bool(snapshot.get("people_hint", False))
    question_hint = bool(snapshot.get("question_hint", False))
    vehicle_need_hint = bool(snapshot.get("vehicle_need_hint", False))
    pax_words = bool(snapshot.get("pax_words", False))
    tme_route_hint = bool(snapshot.get("tme_route_hint", False))

    if direct_hits >= 1:
        return True
    if taxi_hits >= 1 and (request_hits >= 1 or route_hint or people_hint or vehicle_need_hint or question_hint):
        return True
    if vehicle_need_hint and (request_hits >= 1 or route_hint or people_hint or question_hint):
        return True
    if request_hits >= 2 and (route_hint or destination_hint or people_hint or pax_words):
        return True
    if trip_hits >= 1 and (route_hint or destination_hint or people_hint or taxi_hits >= 1 or request_hits >= 1):
        return True
    if route_hint and (taxi_hits >= 1 or request_hits >= 1 or people_hint or pax_words):
        return True
    if tme_route_hint and (taxi_hits >= 1 or request_hits >= 1 or vehicle_need_hint):
        return True
    if passenger_typo_hits >= 2 and (question_hint or taxi_hits >= 1 or request_hits >= 1):
        return True
    return False


def _is_passenger_strong(snapshot: Dict[str, Any]) -> bool:
    taxi_hits = int(snapshot.get("taxi_hits", 0))
    request_hits = int(snapshot.get("request_hits", 0))
    direct_hits = int(snapshot.get("direct_hits", 0))
    trip_hits = int(snapshot.get("trip_hits", 0))
    route_hint = bool(snapshot.get("route_hint", False))
    destination_hint = bool(snapshot.get("destination_hint", False))
    people_hint = bool(snapshot.get("people_hint", False))
    question_hint = bool(snapshot.get("question_hint", False))
    vehicle_need_hint = bool(snapshot.get("vehicle_need_hint", False))
    pax_words = bool(snapshot.get("pax_words", False))

    if direct_hits >= 1 and (taxi_hits >= 1 or request_hits >= 1 or route_hint or people_hint):
        return True
    if taxi_hits >= 1 and request_hits >= 1 and (
        route_hint or destination_hint or people_hint or vehicle_need_hint or question_hint
    ):
        return True
    if route_hint and people_hint and (taxi_hits >= 1 or request_hits >= 1 or vehicle_need_hint):
        return True
    if vehicle_need_hint and taxi_hits >= 1 and (route_hint or people_hint or request_hits >= 1):
        return True
    if pax_words and trip_hits >= 1 and (route_hint or destination_hint):
        return True
    return False


def classify_passenger_pipeline(text: str, normalized: Optional[str] = None, tokens: Optional[List[str]] = None) -> Dict[str, str]:
    raw = (text or "").strip()
    if not raw:
        return {"decision": "BLOCK", "reason": "empty-text"}

    t = normalized if normalized is not None else normalize_text(raw)
    if HARD_BLOCK_KAM_RE.search(t):
        return {"decision": "BLOCK", "reason": "kam-driver-offer"}
    text_tokens = list(tokens) if tokens is not None else _tokenize_for_fuzzy(normalized=t)
    snapshot = _passenger_signal_snapshot(raw, t, text_tokens)

    passenger_possible = _is_passenger_possible(snapshot)
    passenger_strong = _is_passenger_strong(snapshot)
    token_count = int(snapshot.get("token_count", 0))
    spam_hits = count_substring_hits(t, merged_markers(SPAM_MARKERS, "SPAM_MARKERS"), tokens=text_tokens)
    taxi_hit_count = int(snapshot.get("taxi_hits", 0))
    request_hit_count = int(snapshot.get("request_hits", 0))
    direct_hit_count = int(snapshot.get("direct_hits", 0))
    people_hint = bool(snapshot.get("people_hint", False))
    vehicle_word = bool(snapshot.get("vehicle_word", False))
    vehicle_need_hint = bool(snapshot.get("vehicle_need_hint", False))
    question_hint = bool(snapshot.get("question_hint", False))
    passenger_typo_hits = int(snapshot.get("passenger_typo_hits", 0))
    request_context_hint = request_hit_count > 0 and any(
        (
            taxi_hit_count > 0,
            direct_hit_count > 0,
            people_hint,
            vehicle_need_hint,
            vehicle_word,
        )
    )
    weak_hint = any(
        (
            taxi_hit_count > 0,
            direct_hit_count > 0,
            people_hint,
            vehicle_need_hint,
            request_context_hint,
        )
    )
    if question_hint and (taxi_hit_count > 0 or direct_hit_count > 0 or vehicle_need_hint or vehicle_word):
        weak_hint = True
    if passenger_typo_hits >= 2 and (taxi_hit_count > 0 or request_context_hint or question_hint):
        weak_hint = True

    # 1) passenger bo'lishi mumkin bo'lganlarni qoldirish
    if not passenger_possible:
        if GENERIC_LINK_RE.search(raw):
            return {"decision": "BLOCK", "reason": "contains-link"}
        if CARGO_BOR_RE.search(t):
            return {"decision": "BLOCK", "reason": "cargo-posilka-offer"}
        if OLAMAN_WORD_RE.search(t):
            return {"decision": "BLOCK", "reason": "olaman-driver-offer"}
        if is_service_gate_message(normalized=t):
            return {"decision": "BLOCK", "reason": "service-gate"}
        if is_commercial_ad_message(normalized=t, tokens=text_tokens):
            return {"decision": "BLOCK", "reason": "commercial-ad"}
        if spam_hits >= 1:
            return {"decision": "BLOCK", "reason": "spam-marker"}
        if weak_hint and token_count <= 4:
            return {"decision": "REVIEW", "reason": "passenger-low-info"}
        return {"decision": "BLOCK", "reason": "not-passenger"}

    # 2) emoji tekshiruv
    emoji_count = len(DRIVER_STYLE_EMOJI_RE.findall(raw))
    emoji_banner = bool(DRIVER_STYLE_EMOJI_BANNER_RE.search(raw))
    if emoji_banner and not passenger_strong:
        return {"decision": "BLOCK", "reason": "emoji-banner"}
    if emoji_count >= 3 and token_count <= 4 and not passenger_strong:
        return {"decision": "REVIEW", "reason": "emoji-low-info"}

    # 3) driver-offerlarni chiqarib tashlash
    explicit_driver_offer = bool(
        EXPLICIT_DRIVER_IDENTITY_RE.search(t)
        or DRIVER_OLAMIZ_YURAMIZ_RE.search(t)
        or DRIVER_BERIN_CHIQIB_RE.search(t)
        or has_driver_seat_or_baggage_offer(t)
    )
    driver_offer = bool(
        DRIVER_SELF_OFFER_RE.search(t)
        or DRIVER_QUESTION_OFFER_RE.search(t)
        or DRIVER_CLEAR_INTENT_RE.search(t)
        or has_driver_force_token(t, tokens=text_tokens)
    )
    has_olaman = bool(OLAMAN_WORD_RE.search(t))

    if explicit_driver_offer:
        return {"decision": "BLOCK", "reason": "driver-explicit-offer"}
    if driver_offer:
        if has_olaman and passenger_strong:
            return {"decision": "REVIEW", "reason": "olaman-mixed-review"}
        if has_olaman:
            return {"decision": "BLOCK", "reason": "olaman-driver-offer"}
        if passenger_strong and int(snapshot.get("direct_hits", 0)) >= 1 and int(snapshot.get("request_hits", 0)) >= 1:
            return {"decision": "REVIEW", "reason": "driver-passenger-mixed-review"}
        return {"decision": "BLOCK", "reason": "driver-offer"}

    # 3.1) posilka/yuk bor
    cargo_offer = bool(DRIVER_CARGO_OFFER_RE.search(t) or CARGO_BOR_RE.search(t))
    if cargo_offer:
        if passenger_strong and bool(snapshot.get("vehicle_need_hint", False)):
            return {"decision": "REVIEW", "reason": "cargo-mixed-review"}
        return {"decision": "BLOCK", "reason": "cargo-posilka-offer"}

    # 4) reklama/scam
    if GENERIC_LINK_RE.search(raw):
        return {"decision": "BLOCK", "reason": "contains-link"}
    if is_service_gate_message(normalized=t):
        return {"decision": "BLOCK", "reason": "service-gate"}
    if is_commercial_ad_message(normalized=t, tokens=text_tokens):
        return {"decision": "BLOCK", "reason": "commercial-ad"}
    if spam_hits >= 1 and not passenger_strong:
        return {"decision": "BLOCK", "reason": "spam-marker"}

    # 5) low-info => REVIEW
    low_info = token_count <= 4 and not (
        bool(snapshot.get("route_hint", False))
        or bool(snapshot.get("people_hint", False))
        or int(snapshot.get("direct_hits", 0)) >= 1
        or int(snapshot.get("request_hits", 0)) >= 2
    )
    if low_info:
        return {"decision": "REVIEW", "reason": "low-info-review"}

    # 6) best-case
    if passenger_strong:
        return {"decision": "ALLOW", "reason": "best-case-allow"}
    return {"decision": "REVIEW", "reason": "passenger-possible-review"}


def is_fast_driver_block_message(text: str) -> bool:
    if has_emoji_by_ord(text):
        return True
    if is_driver_poster_style(text):
        return True

    t = normalize_text(text)
    if not t:
        return False
    tokens = _tokenize_for_fuzzy(normalized=t)
    if DRIVER_OLAMIZ_YURAMIZ_RE.search(t):
        return True
    if has_driver_force_token(t, tokens=tokens):
        return True
    if DRIVER_BERIN_CHIQIB_RE.search(t):
        return True
    if has_driver_seat_or_baggage_offer(t):
        return True
    if HARD_BLOCK_KAM_RE.search(t):
        return True
    if HARD_BLOCK_CARGO_RE.search(t) or DRIVER_CARGO_OFFER_RE.search(t):
        return True
    if EXPLICIT_DRIVER_IDENTITY_RE.search(t):
        return True
    if DRIVER_CAR_BOR_RE.search(t):
        return True
    if DRIVER_NEEDS_PASSENGER_RE.search(t):
        return True
    if SEAT_NEEDED_RE.search(t):
        return True
    if DRIVER_SELF_OFFER_RE.search(t) or DRIVER_QUESTION_OFFER_RE.search(t):
        return True

    passenger_taxi_hits = count_substring_hits(t, merged_markers(PASSENGER_TAXI_MARKERS, "PASSENGER_TAXI_MARKERS"), tokens=tokens)
    passenger_request_hits = count_substring_hits(t, merged_markers(PASSENGER_REQUEST_MARKERS, "PASSENGER_REQUEST_MARKERS"), tokens=tokens)
    people_hint = has_people_pattern(t)
    passenger_question_need = bool(re.search(r"\b(?:bor\s*mi|bormi)\b", t))
    passenger_need_context = bool(PAX_VEHICLE_NEED_RE.search(t)) or (
        passenger_taxi_hits >= 1 and (passenger_request_hits >= 1 or people_hint or passenger_question_need)
    )
    passenger_request = bool(
        passenger_request_hits
        or count_substring_hits(t, merged_markers(PASSENGER_DIRECT_PHRASES, "PASSENGER_DIRECT_PHRASES"), tokens=tokens)
    )
    if DRIVER_ORDER_AVAILABLE_RE.search(t) and not passenger_need_context:
        return True
    if DRIVER_ORDER_WORD_RE.search(t) and not passenger_need_context and not passenger_request:
        return True

    return False


def is_driver_message(text: str) -> bool:
    if has_emoji_by_ord(text):
        return True
    if is_driver_poster_style(text):
        return True

    t = normalize_text(text)
    if not t:
        return False
    tokens = _tokenize_for_fuzzy(normalized=t)
    if DRIVER_OLAMIZ_YURAMIZ_RE.search(t):
        return True
    if has_driver_force_token(t, tokens=tokens):
        return True
    if DRIVER_BERIN_CHIQIB_RE.search(t):
        return True
    if has_driver_seat_or_baggage_offer(t):
        return True

    strong_driver = count_word_hits(t, STRONG_DRIVER_MARKERS, tokens=tokens) >= 1
    clear_driver_intent = bool(DRIVER_CLEAR_INTENT_RE.search(t))
    service_ad = bool(DRIVER_SERVICE_AD_RE.search(t))
    price_offer = bool(DRIVER_PRICE_OFFER_RE.search(t))
    passenger_request = bool(
        count_substring_hits(t, merged_markers(PASSENGER_REQUEST_MARKERS, "PASSENGER_REQUEST_MARKERS"), tokens=tokens)
        or count_substring_hits(t, merged_markers(PASSENGER_DIRECT_PHRASES, "PASSENGER_DIRECT_PHRASES"), tokens=tokens)
    )
    passenger_taxi_hits = count_substring_hits(t, merged_markers(PASSENGER_TAXI_MARKERS, "PASSENGER_TAXI_MARKERS"), tokens=tokens)
    passenger_request_hits = count_substring_hits(t, merged_markers(PASSENGER_REQUEST_MARKERS, "PASSENGER_REQUEST_MARKERS"), tokens=tokens)
    people_hint = has_people_pattern(t)
    passenger_question_need = bool(re.search(r"\b(?:bor\s*mi|bormi)\b", t))
    passenger_need_context = bool(PAX_VEHICLE_NEED_RE.search(t)) or (
        passenger_taxi_hits >= 1 and (passenger_request_hits >= 1 or people_hint)
    )
    if passenger_taxi_hits >= 1 and passenger_question_need:
        passenger_need_context = True
    order_available = bool(DRIVER_ORDER_AVAILABLE_RE.search(t))
    order_word = bool(DRIVER_ORDER_WORD_RE.search(t))
    car_bor = bool(DRIVER_CAR_BOR_RE.search(t))
    car_word = bool(DRIVER_CAR_WORD_RE.search(t))
    seat_available = bool(DRIVER_SEAT_AVAILABLE_RE.search(t))
    seat_count = bool(DRIVER_SEAT_COUNT_RE.search(t))
    needs_passenger = bool(DRIVER_NEEDS_PASSENGER_RE.search(t))
    cargo_offer = bool(DRIVER_CARGO_OFFER_RE.search(t))
    group_trip = bool(GROUP_TRIP_RE.search(t))
    seat_needed = bool(SEAT_NEEDED_RE.search(t))
    explicit_driver_identity = bool(EXPLICIT_DRIVER_IDENTITY_RE.search(t))

    if explicit_driver_identity:
        return True
    if strong_driver:
        return True
    if seat_needed:
        return True
    if cargo_offer:
        return True
    if needs_passenger:
        return True
    if order_available and not passenger_need_context:
        return True
    if order_word and not passenger_need_context and not passenger_request:
        return True
    if car_bor:
        return True
    if group_trip and seat_available:
        return True
    if group_trip and car_word:
        return True
    if group_trip and seat_count and not passenger_request:
        return True
    if car_word and seat_count:
        return True
    if clear_driver_intent:
        return True
    if service_ad and not passenger_request:
        return True
    if price_offer and ("taxi" in t or "taksi" in t or "xizmat" in t) and not passenger_request:
        return True
    return False


def extract_phone(text: str) -> Optional[str]:
    raw_text = unicodedata.normalize("NFKC", text or "")
    raw_text = ZERO_WIDTH_RE.sub("", raw_text)
    for pattern in PHONE_PATTERNS:
        m = pattern.search(raw_text)
        if not m:
            continue
        raw = m.group(0).strip()
        if not raw:
            continue
        cleaned = re.sub(r"[^\d+]", "", raw)
        if cleaned.startswith("00"):
            cleaned = "+" + cleaned[2:]
        digits = re.sub(r"\D", "", cleaned)
        if len(digits) < 9 or len(digits) > 15:
            continue
        if cleaned.startswith("+"):
            return "+" + digits
        return digits
    return None


def has_phone_number(text: str) -> bool:
    return extract_phone(text or "") is not None


def has_phone_keyword_number(text: str) -> bool:
    raw_text = unicodedata.normalize("NFKC", text or "")
    raw_text = ZERO_WIDTH_RE.sub("", raw_text)
    normalized = normalize_text(raw_text)
    if not PHONE_KEYWORD_RE.search(normalized):
        return False
    if has_phone_number(raw_text):
        return True
    digit_count = len(re.findall(r"\d", raw_text))
    if digit_count >= 7:
        return True
    near_keyword_number = re.search(
        rf"{PHONE_KEYWORD_RE.pattern}[^\n]{{0,28}}\d(?:{PHONE_SEP_PATTERN}\d){{5,}}",
        normalized,
    )
    return bool(near_keyword_number)


def has_message_phone(message: Any, source_text: str) -> bool:
    if has_phone_keyword_number(source_text):
        return True
    if has_phone_number(source_text):
        return True
    media = getattr(message, "media", None)
    if media and getattr(media, "phone_number", None):
        media_phone = str(getattr(media, "phone_number", "")).strip()
        if has_phone_number(media_phone):
            return True
    return False


def _extract_block_refs_from_text(text: str) -> List[str]:
    raw_text = unicodedata.normalize("NFKC", text or "")
    raw_text = ZERO_WIDTH_RE.sub("", raw_text)
    found: List[str] = []

    def _add(ref: Optional[str]) -> None:
        if not ref or ref in found:
            return
        found.append(ref)

    for m in BLOCK_REF_MENTION_RE.finditer(raw_text):
        _add(_normalize_ad_block_ref(f"@{m.group(1)}"))
    for m in BLOCK_REF_TME_RE.finditer(raw_text):
        _add(_normalize_ad_block_ref(f"@{m.group(1)}"))
    return found


def is_ad_blocked_ref_message(message: Any, source_text: str) -> bool:
    refs = ad_block_refs()
    if not refs:
        return False
    blocked_set = set(refs)

    sender = getattr(message, "sender", None)
    sender_username = str(getattr(sender, "username", "") or "").strip()
    if sender_username:
        sender_ref = _normalize_ad_block_ref(f"@{sender_username}")
        if sender_ref and sender_ref in blocked_set:
            return True

    for ref in _extract_block_refs_from_text(source_text):
        if ref in blocked_set:
            return True
    return False


def extract_people_count(text: str) -> str:
    n = normalize_text(text)
    m = re.search(rf"\b(\d{{1,2}})\s*(?:ta)?\s*{PEOPLE_UNIT_PATTERN}\b", n)
    if m:
        return f"{m.group(1)} kishi"
    m2 = re.search(r"\b(\d{1,2})\s*ta\b", n)
    if m2:
        return f"{m2.group(1)} kishi"
    m3 = re.search(rf"\b(bitta|ikki|uch|{TORT_TOKEN}|besh|olti)\b", n)
    if m3:
        key = APOSTROPHE_STRIP_RE.sub("", m3.group(1))
        if key in BASIC_WORD_NUMBER_MAP:
            return f"{BASIC_WORD_NUMBER_MAP[key]} kishi"
    for w, value in WORD_NUMBERS.items():
        if w.endswith("ta") or w in {"bitta", "bittamiz", "onta", "o'nta"}:
            if re.search(rf"\b{re.escape(w)}\b", n):
                return f"{value} kishi"
        else:
            if re.search(rf"\b{re.escape(w)}\s*{PEOPLE_UNIT_PATTERN}\b", n):
                return f"{value} kishi"
    return "Aniqlanmadi"


def clean_route_part(part: str) -> str:
    out = part.strip(" -/>.,;:")
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"(dan|ga)$", "", out).strip()
    out = re.sub(
        rf"\b(taksi|taxi|kerak|bormi|iltimos|{YOLOVCHI_TOKEN}|passajir|pasajir|{RU_PASSAZHIR}|mijoz|kishi|odam|nafar|soat|bugun|ertaga)\b",
        "",
        out,
    )
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out


def extract_route(text: str) -> str:
    n = normalize_text(text)
    m0 = re.search(r"\b([\w'`-]{2,45})dan\s+([\w'`-]{2,45})ga\b", n)
    if m0:
        src = clean_route_part(m0.group(1))
        dst = clean_route_part(m0.group(2))
        if src and dst:
            return f"{src} -> {dst}"

    m = re.search(r"\b([\w'` -]{2,45})\s+dan\s+([\w'` -]{2,45})\s+ga\b", n)
    if m:
        src = clean_route_part(m.group(1))
        dst = clean_route_part(m.group(2))
        if src and dst:
            return f"{src} -> {dst}"

    m1 = re.search(r"\b([\w'` -]{2,45})\s+dan\s+([\w'` -]{2,45})ga\b", n)
    if m1:
        src = clean_route_part(m1.group(1))
        dst = clean_route_part(m1.group(2))
        if src and dst:
            return f"{src} -> {dst}"

    m2 = re.search(r"\b([\w'` -]{2,45})\s*[-/>]+\s*([\w'` -]{2,45})\b", n)
    if m2:
        src = clean_route_part(m2.group(1))
        dst = clean_route_part(m2.group(2))
        if src and dst:
            return f"{src} -> {dst}"

    m3 = re.search(r"\b([\w'` -]{2,45})\s+ga\b", n)
    if m3:
        dst = clean_route_part(m3.group(1))
        if dst:
            return dst
    m4 = re.search(r"\b([\w'` -]{2,45})ga\b", n)
    if m4:
        dst = clean_route_part(m4.group(1))
        if dst:
            return dst
    return "Aniqlanmadi"


def _entity_username(entity: Any) -> Optional[str]:
    username = getattr(entity, "username", None)
    if username:
        return f"@{username}"
    return None


async def get_username(message, fast_mode: bool = False, client: Any = None) -> str:
    cached = _entity_username(getattr(message, "sender", None))
    if cached:
        return cached
    sender_id = int(getattr(message, "sender_id", 0) or 0)
    if fast_mode:
        sender = None
        try:
            sender = await asyncio.wait_for(message.get_sender(), timeout=0.35)
        except Exception:
            sender = None
        resolved_fast = _entity_username(sender)
        if resolved_fast:
            return resolved_fast
        sender_entity_id = int(getattr(sender, "id", 0) or 0) if sender is not None else 0
        lookup_id = sender_entity_id or sender_id
        if client is not None and lookup_id > 0:
            try:
                entity = await asyncio.wait_for(client.get_entity(lookup_id), timeout=0.8)
            except Exception:
                entity = None
            resolved_entity = _entity_username(entity)
            if resolved_entity:
                return resolved_entity
        if lookup_id > 0:
            return f"id:{lookup_id}"
        return "yo'q"
    try:
        sender = await message.get_sender()
    except Exception:
        sender = None
    resolved = _entity_username(sender)
    if resolved:
        return resolved
    sender_entity_id = int(getattr(sender, "id", 0) or 0) if sender is not None else 0
    lookup_id = sender_entity_id or sender_id
    if client is not None and lookup_id > 0:
        try:
            entity = await client.get_entity(lookup_id)
        except Exception:
            entity = None
        resolved_entity = _entity_username(entity)
        if resolved_entity:
            return resolved_entity
    if lookup_id > 0:
        return f"id:{lookup_id}"
    return "yo'q"


async def get_chat_cached(message, fast_mode: bool = False) -> Any:
    chat = getattr(message, "chat", None)
    if chat is not None:
        return chat
    if fast_mode:
        return None
    try:
        return await message.get_chat()
    except Exception:
        return None


def get_source_title(chat: Any, chat_id: int) -> str:
    title = getattr(chat, "title", None)
    if title:
        return str(title)
    username = getattr(chat, "username", None)
    if username:
        return f"@{username}"
    return str(chat_id)


def get_message_link(chat_id: int, chat_username: Optional[str], message_id: int) -> str:
    if chat_username:
        return f"https://t.me/{chat_username}/{message_id}"
    chat_id_text = str(chat_id)
    if chat_id_text.startswith("-100"):
        return f"https://t.me/c/{chat_id_text[4:]}/{message_id}"
    return f"id:{chat_id}:{message_id}"


def _trim_to_telegram_limit(prefix: str, body: str) -> str:
    out = prefix + body
    if len(out) <= MAX_TELEGRAM_TEXT:
        return out

    raw = body
    while raw and len(prefix + raw) > MAX_TELEGRAM_TEXT - 3:
        raw = raw[:-64]
    if raw:
        return prefix + raw + "..."
    return (prefix[: MAX_TELEGRAM_TEXT - 3] + "...") if len(prefix) > MAX_TELEGRAM_TEXT else prefix


async def format_driver_post(message, source_text: str, fast_mode: bool = False, client: Any = None) -> str:
    username = await get_username(message, fast_mode=fast_mode, client=client)
    phone = extract_phone(source_text)

    media = getattr(message, "media", None)
    if not phone and media and getattr(media, "phone_number", None):
        phone = str(media.phone_number).strip()
    phone_text = phone or "Mavjud emas!"

    chat_id = int(getattr(message, "chat_id", 0) or 0)
    chat = await get_chat_cached(message, fast_mode=fast_mode)
    chat_username = getattr(chat, "username", None)
    link = get_message_link(chat_id, chat_username, int(getattr(message, "id", 0) or 0))

    if username.startswith("@"):
        username_html = f"<a href=\"https://t.me/{escape(username[1:])}\">{escape(username)}</a>"
    elif username.startswith("id:") and re.fullmatch(r"\d+", username[3:]):
        uid = username[3:]
        username_html = (
            f"<a href=\"tg://user?id={uid}\">id:{escape(uid)}</a>"
            f" (<a href=\"tg://openmessage?user_id={uid}\">profil</a>)"
        )
    else:
        username_html = escape(username)

    message_html = escape((source_text or "").strip() or "-")
    header = "<b>🔔 Signal</b>\n"
    info_line = f"👤☎️ {username_html} - {escape(phone_text)}\n"
    divider = "──────────\n"
    if link.startswith("https://"):
        link_line = f"➡️ <a href=\"{escape(link)}\">Xabarni ko'rish</a>"
    else:
        link_line = f"➡️ Xabar linki mavjud emas ({escape(link)})"

    tail = f"\n{info_line}{divider}{link_line}"
    max_message = MAX_TELEGRAM_TEXT - len(header) - len(tail)
    if max_message < 1:
        return _trim_to_telegram_limit(header, message_html + tail)
    if len(message_html) > max_message:
        if max_message > 3:
            message_html = message_html[: max_message - 3] + "..."
        else:
            message_html = message_html[:max_message]
    return header + message_html + tail


async def send_to_driver_group(client, text: str) -> bool:
    dg = driver_group()
    if dg == 0:
        logging.warning("DRIVER_GROUP o'rnatilmagan")
        return False

    for cid in chat_id_candidates(dg):
        try:
            await client.send_message(cid, text, parse_mode="html", link_preview=False)
            if cid != dg:
                CONFIG["DRIVER_GROUP"] = cid
                save_runtime()
            return True
        except RPCError:
            logging.exception("driver send telegram error chat_id=%s", cid)
        except Exception:
            logging.exception("driver send unexpected error chat_id=%s", cid)
    return False


def register_handlers(client) -> None:
    pending_tasks: set[asyncio.Task[Any]] = set()

    def _spawn_background(coro: Any) -> None:
        task: asyncio.Task[Any] = asyncio.create_task(coro)
        pending_tasks.add(task)

        def _cleanup(done: asyncio.Task[Any]) -> None:
            pending_tasks.discard(done)
            with suppress(Exception):
                done.result()

        task.add_done_callback(_cleanup)

    async def _process_captured_message(message: Any, source_text: str, chat_id: int) -> None:
        try:
            source_text_token_count = estimate_text_token_count(source_text)
            _stat_inc("tokens_total", source_text_token_count)

            def _mark_filtered() -> None:
                _stat_inc("filtered")
                _stat_inc("tokens_filtered", source_text_token_count)

            normalized_source = normalize_text(source_text)
            source_tokens = _tokenize_for_fuzzy(normalized=normalized_source)
            sender_user_id = extract_sender_user_id(message)

            if is_duplicate_message(chat_id, sender_user_id, normalized_source):
                logging.info(
                    "filtered duplicate chat_id=%s sender_id=%s text=%s",
                    chat_id,
                    sender_user_id,
                    normalized_source[:180],
                )
                _mark_filtered()
                return

            if await is_bot_message(message):
                logging.info(
                    "filtered bot-message chat_id=%s sender_id=%s text=%s",
                    chat_id,
                    sender_user_id,
                    normalized_source[:180],
                )
                _mark_filtered()
                return

            if sender_user_id > 0 and is_ad_blocked_user(sender_user_id):
                logging.info(
                    "filtered ad-blocked-user sender_id=%s text=%s",
                    sender_user_id,
                    normalized_source[:180],
                )
                _mark_filtered()
                return
            if is_ad_blocked_ref_message(message, source_text):
                blocked_now = add_ad_block_user(sender_user_id) if sender_user_id > 0 else False
                logging.info(
                    "filtered ad-blocked-ref sender_id=%s blocked_now=%s text=%s",
                    sender_user_id,
                    blocked_now,
                    normalized_source[:180],
                )
                _mark_filtered()
                return

            stage_result = classify_passenger_pipeline(
                source_text,
                normalized=normalized_source,
                tokens=source_tokens,
            )
            stage_decision = str(stage_result.get("decision", "BLOCK")).upper()
            stage_reason = str(stage_result.get("reason", "unknown"))

            if stage_decision == "BLOCK":
                ad_reasons = {"commercial-ad", "contains-link", "spam-marker"}
                blocked_now = add_ad_block_user(sender_user_id) if sender_user_id > 0 and stage_reason in ad_reasons else False
                logging.info(
                    "filtered stage-block reason=%s sender_id=%s blocked_now=%s text=%s",
                    stage_reason,
                    sender_user_id,
                    blocked_now,
                    normalized_source[:180],
                )
                _mark_filtered()
                return

            out = await format_driver_post(message, source_text, fast_mode=True, client=client)
            if stage_decision == "REVIEW":
                review_out = out.replace("<b>🔔 Signal</b>", "<b>🟡 REVIEW</b>", 1)
                logging.info(
                    "reviewed stage reason=%s text=%s",
                    stage_reason,
                    normalized_source[:180],
                )
                sent = await send_to_driver_group(client, review_out)
                if sent:
                    _stat_inc("reviewed")
                    _stat_inc("tokens_reviewed", source_text_token_count)
                else:
                    _stat_inc("errors")
                return

            logging.info("forwarded passenger reason=%s text=%s", stage_reason, normalized_source[:180])
            sent = await send_to_driver_group(client, out)
            if sent:
                _stat_inc("forwarded")
                _stat_inc("tokens_forwarded", source_text_token_count)
            else:
                _stat_inc("errors")
        except Exception:
            _stat_inc("errors")
            logging.exception("incoming handler failed")

    @client.on(events.NewMessage(incoming=True))
    async def incoming(event: Any) -> None:
        try:
            def _mark_filtered_event(raw_text: str = "") -> None:
                _stat_inc("filtered")
                text = (raw_text or "").strip()
                if not text:
                    return
                token_count = estimate_text_token_count(text)
                _stat_inc("tokens_total", token_count)
                _stat_inc("tokens_filtered", token_count)

            _stat_inc("received")
            incoming_text = ((event.raw_text or getattr(event.message, "message", "") or "")).strip()
            if not relay_enabled():
                _mark_filtered_event(incoming_text)
                return

            chat_id = resolve_event_chat_id(event)
            if chat_id == 0:
                logging.info("filtered no-chat-id")
                _mark_filtered_event(incoming_text)
                return
            if not is_source_chat(chat_id):
                return
            if is_blocked_chat(chat_id):
                logging.info("filtered blocked-chat chat_id=%s", chat_id)
                _mark_filtered_event(incoming_text)
                return

            source_text = incoming_text
            if not source_text:
                _mark_filtered_event(source_text)
                return

            # Snapshot first, then run filtering/forwarding in the background.
            _spawn_background(_process_captured_message(event.message, source_text, chat_id))
        except Exception:
            _stat_inc("errors")
            logging.exception("incoming handler failed")


async def _run() -> None:
    if TELETHON_IMPORT_ERROR is not None:
        raise RuntimeError("telethon o'rnatilmagan. O'rnatish: pip install telethon")

    load_runtime()
    if api_id() <= 0 or not api_hash():
        raise RuntimeError("config.json ichida API_ID va API_HASH ni to'ldiring")

    user_client = TelegramClient(session_name(), api_id(), api_hash())
    register_handlers(user_client)

    admin_client = None
    admin_task = None
    token = bot_token()
    if token:
        if register_admin_panel_handlers is None:
            logging.warning("Admin panel import bo'lmadi, BOT_TOKEN e'tiborsiz qoldirildi.")
        else:
            admin_client = TelegramClient(admin_bot_session_name(), api_id(), api_hash())
            register_admin_panel_handlers(admin_client, admin_panel_hooks())

    await user_client.start()
    logging.info("Passenger relay userbot started | filter=%s", FILTER_RULESET_VERSION)

    if admin_client is not None:
        await admin_client.start(bot_token=token)
        if not admin_ids():
            logging.warning("ADMIN_IDS bo'sh. Admin botga ruxsat berilmaydi.")
        logging.info("Admin panel bot started | admins=%s", admin_ids())
        admin_task = asyncio.create_task(admin_client.run_until_disconnected())

    try:
        await user_client.run_until_disconnected()
    finally:
        if admin_task is not None:
            admin_task.cancel()
            with suppress(asyncio.CancelledError):
                await admin_task
        if admin_client is not None:
            with suppress(Exception):
                await admin_client.disconnect()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s - %(message)s")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
