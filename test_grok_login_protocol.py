"""离线协议检查：python3 -m unittest test_grok_login_protocol.py。"""

import unittest
from unittest.mock import Mock, patch

from grok_login_protocol import enc_message, login_one, login_payload, session_cookie
from grok_reset_pwd import enc_str, grpc_web_frame


class ProtocolLoginTest(unittest.TestCase):
    def test_password_request_and_confirmed_session(self):
        expected = bytes.fromhex("0000000016 0a0c 0a0a 0a056140622e63 120170 2203 0a0174 520163")
        self.assertEqual(login_payload("a@b.c", "p", "t", "c"), expected)
        # 响应里的 one_time_link_tokens 不能误当作 SSO。
        body = enc_message(1, b"\x28\x02") + enc_str(2, "new-sso") + enc_str(3, "one-time-token")
        self.assertEqual(session_cookie(body), "new-sso")
        for invalid in (
            enc_message(1, b"\x28\x01") + enc_str(2, "pending-sso"),
            enc_message(1, b"\x28\x02") + enc_str(3, "not-an-sso"),
            b"\x12\x7fshort",
        ):
            with self.assertRaises(ValueError):
                session_cookie(invalid)

        session = Mock()
        session.get.return_value = Mock(status_code=200, text='sitekey="0x4test"')
        trailer = b"grpc-status: 0\r\n"
        response = Mock(status_code=200, headers={}, content=grpc_web_frame(body) + b"\x80" + len(trailer).to_bytes(4, "big") + trailer)
        session.post.return_value = response
        solver = Mock()
        solver.get_response.return_value = "turnstile"
        with patch("grok_login_protocol.requests.Session") as constructor:
            constructor.return_value.__enter__.return_value = session
            self.assertEqual(login_one("a@b.c", "password", solver, 30), "new-sso")
            session.post.assert_called_once()
            self.assertTrue(session.post.call_args.args[0].endswith("/auth_mgmt.AuthManagement/CreateSession"))
            self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
            response.content = b"\x80\x00\x00\x00\x10grpc-status: 7\r\n"
            with self.assertRaises(ValueError):
                login_one("a@b.c", "password", solver, 30)


if __name__ == "__main__":
    unittest.main()
