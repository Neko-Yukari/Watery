import argparse
import json
import sys
import time
from typing import Dict, List, Tuple

import httpx


def _ok(label: str, detail: str = "") -> None:
    print(f"[PASS] {label}" + (f" | {detail}" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    print(f"[FAIL] {label}" + (f" | {detail}" if detail else ""))


def _read_sse_events(resp: httpx.Response, max_events: int = 80, timeout_s: float = 35.0) -> List[Dict]:
    events: List[Dict] = []
    buffer = ""
    start = time.time()

    for chunk in resp.iter_text():
        if not chunk:
            if time.time() - start > timeout_s:
                break
            continue

        buffer += chunk

        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            data_lines = [ln[5:].strip() for ln in frame.split("\n") if ln.startswith("data:")]
            if not data_lines:
                continue
            payload = "\n".join(data_lines).strip()
            if not payload:
                continue
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                continue

            if len(events) >= max_events:
                return events

        if time.time() - start > timeout_s:
            break

    return events


def _check_health(client: httpx.Client, base: str) -> bool:
    try:
        r = client.get(f"{base}/health", timeout=8.0)
        if r.status_code == 200:
            _ok("health", f"status={r.status_code}")
            return True
        _fail("health", f"status={r.status_code}")
        return False
    except Exception as e:
        _fail("health", str(e))
        return False


def _create_conversation(client: httpx.Client, api_base: str) -> Tuple[bool, str]:
    try:
        r = client.post(
            f"{api_base}/conversations",
            json={"title": "phase14-smoke", "model": "ark-code-latest"},
            timeout=10.0,
        )
        if r.status_code != 201:
            _fail("create_conversation", f"status={r.status_code}")
            return False, ""
        conv_id = r.json().get("id", "")
        if not conv_id:
            _fail("create_conversation", "missing id")
            return False, ""
        _ok("create_conversation", conv_id)
        return True, conv_id
    except Exception as e:
        _fail("create_conversation", str(e))
        return False, ""


def _stream_chat(client: httpx.Client, api_base: str, conv_id: str) -> bool:
    payload = {
        "conversation_id": conv_id,
        "messages": [{"role": "user", "content": "请简单回复：Phase14 smoke test ok"}],
        "model": "ark-code-latest",
    }

    try:
        with client.stream("POST", f"{api_base}/chat/stream", json=payload, timeout=45.0) as resp:
            if resp.status_code != 200:
                _fail("chat_stream_status", f"status={resp.status_code}")
                return False

            events = _read_sse_events(resp)
            if not events:
                _fail("chat_stream_events", "no events")
                return False

            event_names = [e.get("event") for e in events]
            has_text = "text_delta" in event_names
            has_done = "done" in event_names
            has_error = "error" in event_names
            has_usage = "usage" in event_names

            if has_error:
                first_error = next((e for e in events if e.get("event") == "error"), {})
                _fail("chat_stream_error_event", first_error.get("message") or first_error.get("error") or "unknown")
                return False

            if not has_done:
                _fail("chat_stream_done", f"events={event_names[:12]}")
                return False

            if not has_text:
                _fail("chat_stream_text_delta", "missing text_delta")
                return False

            _ok("chat_stream_events", f"count={len(events)} usage_event={has_usage}")
            return True
    except Exception as e:
        _fail("chat_stream_exception", str(e))
        return False


def _rollback(client: httpx.Client, api_base: str, conv_id: str) -> bool:
    try:
        r = client.delete(f"{api_base}/conversations/{conv_id}/rollback", timeout=12.0)
        if r.status_code != 200:
            _fail("rollback_status", f"status={r.status_code}")
            return False
        data = r.json()
        rolled_back = int(data.get("rolled_back", 0) or 0)
        if rolled_back <= 0:
            _fail("rollback_count", f"rolled_back={rolled_back}")
            return False
        _ok("rollback", f"rolled_back={rolled_back}")
        return True
    except Exception as e:
        _fail("rollback_exception", str(e))
        return False


def _check_archive(client: httpx.Client, api_base: str) -> bool:
    try:
        r = client.get(f"{api_base}/rollback-archive", timeout=10.0)
        if r.status_code != 200:
            _fail("archive_status", f"status={r.status_code}")
            return False
        data = r.json()
        items = data if isinstance(data, list) else data.get("items", [])
        if not isinstance(items, list):
            _fail("archive_items", "unexpected payload")
            return False
        _ok("archive_list", f"items={len(items)}")
        return True
    except Exception as e:
        _fail("archive_exception", str(e))
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase14 chat regression smoke checks")
    parser.add_argument("--base-url", default="http://127.0.0.1:18000", help="Server base URL")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    api_base = f"{base}/api/v1"

    passed = 0
    total = 0

    with httpx.Client() as client:
        total += 1
        if _check_health(client, base):
            passed += 1
        else:
            print("\n服务不可用：请先启动服务后重试（例如 docker-compose up --build）。")
            return 1

        total += 1
        ok_create, conv_id = _create_conversation(client, api_base)
        if ok_create:
            passed += 1

        if not conv_id:
            print(f"\nResult: {passed}/{total} checks passed")
            return 1

        total += 1
        if _stream_chat(client, api_base, conv_id):
            passed += 1

        total += 1
        if _rollback(client, api_base, conv_id):
            passed += 1

        total += 1
        if _check_archive(client, api_base):
            passed += 1

    print(f"\nResult: {passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
