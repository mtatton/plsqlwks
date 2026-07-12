from __future__ import annotations

Q_QUOTE_DELIMITERS = {
    "[": "]",
    "{": "}",
    "(": ")",
    "<": ">",
}


def oracle_q_quote_start(text: str, start: int) -> tuple[int, str] | None:
    """Return the content start and closing delimiter for Q/NQ literals."""
    delimiter_idx = -1
    if text[start : start + 2].lower() == "q'":
        delimiter_idx = start + 2
    elif text[start : start + 3].lower() == "nq'":
        delimiter_idx = start + 3
    if delimiter_idx < 0 or delimiter_idx >= len(text):
        return None
    delimiter = text[delimiter_idx]
    if delimiter.isspace():
        return None
    return delimiter_idx + 1, Q_QUOTE_DELIMITERS.get(delimiter, delimiter)


def sql_code_mask(text: str) -> str:
    mask = [" "] * len(text)
    idx = 0
    in_single = False
    in_q_quote: str | None = None
    in_line_comment = False
    in_block_comment = False
    while idx < len(text):
        ch = text[idx]
        nxt = text[idx + 1] if idx + 1 < len(text) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            idx += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                idx += 2
                continue
            idx += 1
            continue
        if in_q_quote is not None:
            if ch == in_q_quote and nxt == "'":
                in_q_quote = None
                idx += 2
                continue
            idx += 1
            continue
        if in_single:
            if ch == "'" and nxt == "'":
                idx += 2
                continue
            if ch == "'":
                in_single = False
            idx += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            idx += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            idx += 2
            continue
        q_quote = oracle_q_quote_start(text, idx)
        if q_quote is not None:
            idx, in_q_quote = q_quote
            continue
        if ch == "'":
            in_single = True
            idx += 1
            continue
        mask[idx] = ch
        idx += 1
    return "".join(mask)


def find_top_level_sql_keyword(text: str, keyword: str, start: int = 0) -> int | None:
    mask = sql_code_mask(text)
    keyword_len = len(keyword)
    depth = 0
    idx = start
    while idx < len(mask):
        ch = mask[idx]
        if ch == "(":
            depth += 1
            idx += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            idx += 1
            continue
        if depth == 0 and mask[idx : idx + keyword_len].lower() == keyword.lower():
            before = mask[idx - 1] if idx > 0 else ""
            after = mask[idx + keyword_len] if idx + keyword_len < len(mask) else ""
            if not is_oracle_identifier_char(before) and not is_oracle_identifier_char(after):
                return idx
        idx += 1
    return None


def is_oracle_identifier_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_$#"


def strip_sql_comments(text: str) -> str:
    chars: list[str] = []
    idx = 0
    in_single = False
    in_q_quote: str | None = None
    in_line_comment = False
    in_block_comment = False
    while idx < len(text):
        ch = text[idx]
        nxt = text[idx + 1] if idx + 1 < len(text) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                chars.append(ch)
            idx += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                idx += 2
                continue
            idx += 1
            continue
        if in_q_quote is not None:
            chars.append(ch)
            if ch == in_q_quote and nxt == "'":
                chars.append(nxt)
                in_q_quote = None
                idx += 2
                continue
            idx += 1
            continue
        if in_single:
            chars.append(ch)
            if ch == "'" and nxt == "'":
                chars.append(nxt)
                idx += 2
                continue
            if ch == "'":
                in_single = False
            idx += 1
            continue
        if ch == "-" and nxt == "-":
            chars.append(" ")
            in_line_comment = True
            idx += 2
            continue
        if ch == "/" and nxt == "*":
            chars.append(" ")
            in_block_comment = True
            idx += 2
            continue
        q_quote = oracle_q_quote_start(text, idx)
        if q_quote is not None:
            content_start, in_q_quote = q_quote
            chars.extend(text[idx:content_start])
            idx = content_start
            continue
        if ch == "'":
            in_single = True
        chars.append(ch)
        idx += 1
    return "".join(chars)


def tail_sql_words(tail: str) -> list[str]:
    words: list[str] = []
    idx = 0
    in_single = False
    in_double = False
    in_q_quote: str | None = None
    in_line_comment = False
    in_block_comment = False
    while idx < len(tail):
        ch = tail[idx]
        nxt = tail[idx + 1] if idx + 1 < len(tail) else ""
        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            idx += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                idx += 2
                continue
            idx += 1
            continue
        if in_q_quote is not None:
            if ch == in_q_quote and nxt == "'":
                in_q_quote = None
                idx += 2
                continue
            idx += 1
            continue
        if in_single:
            if ch == "'" and nxt == "'":
                idx += 2
                continue
            if ch == "'":
                in_single = False
            idx += 1
            continue
        if in_double:
            if ch == '"' and nxt == '"':
                idx += 2
                continue
            if ch == '"':
                in_double = False
            idx += 1
            continue
        if ch == "-" and nxt == "-":
            in_line_comment = True
            idx += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            idx += 2
            continue
        q_quote = oracle_q_quote_start(tail, idx)
        if q_quote is not None:
            idx, in_q_quote = q_quote
            continue
        if ch == "'":
            in_single = True
            idx += 1
            continue
        if ch == '"':
            in_double = True
            idx += 1
            continue
        if ch.isalpha() or ch in "_$#":
            word_start = idx
            idx += 1
            while idx < len(tail) and (tail[idx].isalnum() or tail[idx] in "_$#"):
                idx += 1
            words.append(tail[word_start:idx].upper())
            continue
        idx += 1
    return words
