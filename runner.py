# -*- coding: utf-8 -*-
"""
GitHub Actions에서 5분마다 실행되는 스레드 발행기.
- queue.enc : 예약 프로그램(PC)이 올린 계정·예약 목록 (암호화)
- state.enc : 이 스크립트가 기록하는 발행 결과·갱신된 토큰 (암호화)
암호화 키는 저장소 Secrets 의 THREADS_KEY 에만 있습니다.
"""
import json
import os
import subprocess
import time

import requests
from cryptography.fernet import Fernet

API = "https://graph.threads.net/v1.0"
ROOT = "https://graph.threads.net"
MISS_SEC = 3 * 3600          # 예정보다 3시간 넘게 지난 예약은 '놓침' (몰아서 올라가는 것 방지)
NEW_WORK_SEC = 20 * 60       # 실행 시작 20분 이후엔 새 본문 발행을 시작하지 않음
COMMENT_WAIT_SEC = 22 * 60   # 실행 시작 22분 안에 올릴 수 있는 댓글만 기다림(나머지는 다음 실행)

F = Fernet(os.environ["THREADS_KEY"].encode())
START = time.time()


def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "rb") as f:
        return json.loads(F.decrypt(f.read()))


def save_state(state, msg):
    with open("state.enc", "wb") as f:
        f.write(F.encrypt(json.dumps(state, ensure_ascii=False).encode()))
    subprocess.run(["git", "add", "state.enc"], check=True)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    subprocess.run(["git", "commit", "-q", "-m", msg], check=True)
    for _ in range(6):
        if subprocess.run(["git", "push", "-q"]).returncode == 0:
            return
        subprocess.run(["git", "pull", "-q", "--rebase"])
        time.sleep(3)
    print("!! push 실패")


def check(r):
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    if r.status_code >= 400 or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise RuntimeError(err.get("error_user_msg") or err.get("message") or str(data)[:300])
    return data


def publish(user_id, token, text, reply_to=None):
    data = {"media_type": "TEXT", "text": text, "access_token": token}
    if reply_to:
        data["reply_to_id"] = reply_to
    cid = check(requests.post(f"{API}/{user_id}/threads", data=data, timeout=30))["id"]
    time.sleep(3)
    last = None
    for _ in range(3):
        try:
            return check(requests.post(f"{API}/{user_id}/threads_publish",
                                       data={"creation_id": cid, "access_token": token}, timeout=30))["id"]
        except Exception as e:  # noqa
            last = e
            time.sleep(10)
    raise RuntimeError(f"발행 단계 실패: {last}")


def main():
    queue = load("queue.enc", {"accounts": {}, "posts": []})
    state = load("state.enc", {"posts": {}, "tokens": {}})
    state.setdefault("posts", {})
    state.setdefault("tokens", {})
    now = time.time()
    accounts = queue.get("accounts", {})

    def cred(aid):
        a = accounts[aid]
        t = state["tokens"].get(aid)
        if t and t.get("expires", 0) > a.get("expires", 0):
            return a["user_id"], t["token"], t["expires"]
        return a["user_id"], a["token"], a.get("expires", 0)

    # 1) 토큰 자동 갱신 (만료 10일 이내, 하루 1번)
    for aid in accounts:
        _, tok, exp = cred(aid)
        last = state["tokens"].get(aid, {}).get("refreshed", 0)
        if exp and now < exp < now + 10 * 86400 and now - last > 86400:
            try:
                r = check(requests.get(f"{ROOT}/refresh_access_token",
                                       params={"grant_type": "th_refresh_token", "access_token": tok}, timeout=20))
                state["tokens"][aid] = {"token": r["access_token"], "refreshed": now,
                                        "expires": now + int(r.get("expires_in", 5184000))}
                print(f"토큰 갱신: {accounts[aid].get('name')}")
                save_state(state, "refresh token")
            except Exception as e:  # noqa
                print(f"토큰 갱신 실패 {accounts[aid].get('name')}: {e}")

    # 2) 본문 발행
    posts = sorted(queue.get("posts", []), key=lambda p: p["at"])
    for p in posts:
        if p.get("canceled") or p["account"] not in accounts:
            continue
        s = state["posts"].get(p["id"], {})
        if s.get("attempt", 0) < p.get("attempt", 0):          # 앱에서 재시도/수정함
            s = {"attempt": p.get("attempt", 0), "post_id": s.get("post_id"), "posted_at": s.get("posted_at")}
            if s["post_id"]:
                s["status"] = "comment_wait" if p.get("comment") else "done"
            state["posts"][p["id"]] = s
        if s.get("status") in ("done", "failed", "missed", "comment_failed", "comment_wait"):
            continue
        if p["at"] > time.time():
            continue
        if time.time() - p["at"] > MISS_SEC:
            s.update(status="missed", error="예정 시각보다 3시간 넘게 지나 발행하지 않음", updated=time.time())
            state["posts"][p["id"]] = s
            save_state(state, "missed")
            continue
        if time.time() - START > NEW_WORK_SEC:
            break
        uid, tok, _ = cred(p["account"])
        try:
            pid = publish(uid, tok, p["body"])
            s.update(post_id=pid, posted_at=time.time(), error=None, updated=time.time(),
                     status="comment_wait" if (p.get("comment") or "").strip() else "done")
            print(f"본문 발행: {accounts[p['account']].get('name')} {pid}")
        except Exception as e:  # noqa
            s.update(status="failed", error=str(e)[:300], updated=time.time())
            print(f"본문 실패: {e}")
        s["attempt"] = p.get("attempt", 0)
        state["posts"][p["id"]] = s
        save_state(state, "publish")

    # 3) 첫 댓글 (본문 발행 + 지연시간 이후)
    by_id = {p["id"]: p for p in posts}
    while True:
        waiting = []
        for pid, s in state["posts"].items():
            p = by_id.get(pid)
            if p and s.get("status") == "comment_wait" and p["account"] in accounts:
                waiting.append((s.get("posted_at", 0) + p.get("delay", 180), p, s))
        if not waiting:
            break
        waiting.sort(key=lambda x: x[0])
        due, p, s = waiting[0]
        if due - START > COMMENT_WAIT_SEC:
            break                                              # 다음 실행에서 처리
        if due > time.time():
            time.sleep(due - time.time())
        uid, tok, _ = cred(p["account"])
        try:
            cid = publish(uid, tok, p["comment"], s["post_id"])
            s.update(status="done", comment_id=cid, error=None, updated=time.time())
            print(f"댓글 발행: {accounts[p['account']].get('name')} {cid}")
        except Exception as e:  # noqa
            s.update(status="comment_failed", error="본문은 발행됨 · 댓글 실패: " + str(e)[:250], updated=time.time())
        save_state(state, "comment")

    # 4) 오래된 기록 정리
    keep = set(by_id)
    old = [k for k, v in state["posts"].items() if k not in keep and time.time() - v.get("updated", 0) > 7 * 86400]
    if old:
        for k in old:
            state["posts"].pop(k, None)
        save_state(state, "cleanup")


if __name__ == "__main__":
    main()
