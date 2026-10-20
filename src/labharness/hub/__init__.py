"""The Kelvin hub: online accounts for Kelvin users across labs, and a home for the work they share.

Kelvin itself keeps accounts per computer (accounts.py). The hub is one server that many Kelvins sign in to:
people register once (with their consent to the terms and privacy notice), join their labs, and connect each
Kelvin they use through the browser. A connected Kelvin then sends the hub what its person shares:

- **Shared by default:** normal projects and unfiled chats, with their runs and whole conversations.
- **Never sent:** projects marked unpublished or confidential (or limited to some models), incognito chats,
  API keys, and ChatGPT or Claude Code sign-ins. The hub refuses them even if a Kelvin sends them.
- **Who sees it:** its owner, and the other members of the lab it was shared in, while both are members.

Everyone is equal: there is no lab owner or administrator role. The Kelvin team runs the hub with a separate
operator key, which is not an account.

    kelvin-hub --home <folder>           # serve on http://127.0.0.1:8780
    kelvin-hub backup --home <folder>    # copy the database to backups/ now

See docs/hub.md."""
