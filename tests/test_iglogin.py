"""ig-login: yeniden giriş, 2FA (accounts/login'i tekrar göndermeden), oturum kaydı, hata mesajları. Ağa çıkmaz."""
import pytest
import requests
from instagrapi import exceptions as ex

from igsaved import cli, ig_private
from igsaved.config import Config


class FakeResponse:
    headers = {"ig-set-authorization": "Bearer IGT:2:yeni"}


class FakeClient:
    """instagrapi Client'ın ig_private'in dokunduğu yüzeyi."""

    def __init__(self, login_errors=(), two_factor_info=None, private_error=None, authorize_on_login=True):
        self.login_errors = list(login_errors)
        self.login_calls = []
        self.private_calls = []
        self.private_error = private_error
        self.authorize_on_login = authorize_on_login
        self.authorization_data = {}
        self.dumped = None
        self.last_json = {"two_factor_info": two_factor_info} if two_factor_info is not None else {}
        self.last_response = FakeResponse()
        self.phone_id, self.token, self.uuid, self.android_device_id = "ph", "csrf", "gu", "android-1"
        self.username = "kullanici"
        self.flow_ran = False

    @property
    def user_id(self):
        return self.authorization_data.get("ds_user_id")

    def _authorize(self):
        self.authorization_data = {"ds_user_id": "42", "sessionid": "s"}

    def login(self, username, password, verification_code=""):
        self.login_calls.append((username, password, verification_code))
        if self.login_errors:
            raise self.login_errors.pop(0)
        if self.authorize_on_login:
            self._authorize()
        return True

    def private_request(self, endpoint, data=None, login=False):
        self.private_calls.append((endpoint, data, login))
        if self.private_error:
            raise self.private_error
        return {"status": "ok"}

    def parse_authorization(self, header):
        return {"ds_user_id": "42", "sessionid": "s"} if header else {}

    def login_flow(self):
        self.flow_ran = True

    def dump_settings(self, path):
        self.dumped = path


def _cfg(tmp_path):
    cfg = Config.load(None)
    cfg.raw["instagrapi"]["username"] = "kullanici"
    cfg.raw["instagrapi"]["session_file"] = str(tmp_path / "sess.json")
    return cfg


def _patch(monkeypatch, tmp_path, client, inputs=("123456",)):
    answers = iter(inputs)
    monkeypatch.setattr(ig_private, "_client", lambda cfg: (client, tmp_path / "sess.json"))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sifre")
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))


def test_login_success_saves_session_and_sets_turkish_handlers(monkeypatch, tmp_path):
    fc = FakeClient()
    _patch(monkeypatch, tmp_path, fc)
    assert ig_private.interactive_login(_cfg(tmp_path)) == "kullanici"
    assert fc.login_calls == [("kullanici", "sifre", "")] and fc.dumped == tmp_path / "sess.json"
    assert fc.challenge_code_handler is ig_private._challenge_code_prompt
    assert fc.change_password_handler is ig_private._change_password_prompt


def test_two_factor_submits_with_first_identifier_without_second_login(monkeypatch, tmp_path):
    fc = FakeClient(login_errors=[ex.TwoFactorRequired("2FA")],
                    two_factor_info={"two_factor_identifier": "ILK-ID", "sms_two_factor_on": True})
    _patch(monkeypatch, tmp_path, fc, inputs=["", "12 34 56"])        # boş giriş tekrar sorulur, boşluklar temizlenir
    ig_private.interactive_login(_cfg(tmp_path))
    assert len(fc.login_calls) == 1                                    # accounts/login ikinci kez gönderilmedi
    endpoint, data, login_flag = fc.private_calls[0]
    assert endpoint == "accounts/two_factor_login/" and login_flag is True
    assert data["two_factor_identifier"] == "ILK-ID" and data["verification_code"] == "123456"
    assert data["device_id"] == "android-1" and data["_csrftoken"] == "csrf"
    assert fc.flow_ran and fc.dumped is not None


def test_two_factor_without_identifier_uses_library_path(monkeypatch, tmp_path):
    fc = FakeClient(login_errors=[ex.TwoFactorRequired("bloks")], two_factor_info={})
    _patch(monkeypatch, tmp_path, fc, inputs=["654321"])
    ig_private.interactive_login(_cfg(tmp_path))
    assert fc.login_calls[-1] == ("kullanici", "sifre", "654321") and fc.private_calls == []


def test_backup_code_uses_library_path(monkeypatch, tmp_path):
    fc = FakeClient(login_errors=[ex.TwoFactorRequired("2FA")], two_factor_info={"two_factor_identifier": "ID"})
    _patch(monkeypatch, tmp_path, fc, inputs=["12345678"])
    ig_private.interactive_login(_cfg(tmp_path))
    assert fc.login_calls[-1][2] == "12345678" and fc.private_calls == []


def test_invalid_parameters_falls_back_to_library(monkeypatch, tmp_path):
    fc = FakeClient(login_errors=[ex.TwoFactorRequired("2FA")], two_factor_info={"two_factor_identifier": "ID"},
                    private_error=ex.UnknownError("invalid parameters"))
    _patch(monkeypatch, tmp_path, fc, inputs=["111111"])
    ig_private.interactive_login(_cfg(tmp_path))
    assert fc.login_calls[-1][2] == "111111"


def test_wrong_code_becomes_two_factor_error(monkeypatch, tmp_path):
    fc = FakeClient(login_errors=[ex.TwoFactorRequired("2FA")], two_factor_info={"two_factor_identifier": "ID"},
                    private_error=ex.UnknownError("Please check the security code"))
    _patch(monkeypatch, tmp_path, fc, inputs=["000000"])
    with pytest.raises(ex.TwoFactorRequired) as info:
        ig_private.interactive_login(_cfg(tmp_path))
    assert "security code" in ig_private.login_error_message(info.value)
    assert fc.dumped is None


def test_session_saved_when_request_after_authorization_fails(monkeypatch, tmp_path):
    class WarmupFails(FakeClient):
        def login(self, username, password, verification_code=""):
            self._authorize()
            raise ex.FeedbackRequired("feed warm-up")
    fc = WarmupFails()
    _patch(monkeypatch, tmp_path, fc)
    with pytest.raises(ex.FeedbackRequired):
        ig_private.interactive_login(_cfg(tmp_path))
    assert fc.dumped == tmp_path / "sess.json"                         # onaylanan oturum kaybolmadı


def test_failed_login_does_not_save(monkeypatch, tmp_path):
    fc = FakeClient(login_errors=[ex.BadPassword("wrong")])
    _patch(monkeypatch, tmp_path, fc)
    with pytest.raises(ex.BadPassword):
        ig_private.interactive_login(_cfg(tmp_path))
    assert len(fc.login_calls) == 1 and fc.dumped is None


def test_unauthorized_result_is_not_reported_as_success(monkeypatch, tmp_path):
    fc = FakeClient(authorize_on_login=False)
    _patch(monkeypatch, tmp_path, fc)
    with pytest.raises(ex.ClientError):
        ig_private.interactive_login(_cfg(tmp_path))
    assert fc.dumped is None


def test_challenge_prompt_names_channel_and_validates(monkeypatch):
    prompts = []
    answers = iter(["abc", "482913"])
    monkeypatch.setattr("builtins.input", lambda prompt="": (prompts.append(prompt), next(answers))[1])
    assert ig_private._challenge_code_prompt("u", "ChallengeChoice.EMAIL") == "482913"
    assert "e-posta" in prompts[0] and len(prompts) == 2


def test_change_password_prompt_uses_getpass(monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "yeni-sifre")
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("yeni şifre açık yazılmamalı"))
    assert ig_private._change_password_prompt("u") == "yeni-sifre"


@pytest.mark.parametrize("exc, needle", [
    (ex.BadPassword("x"), "şifre kabul edilmedi"),
    (ex.TwoFactorRequired("x"), "doğrulama kodu kabul edilmedi"),
    (ex.ChallengeRequired(), "Bu bendim"),
    (ex.ChallengeUnknownStep("x"), "Bu bendim"),
    (ex.ChallengeSelfieCaptcha("x"), "selfie/captcha"),
    (ex.PleaseWaitFewMinutes("x"), "sınırlıyor"),
    (ex.ProxyAddressIsBlocked("x"), "kullanıcı adını tanımadı"),
    (ex.AccountSuspended("x"), "askıya alınmış"),
    (ex.ClientConnectionError("x"), "İnternet bağlantısı"),
    (requests.ConnectionError("x"), "İnternet bağlantısı"),
    (KeyError("step_data"), "doğrulama akışı beklenmedik"),
    (ex.UnknownError("garip"), "UnknownError"),
])
def test_login_error_message_mapping(exc, needle):
    assert needle in ig_private.login_error_message(exc)


def test_unknown_python_error_is_not_swallowed():
    assert ig_private.login_error_message(ValueError("bug")) is None


def test_cmd_ig_login_reports_instead_of_traceback(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(ig_private, "interactive_login", lambda cfg: (_ for _ in ()).throw(ex.ChallengeRequired()))
    assert cli.cmd_ig_login(_cfg(tmp_path)) == cli.EXIT_HARDSTOP
    assert "GİRİŞ OLMADI" in caplog.text and "Bu bendim" in caplog.text


def test_cmd_ig_login_cancel(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(ig_private, "interactive_login", lambda cfg: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert cli.cmd_ig_login(_cfg(tmp_path)) == cli.EXIT_ERROR
    assert "İPTAL" in caplog.text


def test_cmd_ig_login_reraises_real_bugs(monkeypatch, tmp_path):
    monkeypatch.setattr(ig_private, "interactive_login", lambda cfg: (_ for _ in ()).throw(ValueError("bug")))
    with pytest.raises(ValueError):
        cli.cmd_ig_login(_cfg(tmp_path))


def test_error_before_new_session_does_not_resave_old_one(monkeypatch, tmp_path, caplog):
    fc = FakeClient(login_errors=[ex.ClientConnectionError("ag yok")])
    fc.authorization_data = {"ds_user_id": "42", "sessionid": "ESKI-OLU"}      # dosyadan yüklenen ölü oturum
    _patch(monkeypatch, tmp_path, fc)
    with pytest.raises(ex.ClientConnectionError):
        ig_private.interactive_login(_cfg(tmp_path))
    assert fc.dumped is None and "oturum kaydedildi" not in caplog.text
