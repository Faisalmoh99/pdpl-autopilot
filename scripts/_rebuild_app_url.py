"""One-off: rebuild APP_DATABASE_URL in .env from PDPL_APP_PASSWORD.

Reads the password via python-dotenv (NOT a hand-rolled parser), URL-encodes
it with urllib.parse.quote_plus (so any special char is pooler-safe), and
rewrites only the APP_DATABASE_URL line. Also single-quotes PDPL_APP_PASSWORD
so a future non-alphanumeric value cannot be mangled by the .env reader.

The password is never printed; output is the masked URL only.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import dotenv_values

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

HOST = "aws-1-eu-central-1.pooler.supabase.com"
USER = "pdpl_app.<project>"  # set to your Supabase project-ref
PORT = "5432"
DBNAME = "postgres"

values = dotenv_values(ENV_PATH)
password = values.get("PDPL_APP_PASSWORD")
if not password:
    raise SystemExit("PDPL_APP_PASSWORD missing/empty in .env")

encoded = quote_plus(password)
app_url = (
    f"postgresql+asyncpg://{USER}:{encoded}@{HOST}:{PORT}/{DBNAME}"
)

lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
out = []
saw_url = False
saw_pw = False
for line in lines:
    if re.match(r"\s*APP_DATABASE_URL\s*=", line):
        out.append(f"APP_DATABASE_URL={app_url}\n")
        saw_url = True
    elif re.match(r"\s*PDPL_APP_PASSWORD\s*=", line):
        out.append(f"PDPL_APP_PASSWORD='{password}'\n")
        saw_pw = True
    else:
        out.append(line)

if not saw_url:
    raise SystemExit("APP_DATABASE_URL line not found in .env")
if not saw_pw:
    raise SystemExit("PDPL_APP_PASSWORD line not found in .env")

ENV_PATH.write_text("".join(out), encoding="utf-8")

masked = re.sub(r"://[^@]*@", "://REDACTED@", app_url)
print(f"rewrote APP_DATABASE_URL -> {masked}")
print(f"password_url_encoding_noop={encoded == password}")
