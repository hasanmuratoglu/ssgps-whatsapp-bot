import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- Ayarlar (Meta hesabı hazır olunca doldurulacak) ---
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "ssgps_verify_123")
ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
GRAPH_API_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"

# --- Basit in-memory conversation state ---
# Gerçek kullanımda Redis/DB olacak, şimdilik test için yeterli
user_states = {}

# --- Arıza checklist tanımları ---
# Her adım: soru metni + cevaba göre bir sonraki adım / çözüm / eskalasyon
ARIZA_FLOWS = {
    "gps_tasma": {
        0: {
            "question": "Cihaz açılıyor mu? (Evet/Hayır yazabilirsiniz)",
            "yes_next": 1,
            "no_solution": "Lütfen şarj kablosunu kontrol edip cihazı en az 15 dakika şarjda bekletin. Sonra tekrar deneyip 'evet' veya 'hayır' yazın.",
        },
        1: {
            "question": "Cihaz GPS sinyali/konum alıyor mu? (açık alanda birkaç dakika bekleyip kontrol edin)",
            "yes_solution": "Sorun büyük ihtimalle çözülmüş görünüyor. Başka bir konuda yardımcı olabilir miyim?",
            "yes_next": "done",
            "no_next": "escalate",
        },
    },
}


# ---------------------------------------------------------------------
# WhatsApp API'ye mesaj gönderme yardımcıları
# ---------------------------------------------------------------------

def send_text(to, body):
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body},
    }
    requests.post(GRAPH_API_URL, headers=headers, json=payload)


def send_menu(to):
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": "Merhaba! Size nasıl yardımcı olabilirim?"},
            "action": {
                "button": "Seçim Yap",
                "sections": [{
                    "title": "SS GPS Destek",
                    "rows": [
                        {"id": "teknik_destek", "title": "Teknik Destek"},
                        {"id": "urun_bilgisi", "title": "Ürün Bilgisi"},
                        {"id": "satis", "title": "Satış / Sipariş"},
                        {"id": "ariza", "title": "Arıza Bildirimi"},
                    ]
                }]
            }
        }
    }
    requests.post(GRAPH_API_URL, headers=headers, json=payload)


# ---------------------------------------------------------------------
# Webhook endpoint'leri
# ---------------------------------------------------------------------

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive_message():
    data = request.get_json()

    try:
        entry = data["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return jsonify(status="ok"), 200

        message = entry["messages"][0]
        from_number = message["from"]

        # Buton/menü seçimi mi, serbest metin mi?
        if message["type"] == "interactive":
            selection = message["interactive"]["list_reply"]["id"]
            handle_menu_selection(from_number, selection)
        elif message["type"] == "text":
            handle_text_message(from_number, message["text"]["body"])

    except (KeyError, IndexError):
        pass

    return jsonify(status="ok"), 200


# ---------------------------------------------------------------------
# Akış yönlendirme mantığı
# ---------------------------------------------------------------------

def handle_text_message(from_number, text):
    state = user_states.get(from_number, {}).get("flow")
    if state is None:
        # Yeni konuşma -> menü göster
        send_menu(from_number)
    else:
        # Akış içindeyse, o akışın handler'ına yönlendir
        route_flow_input(from_number, text)


def handle_menu_selection(from_number, selection):
    user_states[from_number] = {"flow": selection, "step": 0}

    if selection == "ariza":
        start_ariza_checklist(from_number)
    elif selection == "teknik_destek":
        send_text(from_number, "Hangi ürün hakkında teknik desteğe ihtiyacınız var?")
    elif selection == "urun_bilgisi":
        send_text(from_number, "Hangi ürünümüz hakkında bilgi almak istersiniz?")
    elif selection == "satis":
        send_text(from_number, "Hangi ürünü sipariş etmek istersiniz?")


def start_ariza_checklist(from_number):
    send_text(
        from_number,
        "Arıza bildirimi başlatıldı. Hangi ürünle ilgili sorun yaşıyorsunuz?\n"
        "1) GPS Tasması (eSEEK/Dogtrace)\n"
        "2) Rifle Kamera (HIKMICRO)\n"
        "Lütfen numarayı yazın."
    )
    user_states[from_number] = {"flow": "ariza_urun_secimi", "step": 0}


def route_flow_input(from_number, text):
    state = user_states.get(from_number, {})
    flow = state.get("flow")

    if flow == "ariza_urun_secimi":
        if text.strip() == "1":
            user_states[from_number] = {"flow": "gps_tasma", "step": 0}
            ask_checklist_question(from_number, "gps_tasma", 0)
        elif text.strip() == "2":
            send_text(from_number, "Rifle kamera checklist'i yakında eklenecek. Şimdilik teknik ekibe yönlendiriyorum.")
            escalate_to_technical(from_number, "Rifle kamera arızası")
        else:
            send_text(from_number, "Lütfen 1 veya 2 yazın.")
        return

    if flow in ARIZA_FLOWS:
        handle_checklist_answer(from_number, flow, state.get("step", 0), text)


def ask_checklist_question(from_number, flow, step):
    step_data = ARIZA_FLOWS[flow][step]
    send_text(from_number, step_data["question"])


def handle_checklist_answer(from_number, flow, step, text):
    step_data = ARIZA_FLOWS[flow][step]
    answer = text.strip().lower()

    if answer in ("evet", "e", "yes"):
        next_step = step_data.get("yes_next")
        if next_step == "done":
            send_text(from_number, step_data.get("yes_solution", "Sorun çözülmüş görünüyor, teşekkürler."))
            user_states.pop(from_number, None)
        elif next_step == "escalate":
            escalate_to_technical(from_number, f"{flow} - adım {step}'de çözülemedi")
        elif next_step is not None:
            user_states[from_number]["step"] = next_step
            ask_checklist_question(from_number, flow, next_step)
    elif answer in ("hayır", "hayir", "h", "no"):
        if "no_solution" in step_data:
            send_text(from_number, step_data["no_solution"])
            # Kullanıcı bir sonraki mesajında yine "evet/hayır" ile
            # cevap verecek, step değişmiyor.
        else:
            escalate_to_technical(from_number, f"{flow} - adım {step}'de çözülemedi")
    else:
        send_text(from_number, "Lütfen 'evet' veya 'hayır' yazın.")


def escalate_to_technical(from_number, summary):
    send_text(
        from_number,
        "Bu durumu teknik ekibimize ilettim, en kısa sürede sizinle iletişime geçecekler. Teşekkür ederiz."
    )
    # TODO: Buraya teknik ekibe e-posta/Slack bildirimi eklenecek
    print(f"[ESKALASYON] {from_number}: {summary}")
    user_states.pop(from_number, None)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
