"""Corporate directory lookups against ldap3's in-memory MOCK_SYNC server."""

import pytest
import yaml

from kbmail import directory, people
from kbmail.importers import import_eml
from kbmail.workspace import Workspace

ldap3 = pytest.importorskip("ldap3")
quiet = lambda *a, **k: None
BASE = "ou=people,dc=example,dc=com"


def mock_connection(entries: dict[str, dict]):
    server = ldap3.Server("mock-directory", get_info=ldap3.NONE)
    conn = ldap3.Connection(server, user="cn=reader,dc=example,dc=com", password="pw", client_strategy=ldap3.MOCK_SYNC)
    conn.strategy.add_entry("cn=reader,dc=example,dc=com", {"userPassword": "pw", "sn": "reader"})
    for dn, attrs in entries.items():
        conn.strategy.add_entry(dn, {"objectClass": ["top", "person"], **attrs})
    conn.bind()
    return conn


def configure(ws, **directory_cfg):
    p = ws.path("kbmail.yaml")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["directory"] = {"search_base": BASE, "server": "mock-directory", **directory_cfg}
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    return Workspace(ws.root)


AD = {  # standard Active Directory style: manager is a DN
    f"cn=ceo,{BASE}": {
        "cn": "ceo",
        "mail": "ceo@contoso.com",
        "displayName": "Chief Executive",
        "title": "CEO",
        "department": "Board",
        "employeeID": "1",
    },
    f"cn=sarah,{BASE}": {
        "cn": "sarah",
        "mail": "sarah@contoso.com",
        "displayName": "Sarah Tanaka",
        "title": "Head of Payments",
        "department": "Payments",
        "employeeID": "10",
        "manager": f"cn=ceo,{BASE}",
    },
    f"cn=taro,{BASE}": {
        "cn": "taro",
        "mail": "taro@contoso.com",
        "displayName": "Taro Yamada",
        "title": "Payments Lead",
        "department": "Payments",
        "employeeID": "20",
        "manager": f"cn=sarah,{BASE}",
        "telephoneNumber": "+81 3 0000 0000",
    },
    f"cn=ops,{BASE}": {
        "cn": "ops",
        "mail": "ops@contoso.com",
        "displayName": "Ops Bot Owner",
        "title": "Engineer",
        "department": "Payments",
        "employeeID": "30",
        "manager": f"cn=sarah,{BASE}",
    },
    f"cn=hanako,{BASE}": {
        "cn": "hanako",
        "mail": "hanako@contoso.com",
        "displayName": "佐藤 花子",
        "title": "Analyst",
        "department": "Payments",
        "employeeID": "40",
        "manager": f"cn=taro,{BASE}",
    },
}


def test_lookup_resolves_manager_dn(ws):
    d = directory.Directory(configure(ws), mock_connection(AD))
    rec = d.lookup("TARO@contoso.com ")
    assert rec["found"] and rec["fields"]["title"] == "Payments Lead" and rec["fields"]["employee_id"] == "20"
    assert rec["relations"]["manager"] == {
        "name": "Sarah Tanaka",
        "email": "sarah@contoso.com",
        "id": "10",
        "title": "Head of Payments",
    }
    assert "telephoneNumber" not in rec["fields"]  # only mapped fields are kept
    assert d.lookup("nobody@contoso.com") == {"found": False}


def test_sync_and_org_relations_in_people_digest(ws, mailbox):
    import_eml(ws, [str(mailbox)], log=quiet)
    ws = configure(ws)
    people.build(ws, log=quiet)
    stats = directory.sync(ws, connection=mock_connection(AD), log=quiet)
    assert stats["looked_up"] >= 4
    state = directory.load(ws)
    assert state["chain"] == ["sarah@contoso.com", "ceo@contoso.com"]
    me = ws.me_emails
    rel = {e: directory.org_relation(e, state, me) for e in state["people"]}
    assert rel["sarah@contoso.com"] == "manager" and rel["ceo@contoso.com"] == "skip-manager (+1)"
    assert rel["hanako@contoso.com"] == "direct-report" and rel["ops@contoso.com"] == "peer"
    assert rel.get("noreply@news.example.org", "") == ""  # not in the directory
    # Cached: a second sync does no lookups.
    assert directory.sync(ws, connection=mock_connection(AD), log=quiet)["looked_up"] == 0
    out = people.build(ws, log=quiet)
    assert out["ceo@contoso.com"]["no_recent_mail"]  # key org contact added although no recent mail
    digest = ws.path(people.DIGEST).read_text(encoding="utf-8")
    assert "manager; Head of Payments; Payments; reports to Chief Executive" in digest
    assert "direct-report; Analyst" in digest
    assert "+81 3" not in digest  # phone numbers stay out of the digest


def test_custom_schema_like_level_scope_and_id_references(ws):
    """A directory where managers/secretaries are referenced by staff id, not DN."""
    entries = {
        f"cn=900,{BASE}": {
            "cn": "900",
            "mail": "boss@corp.example",
            "displayName": "LEE, Boss",
            "x-workrole": "Head of Ops",
            "employeeID": "900",
        },
        f"cn=901,{BASE}": {
            "cn": "901",
            "mail": "pa@corp.example",
            "displayName": "WONG, Assistant",
            "employeeID": "901",
        },
        f"cn=100,{BASE}": {
            "cn": "100",
            "mail": "me@corp.example",
            "displayName": "YAMADA, Taro",
            "x-workrole": "Engineer",
            "employeeID": "100",
            "x-linemanagerid": "900",
            "x-linemanagername": "LEE, Boss",
            "x-functionalmanagername": "CHAN, Func",
            "x-secretaryid": "901",
        },
    }
    ws = configure(
        ws,
        scope="level",
        email_filter="(&(objectClass=top)(mail={email}))",
        id_filter="(cn={id})",
        fields={"name": ["displayName"], "email": ["mail"], "title": ["x-workrole"], "employee_id": ["employeeID"]},
        relations={
            "manager": {"id": ["x-linemanagerid"], "name": ["x-linemanagername"]},
            "functional_manager": {"id": ["x-functionalmanagerid"], "name": ["x-functionalmanagername"]},
            "assistant": {"id": ["x-secretaryid"]},
        },
    )
    rec = directory.Directory(ws, mock_connection(entries)).lookup("me@corp.example")
    rels = rec["relations"]
    assert rels["manager"]["email"] == "boss@corp.example" and rels["manager"]["title"] == "Head of Ops"
    assert rels["functional_manager"] == {"name": "CHAN, Func", "email": "", "id": "", "title": ""}  # name only
    assert rels["assistant"]["name"] == "WONG, Assistant" and rels["assistant"]["id"] == "901"
    # Filter values are escaped: an address with LDAP special characters cannot widen the search.
    assert directory.Directory(ws, mock_connection(entries)).lookup("*)(mail=*")["found"] is False


def test_connect_requires_server_and_never_prompts_when_non_interactive(ws, monkeypatch):
    with pytest.raises(directory.DirectoryError, match=r"directory\.server"):
        directory.connect(ws)
    ws = configure(ws, use_ssl=False, auth="ntlm", user="CORP\\me")
    monkeypatch.delenv(directory.PASSWORD_ENV, raising=False)
    monkeypatch.setattr("getpass.getpass", lambda *a: pytest.fail("must not prompt"))
    with pytest.raises(directory.DirectoryError, match="KBMAIL_DIRECTORY_PASSWORD"):
        directory.connect(ws, interactive=False)
