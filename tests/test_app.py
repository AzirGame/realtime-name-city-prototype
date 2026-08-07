import pytest

from app import app, is_valid_word, rematch_requests, rooms, socketio, waiting_players


@pytest.fixture(autouse=True)
def clear_server_state():
    rooms.clear()
    waiting_players.clear()
    rematch_requests.clear()
    yield
    rooms.clear()
    waiting_players.clear()
    rematch_requests.clear()


def test_health_endpoint():
    response = app.test_client().get("/health")

    assert response.status_code == 200
    assert response.get_json() == {"status": "healthy"}


def test_word_validation_checks_letter_and_dataset():
    assert is_valid_word("Şehir", "İzmir", "İ")
    assert not is_valid_word("Şehir", "Ankara", "İ")
    assert not is_valid_word("Şehir", "İzmir", "A")


def test_room_creation_returns_join_event():
    client = socketio.test_client(app)

    client.emit(
        "oda_olustur",
        {"oda": "test-room", "sifre": "1234", "nickname": "Deniz"},
    )
    received_events = client.get_received()
    event_names = {event["name"] for event in received_events}

    assert "oda_katildi" in event_names
    assert "oyuncular_guncellendi" in event_names
    assert rooms["test-room"]["players"]

    client.disconnect()


def test_duplicate_room_name_returns_error():
    first_client = socketio.test_client(app)
    second_client = socketio.test_client(app)
    payload = {"oda": "shared-room", "nickname": "Player"}

    first_client.emit("oda_olustur", payload)
    first_client.get_received()
    second_client.emit("oda_olustur", payload)
    received_events = second_client.get_received()

    assert any(event["name"] == "hata" for event in received_events)

    first_client.disconnect()
    second_client.disconnect()
