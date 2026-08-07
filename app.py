from __future__ import annotations

import logging
import os
import random
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_socketio import SocketIO, emit, join_room


load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
LETTERS = "ABCÇDEFGHİIJKLMNOÖPRSŞTUÜVYZ"
WORD_FILES = {
    "İsim": "isim.txt",
    "Şehir": "sehir.txt",
    "Hayvan": "hayvan.txt",
    "Bitki": "bitki.txt",
    "Ülke": "ulke.txt",
    "Ünlü": "unlu.txt",
    "Eşya": "esya.txt",
    "Yemek": "yemek.txt",
}
DEFAULT_CATEGORIES = ["İsim", "Şehir", "Hayvan", "Bitki", "Eşya"]

rooms: dict[str, dict[str, Any]] = {}
waiting_players: list[dict[str, str]] = []
rematch_requests: dict[str, list[dict[str, str]]] = {}
word_pools: dict[str, set[str]] = {category: set() for category in WORD_FILES}

socketio = SocketIO(async_mode="threading")


def _allowed_origins() -> list[str] | str | None:
    raw_origins = os.getenv("ALLOWED_ORIGINS", "").strip()
    if not raw_origins:
        return None
    if raw_origins == "*":
        logger.warning("ALLOWED_ORIGINS is set to '*'. Use explicit origins in production.")
        return "*"
    return [origin.strip() for origin in raw_origins.split(",") if origin.strip()]


def _secret_key() -> str:
    configured_key = os.getenv("SECRET_KEY")
    if configured_key:
        return configured_key

    logger.warning(
        "SECRET_KEY is not configured. Using an ephemeral development key; "
        "sessions will reset when the process restarts."
    )
    return secrets.token_urlsafe(32)


def create_app() -> Flask:
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = _secret_key()
    socketio.init_app(flask_app, cors_allowed_origins=_allowed_origins())

    @flask_app.get("/")
    def index():
        return jsonify(
            {
                "name": "Realtime Name-City Prototype",
                "status": "ok",
                "transport": "Socket.IO",
            }
        )

    @flask_app.get("/health")
    def health():
        return jsonify({"status": "healthy"})

    return flask_app


def load_word_pools() -> None:
    data_directory = BASE_DIR / "data"
    if not data_directory.is_dir():
        raise RuntimeError(f"Word data directory is missing: {data_directory}")

    for category, filename in WORD_FILES.items():
        path = data_directory / filename
        if not path.is_file():
            logger.warning("Word list is missing for %s: %s", category, path)
            continue

        with path.open(encoding="utf-8") as word_file:
            word_pools[category] = {
                line.strip().upper() for line in word_file if line.strip()
            }
        logger.info("Loaded %s words for %s", len(word_pools[category]), category)


def is_valid_word(category: str, word: str, letter: str) -> bool:
    normalized_word = word.strip().upper()
    if not normalized_word or not normalized_word.startswith(letter):
        return False

    category_pool = word_pools.get(category, set())
    return not category_pool or normalized_word in category_pool


def _payload(data: Any) -> dict[str, Any]:
    return data if isinstance(data, dict) else {}


def _clean_text(value: Any, *, maximum: int, default: str = "") -> str:
    if not isinstance(value, str):
        return default
    cleaned = value.strip()
    return cleaned[:maximum] if cleaned else default


def _room_name(data: dict[str, Any]) -> str:
    return _clean_text(data.get("oda") or data.get("roomName"), maximum=64)


def _nickname(data: dict[str, Any], default: str = "Oyuncu") -> str:
    return _clean_text(data.get("nickname"), maximum=32, default=default)


def _emit_error(message: str) -> None:
    emit("hata", {"mesaj": message})


load_word_pools()
app = create_app()


@socketio.on("oda_olustur")
def handle_create(data: Any) -> None:
    payload = _payload(data)
    room_name = _room_name(payload)
    if not room_name:
        _emit_error("Oda adı zorunludur.")
        return
    if room_name in rooms:
        _emit_error("Bu isimde bir oda zaten var.")
        return

    password = _clean_text(
        payload.get("sifre") or payload.get("password"), maximum=128
    )
    nickname = _nickname(payload, "Anonim")

    rooms[room_name] = {
        "password": password,
        "host": request.sid,
        "letter": "",
        "categories": DEFAULT_CATEGORIES.copy(),
        "players": {request.sid: nickname},
        "answers": {},
        "scored": False,
    }
    join_room(room_name)

    emit(
        "oda_katildi",
        {
            "oda": room_name,
            "is_host": True,
            "kategoriler": rooms[room_name]["categories"],
        },
    )
    emit("oyuncular_guncellendi", {"oyuncular": [nickname]})


@socketio.on("oda_katil")
def handle_join(data: Any) -> None:
    payload = _payload(data)
    room_name = _room_name(payload)
    password = _clean_text(
        payload.get("sifre") or payload.get("password"), maximum=128
    )
    nickname = _nickname(payload, "Misafir")

    room = rooms.get(room_name)
    if room is None or room["password"] != password:
        _emit_error("Hatalı giriş veya oda bulunamadı.")
        return

    join_room(room_name)
    room["players"][request.sid] = nickname
    player_list = list(room["players"].values())

    emit(
        "oda_katildi",
        {
            "oda": room_name,
            "is_host": False,
            "kategoriler": room["categories"],
        },
    )
    emit(
        "kategorileri_guncelle",
        {"kategoriler": room["categories"]},
        room=room_name,
    )
    emit("oyuncular_guncellendi", {"oyuncular": player_list}, room=room_name)


@socketio.on("hemen_oyna")
def handle_matchmaking(data: Any) -> None:
    payload = _payload(data)
    nickname = _nickname(payload)

    if not any(player["sid"] == request.sid for player in waiting_players):
        waiting_players.append({"sid": request.sid, "nick": nickname})
    emit("eslesme_bekleniyor", {"mesaj": "Rakip aranıyor..."})

    if len(waiting_players) < 2:
        return

    first_player = waiting_players.pop(0)
    second_player = waiting_players.pop(0)
    room_name = f"match_{uuid.uuid4().hex[:8]}"
    selected_categories = random.sample(list(WORD_FILES), 5)

    rooms[room_name] = {
        "password": "",
        "host": first_player["sid"],
        "letter": random.choice(LETTERS),
        "categories": selected_categories,
        "players": {
            first_player["sid"]: first_player["nick"],
            second_player["sid"]: second_player["nick"],
        },
        "answers": {},
        "scored": False,
    }

    for player in (first_player, second_player):
        join_room(room_name, sid=player["sid"])

    emit(
        "eslesme_tamam",
        {
            "oda": room_name,
            "harf": rooms[room_name]["letter"],
            "kategoriler": selected_categories,
            "rakipler": rooms[room_name]["players"],
        },
        room=room_name,
    )


@socketio.on("iptal_et")
def handle_cancel() -> None:
    waiting_players[:] = [
        player for player in waiting_players if player["sid"] != request.sid
    ]


@socketio.on("oyunu_baslat")
def handle_start(data: Any) -> None:
    room_name = _room_name(_payload(data))
    room = rooms.get(room_name)
    if room is None or room["host"] != request.sid:
        _emit_error("Oyunu yalnızca oda sahibi başlatabilir.")
        return

    room.update(
        {
            "letter": random.choice(LETTERS),
            "answers": {},
            "scored": False,
        }
    )
    emit(
        "yeni_oyun_basladi",
        {"harf": room["letter"], "kategoriler": room["categories"]},
        room=room_name,
    )


@socketio.on("cevaplari_gonder")
def handle_answers(data: Any) -> None:
    payload = _payload(data)
    room_name = _room_name(payload)
    room = rooms.get(room_name)
    answers = payload.get("cevaplar")
    if room is None or request.sid not in room["players"]:
        _emit_error("Oda bulunamadı.")
        return
    if not isinstance(answers, dict):
        _emit_error("Cevap biçimi geçersiz.")
        return

    room["answers"][request.sid] = {
        category: _clean_text(answers.get(category), maximum=64)
        for category in room["categories"]
    }


@socketio.on("oyunu_bitir")
def handle_finish(data: Any) -> None:
    room_name = _room_name(_payload(data))
    room = rooms.get(room_name)
    if room is None or request.sid not in room["players"]:
        _emit_error("Oda bulunamadı.")
        return

    emit("geri_sayim_baslat", {"sure": 10}, room=room_name)
    socketio.sleep(12)
    if not room["scored"]:
        score_room(room_name)
        room["scored"] = True


@socketio.on("kategori_degistir")
def handle_category_change(data: Any) -> None:
    payload = _payload(data)
    room_name = _room_name(payload)
    room = rooms.get(room_name)
    categories = payload.get("kategoriler")

    if room is None or room["host"] != request.sid:
        _emit_error("Kategorileri yalnızca oda sahibi değiştirebilir.")
        return
    if not isinstance(categories, list):
        _emit_error("Kategori biçimi geçersiz.")
        return

    selected_categories = [
        category
        for category in categories
        if category in WORD_FILES
    ]
    selected_categories = list(dict.fromkeys(selected_categories))
    if not selected_categories:
        _emit_error("En az bir geçerli kategori seçilmelidir.")
        return

    room["categories"] = selected_categories
    emit(
        "kategorileri_guncelle",
        {"kategoriler": selected_categories},
        room=room_name,
    )


@socketio.on("disconnect")
def handle_disconnect() -> None:
    waiting_players[:] = [
        player for player in waiting_players if player["sid"] != request.sid
    ]

    for room_name, room in list(rooms.items()):
        if request.sid not in room["players"]:
            continue

        room["players"].pop(request.sid, None)
        room["answers"].pop(request.sid, None)
        if not room["players"]:
            rooms.pop(room_name, None)
            rematch_requests.pop(room_name, None)
            continue

        if room["host"] == request.sid:
            room["host"] = next(iter(room["players"]))
        socketio.emit(
            "oyuncular_guncellendi",
            {"oyuncular": list(room["players"].values())},
            to=room_name,
        )


def score_room(room_name: str) -> None:
    room = rooms.get(room_name)
    if room is None:
        return

    results: dict[str, dict[str, Any]] = {}
    categories = room["categories"]
    letter = room["letter"]
    answer_pool = room["answers"]

    for sid, player_answers in answer_pool.items():
        total_score = 0
        details: dict[str, dict[str, Any]] = {}

        for category in categories:
            word = player_answers.get(category, "").strip().upper()
            if is_valid_word(category, word, letter):
                other_words = [
                    answers.get(category, "").strip().upper()
                    for other_sid, answers in answer_pool.items()
                    if other_sid != sid
                ]
                score = 5 if word in other_words else 10
            else:
                score = 0

            total_score += score
            details[category] = {"kelime": word, "puan": score}

        results[sid] = {
            "toplam": total_score,
            "detay": details,
            "nickname": room["players"].get(sid, "Bilinmiyor"),
        }

    socketio.emit("puan_durumu", results, to=room_name)


@socketio.on("sohbet_gonder")
def handle_chat(data: Any) -> None:
    payload = _payload(data)
    room_name = _room_name(payload)
    room = rooms.get(room_name)
    if room is None or request.sid not in room["players"]:
        _emit_error("Mesaj gönderilemedi: oda bulunamadı.")
        return

    message = _clean_text(payload.get("message"), maximum=500)
    if not message:
        return

    sender = room["players"].get(request.sid, _nickname(payload))
    emit(
        "sohbet_al",
        {
            "sender": sender,
            "message": message,
            "time": datetime.now().strftime("%H:%M"),
        },
        room=room_name,
    )


@socketio.on("tekrar_oyna_istegi")
def handle_rematch_request(data: Any) -> None:
    payload = _payload(data)
    old_room_name = _clean_text(payload.get("old_room"), maximum=64)
    old_room = rooms.get(old_room_name)
    if old_room is None or request.sid not in old_room["players"]:
        _emit_error("Tekrar oynama isteği için oda bulunamadı.")
        return

    nickname = old_room["players"][request.sid]
    requests_for_room = rematch_requests.setdefault(old_room_name, [])
    if not any(player["sid"] == request.sid for player in requests_for_room):
        requests_for_room.append({"sid": request.sid, "nick": nickname})

    if len(requests_for_room) != len(old_room["players"]):
        return

    room_name = f"match_{uuid.uuid4().hex[:8]}"
    selected_categories = random.sample(list(WORD_FILES), 5)
    players = {player["sid"]: player["nick"] for player in requests_for_room}

    for player in requests_for_room:
        join_room(room_name, sid=player["sid"])

    rooms[room_name] = {
        "password": "",
        "host": requests_for_room[0]["sid"],
        "letter": random.choice(LETTERS),
        "categories": selected_categories,
        "players": players,
        "answers": {},
        "scored": False,
    }
    rematch_requests.pop(old_room_name, None)

    emit(
        "eslesme_tamam",
        {
            "oda": room_name,
            "harf": rooms[room_name]["letter"],
            "kategoriler": selected_categories,
            "rakipler": players,
        },
        room=room_name,
    )


if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )
