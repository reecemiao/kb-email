"""Exercise the pywin32 exporter against a minimal fake of the Outlook object model."""

from datetime import datetime, timedelta

from kbmail import outlook
from kbmail.records import calendar_index, message_index, read_message

quiet = lambda *a, **k: None


class Coll:
    def __init__(self, items):
        self._items = list(items)

    @property
    def Count(self):
        return len(self._items)

    def Item(self, i):
        if isinstance(i, str):
            return next(x for x in self._items if x.Name == i)
        return self._items[i - 1]


class Items(Coll):
    def __init__(self, items, key="ReceivedTime"):
        super().__init__(items)
        self.IncludeRecurrences = False
        self._pos = 0

    def Sort(self, prop, desc=False):
        key = prop.strip("[]")
        self._items.sort(key=lambda x: getattr(x, key), reverse=desc)

    def Restrict(self, _query):
        raise RuntimeError("locale says no")  # forces the fallback path

    def GetFirst(self):
        self._pos = 0
        return self.GetNext()

    def GetNext(self):
        if self._pos >= len(self._items):
            return None
        self._pos += 1
        return self._items[self._pos - 1]


class PA:
    def __init__(self, props):
        self.props = props

    def GetProperty(self, name):
        if name not in self.props:
            raise KeyError(name)
        return self.props[name]


class Recip:
    def __init__(self, name, email, typ=1):
        self.Name, self.Address, self.Type = name, email, typ
        self.PropertyAccessor = PA({outlook.PR_SMTP_ADDRESS: email})


class Att:
    def __init__(self, name, data, cid=""):
        self.FileName, self.Type, self._data = name, 1, data
        self.Size = len(data)
        self.PropertyAccessor = PA({outlook.PR_ATTACH_CONTENT_ID: cid} if cid else {})

    def SaveAsFile(self, path):
        with open(path, "wb") as f:
            f.write(self._data)


class Mail:
    Class = 43

    def __init__(self, subject, when, sender, to, body, msgid, html="", atts=(), conv="conv1"):
        self.Subject, self.ReceivedTime, self.Body, self.HTMLBody = subject, when, body, html
        self.BodyFormat = 2 if html else 1
        self.SenderName, self.SenderEmailAddress, self.SenderEmailType = sender[0], sender[1], "SMTP"
        self.Recipients = Coll([Recip(n, e) for n, e in to])
        self.Attachments = Coll(atts)
        self.EntryID = "EID-" + msgid
        self.PropertyAccessor = PA({outlook.PR_INTERNET_MESSAGE_ID: msgid})
        self.ConversationID, self.ConversationTopic = conv, subject
        self.Importance, self.Categories, self.MessageClass = 1, "Blue, Project", "IPM.Note"


class Appt:
    def __init__(self, subject, start, attendees):
        self.Subject, self.Start, self.End = subject, start, start + timedelta(hours=1)
        self.Location, self.Body = (
            "Room 1",
            "Agenda\n" + "_" * 30 + "\nJoin the meeting now\nteams.microsoft.com/x\n" + "_" * 30,
        )
        self.Recipients = Coll([Recip(n, e) for n, e in attendees])
        self.GlobalAppointmentID, self.EntryID = "G-" + subject, "E-" + subject
        self.IsRecurring, self.ResponseStatus, self.Categories = False, 3, ""

    def GetOrganizer(self):
        raise RuntimeError("no organizer")


class Folder:
    def __init__(self, name, items, sub=()):
        self.Name, self.FolderPath, self.StoreID = name, "\\\\me@contoso.com\\" + name, "STORE"
        self.Items = items
        self.Folders = Coll(sub)
        self.DefaultItemType = 0


class NS:
    def __init__(self, inbox, sent, cal):
        self.f = {6: inbox, 5: sent, 9: cal}

    def GetDefaultFolder(self, n):
        return self.f[n]


def _now():
    return datetime.now().replace(microsecond=0)


def _fake_ns():
    now = _now()
    html = '<p>Plan</p><img src="cid:logo1"><table><tr><td>a</td></tr></table>'
    inbox_items = [
        Mail(
            "Plan",
            now - timedelta(days=1),
            ("Sarah", "sarah@contoso.com"),
            [("Taro", "taro@contoso.com")],
            "Plan body",
            "<a@x>",
            html,
            [Att("logo.png", b"x" * 9000, "logo1"), Att("spec.txt", b"spec")],
        ),
        Mail(
            "Old", now - timedelta(days=40), ("Bob", "bob@contoso.com"), [("Taro", "taro@contoso.com")], "old", "<b@x>"
        ),
    ]
    sub = Folder(
        "Inbox\\Sub",
        Items(
            [
                Mail(
                    "In sub",
                    now - timedelta(hours=5),
                    ("Ken", "ken@x.com"),
                    [("Taro", "taro@contoso.com")],
                    "sub",
                    "<s@x>",
                )
            ]
        ),
    )
    inbox = Folder("Inbox", Items(inbox_items), [sub])
    sent = Folder(
        "Sent Items",
        Items(
            [
                Mail(
                    "RE: Plan",
                    now - timedelta(hours=10),
                    ("Taro", "taro@contoso.com"),
                    [("Sarah", "sarah@contoso.com")],
                    "ok",
                    "<c@x>",
                )
            ]
        ),
    )
    cal = Folder(
        "Calendar",
        Items(
            [
                Appt("Old sync", now - timedelta(days=60), []),
                Appt("Weekly", now + timedelta(days=1), [("Sarah", "sarah@contoso.com")]),
                Appt("Far future", now + timedelta(days=90), []),
            ]
        ),
    )
    return NS(inbox, sent, cal)


def test_export_mail_delta_and_calendar(ws, monkeypatch):
    ns = _fake_ns()
    monkeypatch.setattr(outlook, "connect", lambda: ns)
    s = outlook.export_mail(ws, days=30, log=quiet)
    assert s["exported"] == 2  # old mail outside window, subfolder skipped by default
    idx = message_index(ws)
    recs = {r["subject"]: r for r in idx.all()}
    assert recs["RE: Plan"]["direction"] == "sent" and recs["Plan"]["direction"] == "received"
    assert recs["Plan"]["thread_id"] == recs["RE: Plan"]["thread_id"]
    meta, body = read_message(ws, recs["Plan"])
    assert meta["categories"] == ["Blue", "Project"]
    att = {a["name"]: a for a in meta["attachments"]}
    assert att["logo.png"]["inline"] is True and att["spec.txt"]["inline"] is False
    assert ws.path(att["spec.txt"]["file"]).read_bytes() == b"spec"
    assert ws.path(meta["html_file"]).exists()

    # delta: second run without --days exports nothing new, and new mail is picked up
    assert outlook.export_mail(ws, log=quiet)["exported"] == 0
    ns.f[6].Items._items.append(
        Mail("New", _now(), ("Sarah", "sarah@contoso.com"), [("Taro", "taro@contoso.com")], "new", "<n@x>")
    )
    assert outlook.export_mail(ws, log=quiet)["exported"] == 1
    # subfolders on request
    assert outlook.export_mail(ws, days=30, include_subfolders=True, log=quiet)["exported"] == 1

    c = outlook.export_calendar(ws, days=30, log=quiet)
    assert c["events"] == 1
    ev = calendar_index(ws).all()[0]
    assert ev["subject"] == "Weekly" and ev["attendees"][0]["email"] == "sarah@contoso.com"
    body = ws.path(ev["path"]).read_text(encoding="utf-8")
    assert "join info removed" in body and "teams.microsoft.com" not in body


def test_com_dt_reinterprets_wall_clock():
    from datetime import timezone

    v = datetime(2026, 10, 1, 9, 30, tzinfo=timezone.utc)  # what pywin32 hands back for 09:30 local
    out = outlook.com_dt(v)
    assert (out.hour, out.minute) == (9, 30) and out.tzinfo is not None
    assert outlook.com_dt(datetime(4501, 1, 1)) is None
