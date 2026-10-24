"""Tests with a fake MyAnimeList. Run: python -m pytest (with MangaVibe's requirements installed)."""
import shutil
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from conftest import TMP

PID = "mal-profile-sync"


class FakeMal:
    """Your MAL list ({mal_id: status}) plus a record of what the plugin sent."""

    def __init__(self):
        self.list, self.patches, self.tokens = {}, [], []

    def __call__(self, request: httpx.Request):
        url, method = request.url, request.method
        if url.host == "myanimelist.net" and url.path == "/v1/oauth2/token":
            form = parse_qs(request.content.decode())
            self.tokens.append(form)
            return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh",
                                             "expires_in": 2678400})
        assert request.headers["authorization"].startswith("Bearer ")
        path = url.path.removeprefix("/v2")
        if path == "/users/@me":
            return httpx.Response(200, json={"name": "Ivo"})
        if path == "/users/@me/mangalist":
            return httpx.Response(200, json={"data": [
                {"node": {"id": i}, "list_status": {"status": s}} for i, s in self.list.items()],
                "paging": {}})
        mal_id = int(path.split("/")[2])
        if method == "PATCH":
            status = parse_qs(request.content.decode())["status"][0]
            self.patches.append((mal_id, status))
            self.list[mal_id] = status
            return httpx.Response(200, json={"status": status})
        status = self.list.get(mal_id)
        return httpx.Response(200, json={"id": mal_id, **({"my_list_status": {"status": status}} if status else {})})


def series(client, title):
    return next(s for s in client.get("/api/series").json()["items"] if s["title"] == title)


def set_series(title_id, **values):
    from app.db import SessionLocal
    from app.models import Series
    with SessionLocal() as db:
        s = db.get(Series, title_id)
        for k, v in values.items():
            setattr(s, k, v)
        db.commit()


def info(client):
    return next(p for p in client.get("/api/plugins").json()["plugins"] if p["id"] == PID)


def idle():
    from app.plugins import manager
    manager.wait_idle()


@pytest.fixture()
def mal(client):
    from app.metadata import http
    from app.plugins import Context, manager
    dest = TMP / "data" / "plugins" / "mal_profile_sync.py"
    shutil.copy(Path(__file__).parent / "mal_profile_sync.py", dest)
    client.post("/api/plugins/reload")
    fake = FakeMal()
    old, http.transport = http.transport, httpx.MockTransport(fake)
    from app.db import SessionLocal
    with SessionLocal() as db:  # already connected
        Context(manager.get(PID), db).save_settings({"client_id": "cid", "access_token": "a",
                                                    "refresh_token": "r", "expires_at": time.time() + 9e6,
                                                    "user": "Ivo"})
        db.commit()
    b, h = series(client, "Series B")["id"], series(client, "Hana to Akuma")["id"]
    set_series(b, mal_id=111, publication_status="FINISHED", reading_status=None)
    set_series(h, mal_id=222, publication_status="RELEASING", reading_status=None)
    for sid in (b, h):
        client.post(f"/api/series/{sid}/mark-unread")
    idle()
    fake.patches.clear()
    fake.ids = {"b": b, "h": h}
    yield fake
    idle()
    http.transport = old
    dest.unlink()
    client.post("/api/plugins/reload")


def read_all(client, sid):
    client.post(f"/api/series/{sid}/mark-read")
    idle()


def test_start_and_finish_ended_series(client, mal):
    sid = mal.ids["b"]
    book = client.get(f"/api/series/{sid}/books").json()[0]
    client.put(f"/api/books/{book['id']}/progress", json={"page": 1})
    idle()
    assert mal.patches == [(111, "reading")]
    client.put(f"/api/books/{book['id']}/progress", json={"page": book["page_count"] - 1})
    idle()
    assert mal.patches == [(111, "reading"), (111, "completed")]
    s = client.get(f"/api/series/{sid}").json()
    assert {"label": "MyAnimeList", "value": "Completed"}.items() <= s["plugin_fields"][0].items()


def test_caught_up_on_releasing_series_stays_reading(client, mal):
    read_all(client, mal.ids["h"])
    assert mal.patches == [(222, "reading")]


def test_completed_by_hand_is_sent(client, mal):
    client.patch(f"/api/series/{mal.ids['h']}", json={"reading_status": "completed"})
    idle()
    assert mal.patches == [(222, "completed")]


def test_never_downgrades_completed(client, mal):
    mal.list[111] = "completed"
    book = client.get(f"/api/series/{mal.ids['b']}/books").json()[0]
    client.put(f"/api/books/{book['id']}/progress", json={"page": 1})  # a re-read
    idle()
    assert mal.patches == []


def test_other_statuses_not_sent(client, mal):
    client.patch(f"/api/series/{mal.ids['b']}", json={"reading_status": "on_hold"})
    client.patch(f"/api/series/{mal.ids['b']}", json={"reading_status": "dropped"})
    idle()
    assert mal.patches == []


def test_sync_all(client, mal):
    mal.list = {111: "completed", 222: "reading"}
    r = client.post(f"/api/plugins/{PID}/actions/sync").json()
    assert "started" in r["message"]
    idle()
    assert series(client, "Series B")["reading_status"] == "completed"
    assert series(client, "Hana to Akuma")["reading_status"] == "reading"
    assert "2 updated here, 0 sent" in info(client)["status"]
    assert mal.patches == []

    # finishing a series that came in as Reading still completes it (once the story has ended)
    set_series(mal.ids["h"], publication_status="FINISHED")
    read_all(client, mal.ids["h"])
    assert series(client, "Hana to Akuma")["reading_status"] == "completed"
    assert mal.patches == [(222, "completed")]


def test_sync_all_sends_what_mal_is_missing(client, mal):
    read_all(client, mal.ids["b"])  # pushed by the event already...
    mal.list.clear()                # ...but say MAL lost it
    mal.patches.clear()
    client.post(f"/api/plugins/{PID}/actions/sync")
    idle()
    assert mal.patches == [(111, "completed")]
    assert "0 updated here, 1 sent" in info(client)["status"]


def test_series_button(client, mal):
    mal.list[111] = "completed"
    r = client.post(f"/api/series/{mal.ids['b']}/actions/{PID}/sync").json()
    assert r["message"] == "Marked Completed (from MyAnimeList)"
    set_series(mal.ids["h"], mal_id=None)
    r = client.post(f"/api/series/{mal.ids['h']}/actions/{PID}/sync").json()
    assert "no MyAnimeList id" in r["message"]


def test_connect_flow(client, mal):
    client.put(f"/api/plugins/{PID}/settings", json={"client_id": "my-client"})
    client.post(f"/api/plugins/{PID}/actions/disconnect")
    assert info(client)["status"].startswith("Not connected")
    assert "http://testserver/api/plugins/mal-profile-sync/web/callback" in info(client)["status"]
    assert "not connected" in client.post(f"/api/plugins/{PID}/actions/sync").json()["message"]

    r = client.post(f"/api/plugins/{PID}/actions/connect").json()
    q = parse_qs(urlparse(r["open_url"]).query)
    assert q["client_id"] == ["my-client"] and q["code_challenge_method"] == ["plain"]
    assert q["redirect_uri"] == ["http://testserver/api/plugins/mal-profile-sync/web/callback"]

    bad = client.get(f"/api/plugins/{PID}/web/callback", params={"code": "x", "state": "wrong"})
    assert "old or unknown" in bad.text
    page = client.get(f"/api/plugins/{PID}/web/callback", params={"code": "the-code", "state": q["state"][0]})
    assert "Connected to MyAnimeList as Ivo" in page.text
    sent = mal.tokens[-1]
    assert sent["code"] == ["the-code"] and sent["code_verifier"] == q["code_challenge"]
    assert info(client)["status"].startswith("Connected as Ivo")


def test_expired_token_is_renewed(client, mal):
    from app.db import SessionLocal
    from app.plugins import Context, manager
    with SessionLocal() as db:
        ctx = Context(manager.get(PID), db)
        ctx.save_settings({**ctx.settings, "expires_at": time.time() - 1})
        db.commit()
    client.patch(f"/api/series/{mal.ids['b']}", json={"reading_status": "reading"})
    idle()
    assert mal.tokens[-1]["grant_type"] == ["refresh_token"]
    assert mal.patches == [(111, "reading")]
