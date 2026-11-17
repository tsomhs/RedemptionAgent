# make install   μία φορά: φτιάχνει το περιβάλλον Python και εγκαθιστά τις βιβλιοθήκες
# make run       ξεκινά τον agent (χρονοπρογραμματιστής και dashboard, http://127.0.0.1:8765)
# make test      τρέχει τους αυτόματους ελέγχους

PYTHON = .venv/bin/python

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install -r requirements.txt
	test -f .env || cp .env.example .env

run:
	$(PYTHON) main.py

test:
	$(PYTHON) tests/run_all.py
