"""Bounded notices; full diagnostics belong in the scrollable operation log."""
import re


def compact_notice(value, error=False, limit=100):
    clean = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(value))
    lines = [' '.join(line.split()) for line in clean.splitlines() if line.strip()]
    if not lines:
        return ''
    summary = lines[0]
    if error and len(lines) > 1:
        # Docker progress precedes the cause; prefer the final substantive error.
        for line in reversed(lines):
            if re.search(r'error response|failed to solve|no such image|pull access denied|error:|失败|超时', line, re.I):
                summary = line
                break
    shortened = len(summary) > limit
    if shortened:
        summary = summary[:limit - 1].rstrip() + '…'
    if shortened or len(lines) > 1:
        summary += '\n完整信息见操作记录。'
    return summary
