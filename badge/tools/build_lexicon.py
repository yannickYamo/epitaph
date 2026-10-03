"""The words the badge model may write: every word of its training text that the system's
English dictionary also knows (the training text has a few run-together words, like "actof").

    python badge/tools/build_lexicon.py badge/data/lives.jsonl badge/data/pi_thoughts.txt \
        badge/tufty/epitaph/assets/lexicon.json
"""

import json
import re
import sys
from pathlib import Path

from finetune import corpus

DICTIONARIES = ["/usr/share/dict/american-english", "/usr/share/dict/british-english"]


def main() -> None:
    lives, thoughts, out = map(Path, sys.argv[1:4])
    known: set[str] = set()
    for d in DICTIONARIES:
        if Path(d).exists():
            known |= {w.strip().lower() for w in Path(d).read_text(errors="ignore").splitlines()}
    if not known:
        raise SystemExit("no dictionary in /usr/share/dict (apt install wamerican)")
    seen = {w.lower() for d in corpus(lives, [thoughts]) for w in re.findall(r"[A-Za-z']+", d)}
    words = sorted(
        w for w in seen if not w.startswith("'") and (w in known or w.split("'")[0] in known)
    )
    out.write_text(json.dumps(words))
    print(f"{len(words)} words of {len(seen)} -> {out}")


if __name__ == "__main__":
    main()
