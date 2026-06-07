import os
import requests
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

user_states = {}


def send_message(chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    if reply_markup:
        payload["reply_markup"] = reply_markup

    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)


def send_main_menu(chat_id):
    keyboard = {
        "inline_keyboard": [
            [
                {
                    "text": "📄 Доверенность Москва Карго",
                    "callback_data": "power_moscow_cargo"
                }
            ]
        ]
    }

    send_message(chat_id, "Выберите действие:", keyboard)


@app.route("/", methods=["GET"])
def index():
    return "Bot is running"


@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json()

    if "message" in update:
        handle_message(update["message"])

    if "callback_query" in update:
        handle_callback(update["callback_query"])

    return "ok"


def handle_message(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    if text == "/start":
        send_main_menu(chat_id)
        return

    process_power_input(chat_id, text)


def handle_callback(callback):
    chat_id = callback["message"]["chat"]["id"]
    data = callback["data"]

    if data == "power_moscow_cargo":
        user_states[chat_id] = {
            "step": "company",
            "data": {}
        }
        send_message(chat_id, "Введите название компании-доверителя:")


def process_power_input(chat_id, text):
    state = user_states.get(chat_id)

    if not state:
        send_message(chat_id, "Напишите /start, чтобы открыть меню.")
        return

    step = state["step"]
    data = state["data"]

    if step == "company":
        data["company"] = text
        state["step"] = "address"
        send_message(chat_id, "Введите адрес компании:")
        return

    if step == "address":
        data["address"] = text
        state["step"] = "inn"
        send_message(chat_id, "Введите ИНН:")
        return

    if step == "inn":
        data["inn"] = text
        state["step"] = "ogrn"
        send_message(chat_id, "Введите ОГРН:")
        return

    if step == "ogrn":
        data["ogrn"] = text
        state["step"] = "director_position"
        send_message(chat_id, "Введите должность руководителя. Например: генерального директора")
        return

    if step == "director_position":
        data["director_position"] = text
        state["step"] = "director_name"
        send_message(chat_id, "Введите ФИО руководителя в родительном падеже. Например: Иванова Ивана Ивановича")
        return

    if step == "director_name":
        data["director_name"] = text
        state["step"] = "term"
        send_message(chat_id, "Введите срок действия доверенности. Например: 3 года")
        return

    if step == "term":
        data["term"] = text
        send_message(chat_id, "✅ Данные собраны. Следующим шагом добавим формирование Word-файла.")
        user_states.pop(chat_id, None)
        return


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
