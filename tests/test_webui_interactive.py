from tg_signer.config import SendTextAction
from tg_signer.webui.interactive import build_sign_chats, parse_chat_ids


def test_parse_chat_ids_supports_multiple_separators():
    assert parse_chat_ids("10001, 10002\n10003 10002；10004") == [
        10001,
        10002,
        10003,
        10004,
    ]


def test_build_sign_chats_expands_multiple_chat_ids():
    chats = build_sign_chats(
        [10001, 10002],
        message_thread_id=3,
        name="",
        delete_after=5,
        actions=[SendTextAction(text="签到")],
        chat_labels={10001: "群组A", 10002: "群组B"},
    )

    assert [chat.chat_id for chat in chats] == [10001, 10002]
    assert chats[0].name == "群组A"
    assert chats[1].name == "群组B"
    assert chats[0].message_thread_id == 3
    assert chats[0].delete_after == 5
    assert chats[0].actions[0].text == "签到"
    assert chats[0].actions[0] is not chats[1].actions[0]
