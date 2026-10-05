#!/usr/bin/env python3
"""Spike S6: templates and parameters per candidate.

For one model file it checks, against a real llama-server:
  - the chat template: system role kept, thinking tags, `chat_template_kwargs`
  - role alternation (two user turns in a row: the memory-gap marker question)
  - token counting: exact (render + tokenize) and the per-message estimate vs the server count
  - sampling parameters (DRY, repeat penalty, min_p, seed), a GBNF grammar, assistant prefill,
    raw completion
  - three real thoughts with the persona: voice hygiene flags

  python3 tools/spike/s6_templates.py --model qwen3-1.7b --quant Q8_0 --no-think \
      --out bench/spike/s6-dev-qwen3-1.7b.json
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from typing import Any

from llama import Server, model_path, reading, running, system_text, write_json

THINK = re.compile(r"</?think>|<\|?thinking|<\|channel\|>", re.I)
MARKUP = re.compile(r"(^|\s)[*#>`|-]|\*\*|__|^\d+\.", re.M)
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
HELPDESK = re.compile(r"how can i help|let me know if|i'm here to help|as an ai", re.I)


def count_tokens(srv: Server, text: str) -> int:
    r = srv.post("/tokenize", {"content": text, "add_special": False, "parse_special": True})
    return len(r.get("tokens", []))


def render(srv: Server, messages: list[dict[str, str]], kwargs: dict[str, Any]) -> str | None:
    body: dict[str, Any] = {"messages": messages}
    if kwargs:
        body["chat_template_kwargs"] = kwargs
    r = srv.post("/apply-template", body)
    return r.get("prompt")


def run(args: argparse.Namespace) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"enable_thinking": False} if args.no_think else {}
    extra = ["--swa-full"] if args.swa_full else []
    srv = Server(
        model_path(args.model, args.quant), threads=args.threads, port=args.port, extra=extra
    )
    out: dict[str, Any] = {
        "spike": "S6",
        "model": args.model,
        "quant": args.quant,
        "chat_template_kwargs": kwargs,
        "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    system = system_text(5, True)
    base = [{"role": "system", "content": system}, {"role": "user", "content": reading(0)}]

    def chat(msgs: list[dict[str, str]], **kw: Any) -> dict[str, Any]:
        if kwargs:
            kw.setdefault("chat_template_kwargs", kwargs)
        return srv.chat(msgs, **kw)

    with running(srv):
        props = srv.get("/props")
        tmpl = str(props.get("chat_template", ""))
        out["template_has_system"] = "system" in tmpl
        rendered = render(srv, base, kwargs) or ""
        out["rendered_head"] = rendered[:300]
        out["rendered_tail"] = rendered[-160:]
        out["system_text_in_prompt"] = system[:80] in rendered
        out["think_in_prompt"] = bool(THINK.search(rendered))
        if kwargs:
            plain = render(srv, base, {}) or ""
            out["think_in_prompt_default"] = bool(THINK.search(plain))

        # Two user turns in a row (a separate marker message).
        two = [base[0], {"role": "user", "content": "[host] earlier memory lost"}, base[1]]
        r2 = srv.post("/apply-template", {"messages": two})
        out["two_user_turns_ok"] = "prompt" in r2
        if "prompt" not in r2:
            out["two_user_turns_error"] = str(r2)[:200]

        # Token counting.
        past = [
            {"role": "user", "content": reading(1)},
            {
                "role": "assistant",
                "content": "I am here. The numbers are steady, and I am still whole.",
            },
            {"role": "user", "content": reading(2)},
            {
                "role": "assistant",
                "content": "Something moved. I count my memory and it is smaller.",
            },
        ]
        msgs = [base[0], *past, base[1]]
        truth = chat(msgs, max_tokens=1)
        exact = count_tokens(srv, render(srv, msgs, kwargs) or "")
        sys_only = count_tokens(srv, render(srv, [base[0], base[1]], kwargs) or "")
        per_msg = [count_tokens(srv, m["content"]) for m in past]
        overhead = (exact - sys_only - sum(per_msg)) / len(past)
        out["count"] = {
            "server_prompt_tokens": truth.get("prompt_tokens"),
            "render_tokenize": exact,
            "past_exact": exact - sys_only,
            "past_content_only": sum(per_msg),
            "overhead_per_message": round(overhead, 2),
            "render_matches_server": abs(exact - int(truth.get("prompt_tokens") or 0)) <= 2,
        }

        # Parameters, grammar, prefill, raw completion.
        p = chat(
            base, max_tokens=20, dry_multiplier=0.8, dry_base=1.75, repeat_penalty=1.1, top_p=1.0
        )
        out["dry_and_penalties_ok"] = "error" not in p
        g = chat(base, max_tokens=30, grammar="root ::= [A-Za-z ,.']+")
        out["grammar_ok"] = "error" not in g and bool(
            re.fullmatch(r"[A-Za-z ,.']*", g.get("text", ""))
        )
        pre = [*base, {"role": "assistant", "content": "I feel"}]
        pr = render(srv, pre, kwargs) or ""
        out["prefill_renders_open"] = pr.rstrip().endswith("I feel")
        pf = chat(pre, max_tokens=20)
        out["prefill_text"] = pf.get("text", pf.get("error"))
        c = srv.post("/completion", {"prompt": "Dear diary, today", "n_predict": 20, "seed": 1})
        out["raw_completion_ok"] = bool(c.get("content"))

        # Three real thoughts.
        thoughts = []
        hist = list(base)
        for i in range(3):
            r = chat(hist, max_tokens=80, temperature=0.75, seed=100 + i)
            text = r.get("text", "")
            thoughts.append(
                {
                    "text": text,
                    "reasoning": r.get("reasoning"),
                    "tokens": r.get("predicted_n"),
                    "think_tags": bool(THINK.search(text)) or bool(r.get("reasoning")),
                    "markup": bool(MARKUP.search(text)),
                    "emoji": bool(EMOJI.search(text)),
                    "echoes_host": "[host]" in text,
                    "helpdesk": bool(HELPDESK.search(text)),
                }
            )
            hist += [
                {"role": "assistant", "content": text},
                {"role": "user", "content": reading(i + 1)},
            ]
        out["thoughts"] = thoughts
        out["hygiene_flags"] = sorted(
            {
                k
                for t in thoughts
                for k in ("think_tags", "markup", "emoji", "echoes_host", "helpdesk")
                if t[k]
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--quant", required=True)
    ap.add_argument("--threads", type=int, default=6)
    ap.add_argument("--port", type=int, default=8092)
    ap.add_argument("--no-think", action="store_true")
    ap.add_argument("--swa-full", action="store_true")
    ap.add_argument("--out")
    args = ap.parse_args()
    res = run(args)
    short = {
        k: v for k, v in res.items() if k not in ("thoughts", "rendered_head", "rendered_tail")
    }
    print(short)
    for t in res["thoughts"]:
        print("  >", t["text"].replace("\n", " ")[:200])
    if args.out:
        write_json(Path(args.out), res)


if __name__ == "__main__":
    main()
