import time
from unittest.mock import Mock


def test_session_probe_uses_requested_personal_host_and_resolves_relative_redirect(monkeypatch):
    from backend.feishu_web_login import is_feishu_session_server_valid

    first = Mock(status_code=302, headers={"Location": "/drive/home/"})
    second = Mock(status_code=200, headers={})
    get = Mock(side_effect=[first, second])
    monkeypatch.setattr("backend.feishu_web_login.requests.get", get)

    result = is_feishu_session_server_valid(
        {"session": "redacted", "issued_at": time.time()},
        probe_url="https://dcnhd2xewyfq.feishu.cn/",
    )

    assert result["valid"] is True
    assert get.call_args_list[0].args[0] == "https://dcnhd2xewyfq.feishu.cn/"
    assert get.call_args_list[1].args[0] == "https://dcnhd2xewyfq.feishu.cn/drive/home/"


def test_session_probe_marks_login_redirect_invalid(monkeypatch):
    from backend.feishu_web_login import is_feishu_session_server_valid

    response = Mock(
        status_code=302,
        headers={"Location": "https://accounts.feishu.cn/login"},
    )
    monkeypatch.setattr("backend.feishu_web_login.requests.get", Mock(return_value=response))

    result = is_feishu_session_server_valid(
        {"session": "redacted", "issued_at": time.time()},
        probe_url="https://dcnhd2xewyfq.feishu.cn/",
    )

    assert result["valid"] is False
    assert "login" in result["reason"]
