# Corporate directory (LDAP / Active Directory)

Mail shows *how* you work with people. The directory says *who they are*: title, department,
and above all **reporting lines**. With `directory.*` configured, kbmail looks up your contacts by
e-mail and works out your org relationships deterministically, so `/kb-people` only infers what
the directory can't tell (stakeholder, approver, escalation point):

| org relation | meaning |
|---|---|
| `manager` | your manager (from your own directory entry) |
| `skip-manager (+n)` | n levels above your manager (`directory.chain_depth`) |
| `direct-report` | their manager is you |
| `indirect-report` | their manager reports to you |
| `peer` | same manager as you |
| `same-department` | same department attribute as you |
| `colleague` | in the directory, none of the above |

People in your manager chain and your reports are added to the people digest even when they
haven't mailed you recently.

## Setup

```powershell
uv tool install -e ".[directory]" --reinstall     # ldap3, pycryptodome (NTLM), winkerberos (Windows)
kbmail directory test                             # bind + look yourself up
kbmail directory lookup someone@example.com       # one person as JSON
kbmail directory sync                             # you + people digest -> state/directory.json
```

`kbmail people build` (part of `kbmail sync`) then fills the `org` column of
`work/people/PEOPLE_DIGEST.md`, and `/kb-people` uses it. With `directory.enabled: true`,
`kbmail sync` also refreshes the cache. It never prompts, so it needs Kerberos or the password in
the environment (see below).

## Configuration

Put your company's values in your **workspace** `kbmail.yaml` (or `.env`), not in the tool repo.
The defaults fit a standard Active Directory:

```yaml
directory:
  enabled: true
  server: ldap.example.com
  port: 636                  # LDAPS; 3269 = AD global catalog over SSL
  use_ssl: true
  ca_certs_file: 'C:\Users\me\certs\corp-ca.pem'   # your company's CA bundle (single quotes for backslashes)
  auth: ntlm                 # ntlm | kerberos | simple | anonymous
  user: ''                   # empty = %USERDOMAIN%\%USERNAME%
  search_base: 'OU=People,DC=example,DC=com'
  scope: subtree             # subtree | level | base
  email_filter: '(|(mail={email})(proxyAddresses=smtp:{email}))'
  id_filter: '(|(employeeID={id})(cn={id}))'
  fields:                    # kbmail name -> LDAP attributes, first non-empty wins
    name: [displayName]
    email: [mail]
    title: [title]
    department: [department]
    company: [company]
    office: [physicalDeliveryOfficeName]
    country: [co]
    employee_id: [employeeID]
  relations:                 # references to other people
    manager: {dn: [manager], id: [], name: []}
    assistant: {dn: [assistant], id: [], name: []}
  digest_fields: [title, department, company, office, country]
```

### Custom directory schemas

Many companies extend the schema. Map their attributes with `fields` and `relations`:

- **Extra fields:** any name works, e.g. `grade: [extensionAttribute3]`, `cost_center: [departmentNumber]`,
  `responsibilities: [x-responsibilities]`. `kbmail directory lookup` prints them all, and
  `digest_fields` chooses which appear in the people digest.
- **Relations stored as a DN** (standard `manager`): `{dn: [manager]}`.
- **Relations stored as an id** (a staff number): `{id: [x-managerid], name: [x-managername]}`. The id
  is looked up with `id_filter`, e.g. `'(cn={id})'` if `cn` holds the staff number. `name` is the
  fallback when the id doesn't resolve.
- **Several kinds of manager:** add more relations, e.g. `functional_manager: {id: [...], name: [...]}`.
  Only the relation called `manager` drives the reporting-line analysis. The others are listed.
- **Directory structure:** `scope: level` searches only one level below `search_base`. Filters can
  add conditions, e.g. `'(&(objectClass=top)(mail={email}))'`.

Values substituted into `{email}` and `{id}` are LDAP-escaped.

### Authentication

| `auth` | how | password |
|---|---|---|
| `ntlm` | `DOMAIN\user` + password (what most corporate scripts use) | prompted; or `KBMAIL_DIRECTORY_PASSWORD` in the environment |
| `kerberos` | your Windows sign-in (SASL/GSSAPI via winkerberos) | none (works in scheduled `kbmail sync`) |
| `simple` | bind DN / UPN + password | as NTLM |
| `anonymous` | no bind | none |

The password is never written to disk by kbmail. `KBMAIL_DIRECTORY_PASSWORD` is read only from the
environment, and it is not a setting. Avoid putting it in `.env`. If you do, `.env` is git-ignored,
and `kbmail git check` refuses to commit it.

NTLM needs MD4, which current OpenSSL builds lack. The `directory` extra installs `pycryptodome`,
which ldap3 uses instead.

## Privacy

- `state/directory.json` holds personal data (names, titles, ids, whatever you map). It is
  git-ignored and is refreshed after `directory.cache_days`.
- Map only what helps you understand working relationships. `digest_fields` keeps phone numbers
  and addresses out of the people digest, and so out of the wiki, even if you map them for your own
  lookups.
- Lookups are read-only and limited to `directory.max_lookups` per run, most active contacts first.
