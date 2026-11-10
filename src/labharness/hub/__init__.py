"""The Kelvin hub: online accounts for Kelvin users across labs, and a home for the work they share.

Kelvin itself keeps accounts per computer (accounts.py). The hub is one server that many Kelvins sign in to:
people register once (with their consent to the terms and privacy notice), and each Kelvin they sign in to sends
the hub what its person shares, once they switch sharing on in Kelvin (and while hub.json "sharing", on by
default, lets the hub take it):

- **Shared once switched on:** normal projects, unfiled chats with their whole conversations, and the records of
  finished runs. Runs' data files only while hub.json "run_files" is on (off by default).
- **Never sent:** projects marked unpublished or confidential (or limited to some models), incognito chats,
  API keys, and ChatGPT or Claude Code sign-ins. The hub refuses them even if a Kelvin sends them.
- **Who sees it:** its person, and the Kelvin team in the owner console (each opening goes in the person's
  record). While hub.json "labs" is on (off by default), also the other members of the lab it was shared in,
  while both are members.

Everyone is equal: there is no lab owner or administrator role. The Kelvin team runs the hub with a separate
operator key, which is not an account.

    kelvin-hub --home <folder>           # serve on http://127.0.0.1:8780
    kelvin-hub backup --home <folder>    # copy the database to backups/ now

See docs/hub.md."""
