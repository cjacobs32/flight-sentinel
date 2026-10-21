from types import SimpleNamespace


def _cfg():
    return SimpleNamespace(
        EMAIL_TO=["to@example.com"], SMTP_HOST="smtp.gmail.com", SMTP_PORT=587,
        SMTP_USE_TLS=True, SMTP_USER="u@example.com", SMTP_PASSWORD="app-pass",
        EMAIL_FROM="u@example.com", EMAIL_SUBJECT_PREFIX="[Flight Sentinel]",
    )


def test_smtp_test_ehlo_tls_auth_and_send(monkeypatch):
    import src.scheduled_notifier as mod
    calls=[]
    class FakeSMTP:
        def __init__(self, host, port, timeout=30):
            calls.append(("connect", host, port)); self.esmtp_features={"starttls":""}
        def ehlo(self):
            calls.append("ehlo")
            if calls.count("ehlo") >= 2:
                self.esmtp_features={"auth":"PLAIN LOGIN", "size":""}
            return 250, b"ok"
        def has_extn(self, name): return name.lower() in self.esmtp_features
        def starttls(self): calls.append("starttls"); return 220, b"ready"
        def login(self,u,p): calls.append(("login",u,p)); return 235,b"ok"
        def sendmail(self,f,t,m): calls.append("sendmail"); return {}
        def quit(self): calls.append("quit"); return 221,b"bye"
    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    out=mod.smtp_test(_cfg())
    assert out["auth"] is True and out["sent"] is True
    assert "starttls" in calls and "sendmail" in calls and "quit" in calls


def test_smtp_test_explains_missing_auth(monkeypatch):
    import src.scheduled_notifier as mod
    class FakeSMTP:
        def __init__(self,*a,**k): self.esmtp_features={"starttls":""}
        def ehlo(self): return 250,b"ok"
        def has_extn(self,name): return name.lower() in self.esmtp_features
        def starttls(self): self.esmtp_features={}; return 220,b"ready"
        def quit(self): return 221,b"bye"
    monkeypatch.setattr(mod.smtplib, "SMTP", FakeSMTP)
    try:
        mod.smtp_test(_cfg())
        assert False, "debía fallar"
    except RuntimeError as exc:
        assert "no anuncia AUTH" in str(exc)
        assert "antes de validar la contraseña" in str(exc)


def test_cloud_pipeline_accepts_smtp_test_action():
    src=open("cloud_pipeline.py", encoding="utf-8").read()
    assert 'action == "smtp_test"' in src
    assert 'smtp_test(base_cfg, send_message=True)' in src


def test_ci_documents_smtp_test():
    ci=open(".github/workflows/flight-sentinel.yml", encoding="utf-8").read()
    assert "smtp_test" in ci
