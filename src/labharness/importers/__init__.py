"""Readers for other programs' files, which imports.py turns into runs: makers' formats (BioLogic's EC-Lab files,
Gamry's .dta, Arbin's exports) and the containers data come in (Excel workbooks, NumPy arrays, TIFF images).

Each reader keeps to what its format's documentation (or a long-used open reader of it) says, and refuses a file it
can't read with certainty rather than guess at its layout: a wrong guess would put wrong numbers in a run. None needs
a package the app doesn't bring (NumPy only, where arrays are read)."""
