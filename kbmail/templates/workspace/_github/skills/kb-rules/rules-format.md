# config/rules.yaml format

```yaml
threshold: 2.0            # an area is assigned when the summed weight of matching rules reaches this
rules:
  - id: r001              # stable; new rules continue the sequence
    area: area1           # a configured area, or "others"
    description: Vendor X mail is always area1
    weight: 3
    when:                 # ALL listed fields must match; a field matches if ANY of its patterns matches
      from_domain: [vendorx.com]
      subject: ["re:\\bPRJ-\\d+", "steering"]
    unless:               # optional: the rule is ignored if ANY of these match
      subject: [newsletter]
    disabled: false       # keep a rule but switch it off (say why in description)
```

Fields: `from`, `from_domain`, `to`, `cc`, `participants`, `participant_domain`, `subject`, `body`,
`folder`, `direction` (sent/received), `categories` (Outlook categories), `attachment` (file names),
`source` (outlook/teams).

Patterns are case-insensitive substrings; prefix `re:` for a regular expression. Domain fields match
the domain or any subdomain. Several areas can be assigned when several reach the threshold; `others`
only applies when no area does.

Weights: a signal strong enough on its own gets `weight = threshold`; weaker signals get less, so two
must agree. `kbmail rules commit` rejects unknown areas/fields, bad regexes and duplicate ids.
