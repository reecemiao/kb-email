from kbmail.noise import is_empty_response, is_meeting_response


def test_simplified_chinese_meeting_responses(ws):
    for subject in ("已接受: Weekly", "已拒绝\uff1a Weekly", "暂定: Weekly"):
        assert is_meeting_response({"subject": subject}), subject
    assert not is_meeting_response({"subject": "Weekly notes"})
    invite = "\n\n-----原始约会-----\nFrom: Taro\nWhen: Monday\nAgenda"
    assert is_empty_response(ws, {"subject": "已接受: Weekly"}, "Thanks" + invite)
    assert not is_empty_response(ws, {"subject": "已拒绝: Weekly"}, "On leave, please send the notes." + invite)
