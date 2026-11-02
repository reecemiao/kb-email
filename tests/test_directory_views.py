"""Directory views and config files: a company 'card' defined purely in YAML, standalone use."""

import json
import subprocess
import textwrap

import pytest
import yaml

from kbmail import directory, directory_views
from kbmail.cli import main
from kbmail.workspace import Workspace

ldap3 = pytest.importorskip("ldap3")
from tests.test_directory import BASE, mock_connection  # noqa: E402

# A directory with company-specific attribute names (placeholders standing in for a real schema).
ENTRIES = {
    f"cn=900,{BASE}": {"cn": "900", "mail": "boss@corp.example", "displayName": "LEE, Boss", "employeeID": "900"},
    f"cn=901,{BASE}": {"cn": "901", "mail": "pa@corp.example", "displayName": "", "x-notesmail": "PA Wong/Corp"},
    f"cn=100,{BASE}": {
        "cn": "100",
        "mail": "me@corp.example",
        "displayName": "Taro YAMADA",
        "employeeID": "100",
        "givenName": "Taro",
        "middleName": "J",
        "x-knownaslastname": "yamada",
        "x-workrole": "Engineer",
        "x-grade": "U",
        "employeeType": "E",
        "x-domain": "CORP",
        "x-sam": "100",
        "x-tel": "1234",
        "x-tieline": "5678",
        "x-cc": "852",
        "x-mobprefix": "9",
        "x-mobno": "1234567",
        "postalAddress": "Level 1\r\nTower A\n\nHong Kong",
        "x-linemanagerid": "900",
        "x-linemanagername": "LEE, Boss",
        "x-secretaryid": "901",
        "personalTitle": "",
    },
}

PROFILE = textwrap.dedent(r"""
    server: ldap.corp.example
    search_base: 'ou=people,dc=example,dc=com'
    scope: level
    email_filter: '(&(objectClass=top)(mail={email}))'
    id_filter: '(&(objectClass=top)(cn={id}))'
    fields:
      name: [displayName, x-notesmail]
      email: [mail]
      title: [x-workrole]
    relations:
      manager: {id: [x-linemanagerid], name: [x-linemanagername]}
      assistant: {id: [x-secretaryid]}
    view: card
    computed:
      given: {join: " ", parts: ["{givenName}", "{middleName}"]}
      personal: {join: ", ", parts: ["{x-knownaslastname|upper}", "{=given}"]}
    views:
      card:
        Basic Information:
          Staff ID: "{employeeID}"
          Name: "{displayName}[ ({=personal})]"
          Job Title: "{x-workrole}"
          GCB Level: "{x-grade|map:U=Unknown|default:Unknown}"
          Employee Type: "{employeeType|map:E=Employee;*=Contractor}"
          AD Account: {join: "\\", parts: ["{x-domain}", "{x-sam}"]}
          Personal Title: {value: "{personalTitle|upper}", omit_empty: true}
        Detailed Information:
          Entity Manager: "{@manager.name}[ ({@manager.id})]"
          Internal Tel Number: "{x-tel}[  (Tieline: {x-tieline})]"
          Mobile:
            value: "[+{x-cc} ][({x-areacode}) ][{x-mobprefix} ]{x-mobno}"
            when: "{x-mobprefix}{x-mobno}"
          Office Location: "{postalAddress|oneline}"
          Alternative Contact: {value: "{@assistant.name}[ ({@assistant.id})]", omit_empty: true}
""")


def _cfg(tmp_path):
    f = tmp_path / "directory.yaml"
    f.write_text(PROFILE, encoding="utf-8")
    return directory.load_config(str(f)), f


def test_company_card_from_yaml_only(tmp_path):
    cfg, _ = _cfg(tmp_path)
    card = directory.Directory(cfg, mock_connection(ENTRIES)).card("me@corp.example")
    basic, detail = card["Basic Information"], card["Detailed Information"]
    assert basic == {
        "Staff ID": "100",
        "Name": "Taro YAMADA (YAMADA, Taro J)",
        "Job Title": "Engineer",
        "GCB Level": "Unknown",
        "Employee Type": "Employee",
        "AD Account": "CORP\\100",
    }  # Personal Title omitted: empty
    assert detail["Entity Manager"] == "LEE, Boss (900)"
    assert detail["Internal Tel Number"] == "1234  (Tieline: 5678)"
    assert detail["Mobile"] == "+852 9 1234567"  # no area code -> that part dropped
    assert detail["Office Location"] == "Level 1, Tower A, Hong Kong"
    assert detail["Alternative Contact"] == "PA Wong/Corp (901)"  # name falls back to the Notes address
    # The plain record is still available, and unknown views are reported.
    d = directory.Directory(cfg, mock_connection(ENTRIES))
    assert d.card("me@corp.example", "record")["relations"]["manager"]["email"] == "boss@corp.example"
    with pytest.raises(directory.DirectoryError, match="unknown view"):
        d.card("me@corp.example", "nope")
    assert d.card("ghost@corp.example") == {"found": False, "email": "ghost@corp.example"}


def test_views_request_the_attributes_they_use(tmp_path):
    cfg, _ = _cfg(tmp_path)
    attrs = {a.casefold() for a in directory.Directory(cfg, mock_connection(ENTRIES)).attributes}
    assert {"x-tieline", "x-mobno", "postaladdress", "x-knownaslastname", "x-linemanagerid"} <= attrs
    used = directory_views.attributes_used({"v": {"a": "{=x}{@manager.name}{mail|upper}"}}, {})
    assert used == {"mail"}


def test_template_edge_cases():
    r = directory_views.render(
        {"a": "[literal] {x|lower}", "b": {"value": "{x}", "when": "{missing}"}, "c": {"join": "-", "parts": []}},
        {"x": ["HELLO"]},
        {},
    )
    assert r == {"a": "[literal] hello", "b": "", "c": ""}
    person = {"first": ["[{n} ({i})]", "{n}", "{i}"]}
    assert directory_views.render({"p": person}, {"n": "Ann", "i": "7"}, {}) == {"p": "Ann (7)"}
    assert directory_views.render({"p": person}, {"i": "7"}, {}) == {"p": "7"}
    with pytest.raises(ValueError, match="itself"):
        directory_views.render({"a": "{=loop}"}, {}, {}, {"loop": "{=loop}"})


def test_directory_yaml_overrides_kbmail_yaml_and_stays_out_of_git(ws, tmp_path):
    p = ws.path("kbmail.yaml")
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg["directory"] = {"server": "from-kbmail-yaml", "port": 636}
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    ws.path("config/directory.yaml").write_text("server: from-directory-yaml\n", encoding="utf-8")
    w = Workspace(ws.root)
    assert w.cfg("directory.server") == "from-directory-yaml" and w.cfg("directory.port") == 636
    assert w.config_sources["directory.server"] == "config/directory.yaml"
    assert ws.path("config/directory.example.yaml").exists()
    subprocess.run(["git", "init", "-q", str(ws.root)], check=True)
    ignored = subprocess.run(["git", "-C", str(ws.root), "check-ignore", "config/directory.yaml"], capture_output=True)
    assert ignored.returncode == 0


def test_standalone_cli_with_config_file(tmp_path, monkeypatch, capsys):
    _, f = _cfg(tmp_path)
    monkeypatch.chdir(tmp_path)  # not a workspace
    monkeypatch.setattr(directory, "connect", lambda cfg, **k: mock_connection(ENTRIES))
    assert main(["directory", "lookup", "--config", str(f), "me@corp.example"]) == 0
    card = json.loads(capsys.readouterr().out)
    assert card["Basic Information"]["Name"].startswith("Taro YAMADA")
    assert main(["directory", "lookup", "--config", str(f), "--view", "record", "me@corp.example"]) == 0
    assert json.loads(capsys.readouterr().out)["fields"]["title"] == "Engineer"
    # Without any settings it explains where they go.
    monkeypatch.setattr(directory, "user_config_path", lambda: tmp_path / "none.yaml")
    assert main(["directory", "lookup", "x@y.z"]) == 1
    assert "config/directory.yaml" in capsys.readouterr().err


def test_directory_init(ws, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(ws.root)
    assert main(["directory", "init"]) == 0
    assert ws.path("config/directory.yaml").read_text(encoding="utf-8").startswith("# Corporate directory")
    target = tmp_path / "elsewhere" / "dir.yaml"
    assert main(["directory", "init", "--config", str(target)]) == 0 and target.exists()
