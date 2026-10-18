"""Print the links from the newest emails in sent_emails/ (for local testing).

Run from the tt_mcp folder:
    python -m app.api.show_links        # newest email
    python -m app.api.show_links 3      # newest 3 emails

Copy a link into the browser to act as the faculty member or the student.
"""
import re
import sys
from email import policy
from email.parser import BytesParser
from pathlib import Path

from .config import Settings


def main(count: int = 1) -> None:
    maildir = Path(Settings.from_env().maildir)
    files = sorted(maildir.glob("*.eml"))
    if not files:
        print(f"No emails in {maildir.resolve()} yet.")
        return
    for path in files[-count:][::-1]:
        msg = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        text = msg.get_body(("plain",)).get_content()
        links = re.findall(r"^\s*([A-Za-z][A-Za-z ]+?):?\s+(https?://\S+)", text, re.M)
        print(f"\n{msg['Subject']}\n  To: {msg['To']}")
        for label, url in links:
            print(f"  {label.strip():<22} {url}")
        if not links:
            print("  (no links in this email)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 1)
