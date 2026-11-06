---
name: set-up-an-instrument
description: Adding an instrument Kelvin does not know yet: find it, identify it, read its manual, draft and try its card, then add it.
---
1. Find it: connected_devices (USB, serial), scan_instruments (VISA, the local network), find_on_network for one on the network whose address nobody knows, then inspect_address or inspect_serial on what showed up. installed_software and find_maker_files show the maker's program and libraries if the person installed them.
2. Identify maker, model and firmware (*IDN?, or the USB vendor and product IDs). Ask the person when two candidates fit.
3. The manual: save_manual (from the maker's site, or the PDF the person has), then search_manual for the command set, terminators, baud rate, ranges and limits. Never guess a command that drives the sample.
4. If it needs the maker's library: install_for_instrument (a person approves), then library_api to read its functions.
5. Draft the card: draft_card with the address, maker, model and manual; check_card; try_card (it reads, then writes only the safe values).
6. Limits: ask the person for the sample's own limits (current, voltage, power, temperature, field) and set the card's limits at or below them and below the instrument's maximum. Never raise a limit to make something work.
7. add_instrument, then health_check and describe_instrument to confirm units, ranges and safe values.
8. Kelvin has a card but it doesn't fit how the lab reaches the instrument (it won't connect, or the lab's scripts use another interface, library or firmware, such as a newer controller on the network where the card has the older one's DLL): read how the lab does it (survey_scripts, read_examples), write this lab's version of the card under the same id and parameter names, try_card it on the instrument's address, save_card with version: true, then change_instrument to it. The person never edits a card by hand.
