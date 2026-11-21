"""Summarize completed operations, including events from shell subprocesses."""
import json
import os
import sys


def human(size):
    for suffix in ("K", "M", "G", "T"):
        size //= 1024
        if size < 1024 or suffix == "T":
            return f"{size}{suffix}"


def main():
    filename, json_mode, check_mode, code = sys.argv[1:]
    with open(filename, "rb") as events:
        fields = events.read().split(b"\0")
    if fields.pop() or len(fields) % 4:
        raise ValueError("incomplete cleanup event")
    items = []
    for index in range(0, len(fields), 4):
        status, path, reason, size = fields[index:index + 4]
        items.append(dict(status=status.decode(), path=os.fsdecode(path),
                          reason=os.fsdecode(reason), bytes=int(size)))
    failed = int(code) or int(any(item["status"] == "failed" for item in items))
    freed = sum(item["bytes"] for item in items if item["status"] == "removed")
    estimate = sum(item["bytes"] for item in items if item["status"] == "would_remove")
    if json_mode == "true":
        print(json.dumps(dict(check=check_mode == "true", exit_code=failed,
                              bytes_freed=freed, bytes_reclaimable=estimate, items=items)))
    elif check_mode == "true":
        print(f"Potential space savings: {human(estimate)}")
    else:
        print(f"Space freed: {human(freed)}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
