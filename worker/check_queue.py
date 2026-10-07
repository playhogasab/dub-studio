#!/usr/bin/env python3
"""Fast queue check (koi bhaari dependency nahi — sirf stdlib).
ntfy jobs topic + jobs/manual.json dekhta hai.
Output: GITHUB_OUTPUT me has_work=true/false.
"""
import json
import os
import urllib.request

NTFY_BASE = "https://ntfy.sh"
NTFY_TOPIC = "dsq_4f8a1c9e2b7d"


def ntfy_get(topic, since="24h"):
    """Return list of payload dicts, ya None (network fail)."""
    try:
        req = urllib.request.Request(
            f"{NTFY_BASE}/{topic}/json?since={since}",
            headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out = []
            for line in r.read().decode().splitlines():
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if isinstance(m, dict) and "message" in m:
                    try:
                        out.append(json.loads(m["message"]))
                    except Exception:
                        pass
            return out
    except Exception as e:
        print(f"ntfy read failed: {e}")
        return None


def main():
    has_work = False

    msgs = ntfy_get(NTFY_TOPIC + "_jobs")
    if msgs:
        seen = {}
        for p in msgs:
            if isinstance(p, dict) and p.get("id"):
                seen[p["id"]] = p
        for jid in seen:
            res = ntfy_get(f"{NTFY_TOPIC}_r_{jid}", since="24h")
            if res is None:
                continue
            last = res[-1] if res else {}
            st = last.get("status") if isinstance(last, dict) else None
            if st in ("done", "error"):
                continue
            has_work = True
            print(f"queued/processing job: {jid} (last={st})")
            break

    # jobs/*.json (GitHub-direct frontend jobs)
    import glob
    for mp in sorted(glob.glob("jobs/*.json")):
        try:
            d = json.load(open(mp, encoding="utf-8"))
            if d and not d.get("processed"):
                has_work = True
                print(f"{mp} unprocessed")
                break
        except Exception as e:
            print(f"{mp} read fail: {e}")

    line = f"has_work={'true' if has_work else 'false'}"
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(line + "\n")
    print(line)


if __name__ == "__main__":
    main()
