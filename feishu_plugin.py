import lark_oapi as lark
from lark_oapi.api.im.v1 import *
import json
import time
from datetime import datetime

# ===================== Fill in your credentials =====================
APP_ID = "cli_a94e44dd62235bd2"
APP_SECRET = "MFAke0AcqWxIy6trp2wJ5fPrKy6jTFL0"
# ===================================================================

# Initialize the REST client
client = (lark.Client.builder()
    .app_id(APP_ID)
    .app_secret(APP_SECRET)
    .log_level(lark.LogLevel.DEBUG)
    .build())


def handle_message(data: P2ImMessageReceiveV1):
    try:
        sender_id = data.event.sender.sender_id.open_id
        msg_type = data.event.message.message_type
        content = json.loads(data.event.message.content)

        # Print incoming message
        print("=" * 60)
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Message received")
        print(f"  Type    : {msg_type}")
        print(f"  Content : {content}")
        print(f"  From    : {sender_id}")
        print("=" * 60)

        # Reply with hello + timestamp
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reply = f"Hello! (received at {ts})"
        send_message(sender_id, reply)

    except Exception as e:
        print(f"Error handling message: {e}")


def send_message(open_id: str, text: str):
    try:
        req = (CreateMessageRequest.builder()
            .receive_id_type("open_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(open_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build())

        resp = client.im.v1.message.create(req)
        if not resp.success():
            print(f"Send failed: code={resp.code} msg={resp.msg}")
        else:
            print(f"Reply sent: {text}")
    except Exception as e:
        print(f"Error sending message: {e}")


def start_long_connection():
    print("Starting Feishu WebSocket long connection...")

    dispatcher = (lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .build())

    ws_client = lark.ws.Client(
        APP_ID,
        APP_SECRET,
        event_handler=dispatcher,
        log_level=lark.LogLevel.DEBUG,
    )
    ws_client.start()


if __name__ == "__main__":
    while True:
        try:
            start_long_connection()
        except Exception as e:
            print(f"Connection dropped, reconnecting in 5s: {e}")
            time.sleep(5)
