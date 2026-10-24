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
        self.list, self.patches, self.tokens, self.deletes = {}, [], [], []

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
        if method == "DELETE":
            self.deletes.append(mal_id)
            if self.list.pop(mal_id, None) is None:
                return httpx.Response(404, json={"error": "not_found"})
            return httpx.Response(200, json=[])
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
    from app.models import PluginField
    with SessionLocal() as db:  # already connected, nothing synced yet
        db.query(PluginField).filter_by(plugin=PID).delete()
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


def choice(s):
    return next(c for c in s["choices"] if c["plugin"] == PID)


def pick(client, sid, value):
    r = client.put(f"/api/series/{sid}/choices/{PID}/mal_status", json={"value": value})
    idle()
    return r


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
    assert choice(s)["value"] == "completed"
    assert not [f for f in s["plugin_fields"] if f["plugin"] == PID]  # the dropdown shows it instead


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
    client.patch(f"/api/series/{mal.ids['b']}", json={"reading_status": "plan_to_read"})
    idle()
    assert mal.patches == []
    client.patch(f"/api/series/{mal.ids['b']}", json={"reading_status": "dropped"})
    idle()
    assert mal.patches == [(111, "dropped")]


def test_dropped_by_hand_is_sent_over_completed(client, mal):
    mal.list[111] = "completed"
    client.patch(f"/api/series/{mal.ids['b']}", json={"reading_status": "dropped"})
    idle()
    assert mal.patches == [(111, "dropped")]
    assert choice(client.get(f"/api/series/{mal.ids['b']}").json())["value"] == "dropped"
    # already dropped there: nothing sent again
    client.patch(f"/api/series/{mal.ids['b']}", json={"reading_status": "reading"})
    mal.list[111] = "dropped"
    mal.patches.clear()
    client.patch(f"/api/series/{mal.ids['b']}", json={"reading_status": "dropped"})
    idle()
    assert mal.patches == []


def test_manual_doesnt_send_dropped(client, mal):
    client.put(f"/api/plugins/{PID}/settings", json={"sync_mode": "manual"})
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


def test_sync_all_pulls_dropped(client, mal):
    read_all(client, mal.ids["h"])  # Reading here
    mal.list = {111: "dropped", 222: "dropped"}
    mal.patches.clear()
    client.post(f"/api/plugins/{PID}/actions/sync")
    idle()
    assert series(client, "Series B")["reading_status"] == "dropped"
    assert series(client, "Hana to Akuma")["reading_status"] == "dropped"
    assert choice(client.get(f"/api/series/{mal.ids['b']}").json())["value"] == "dropped"
    assert "2 updated here, 0 sent" in info(client)["status"]
    assert mal.patches == []
    client.post(f"/api/plugins/{PID}/actions/sync")  # again: nothing new
    idle()
    assert "0 updated here, 0 sent" in info(client)["status"]


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
    mal.list[111] = "dropped"
    r = client.post(f"/api/series/{mal.ids['b']}/actions/{PID}/sync").json()
    assert r["message"] == "Marked Dropped (from MyAnimeList)"
    assert series(client, "Series B")["reading_status"] == "dropped"
    assert mal.patches == []
    set_series(mal.ids["h"], mal_id=None)
    r = client.post(f"/api/series/{mal.ids['h']}/actions/{PID}/sync").json()
    assert "no MyAnimeList id" in r["message"]


def test_reset_here_resets_on_mal(client, mal):
    sid = mal.ids["b"]
    book = client.get(f"/api/series/{sid}/books").json()[0]
    client.put(f"/api/books/{book['id']}/progress", json={"page": 1})
    idle()
    assert mal.list == {111: "reading"}
    client.post(f"/api/series/{sid}/mark-unread")  # back to unread
    idle()
    assert mal.deletes == [111] and mal.list == {}
    assert choice(client.get(f"/api/series/{sid}").json())["value"] == ""
    client.patch(f"/api/series/{mal.ids['h']}", json={"reading_status": "on_hold"})
    client.patch(f"/api/series/{mal.ids['h']}", json={"clear_reading_status": True})  # never on MAL: 404 is fine
    idle()
    assert mal.deletes == [111, 222]


def test_manual_sync_sends_nothing_by_itself(client, mal):
    mode = next(f for f in info(client)["settings"] if f["key"] == "sync_mode")
    assert mode["value"] == "auto" and mode["options"] == [["auto", "Automatic"], ["manual", "Manual"]]
    client.put(f"/api/plugins/{PID}/settings", json={"sync_mode": "manual"})
    sid = mal.ids["b"]
    read_all(client, sid)
    client.patch(f"/api/series/{mal.ids['h']}", json={"reading_status": "completed"})
    client.post(f"/api/series/{sid}/mark-unread")
    idle()
    assert mal.patches == [] and mal.deletes == []
    # the dropdown still changes it
    assert pick(client, sid, "reading").status_code == 200
    assert mal.patches == [(111, "reading")]


def test_dropdown_sets_mal_and_here(client, mal):
    sid = mal.ids["b"]
    s = client.get(f"/api/series/{sid}").json()
    assert choice(s)["value"] is None  # not synced yet
    assert [v for v, _ in choice(s)["options"]] == ["reading", "completed", "on_hold", "dropped",
                                                    "plan_to_read", ""]
    for status in ("on_hold", "dropped", "plan_to_read", "completed", "reading"):
        r = pick(client, sid, status).json()
        assert r["message"].endswith("on MyAnimeList and here")
        s = client.get(f"/api/series/{sid}").json()
        assert choice(s)["value"] == status and s["reading_status"] == status
    # Completed -> Reading is allowed here: the owner picked it
    assert mal.patches == [(111, "on_hold"), (111, "dropped"), (111, "plan_to_read"),
                           (111, "completed"), (111, "reading")]


def test_dropdown_remove_keeps_it_off_the_list(client, mal):
    sid = mal.ids["b"]
    book = client.get(f"/api/series/{sid}/books").json()[0]
    client.put(f"/api/books/{book['id']}/progress", json={"page": 1})  # Reading, sent to MAL
    idle()
    assert pick(client, sid, "").json()["message"] == "Removed from your MyAnimeList list"
    assert mal.deletes == [111] and 111 not in mal.list
    s = client.get(f"/api/series/{sid}").json()
    assert choice(s)["value"] == "" and s["reading_status"] == "reading"  # unchanged here

    mal.patches.clear()
    client.post(f"/api/plugins/{PID}/actions/sync")  # doesn't send it back
    idle()
    assert mal.patches == []
    r = client.post(f"/api/series/{sid}/actions/{PID}/sync").json()
    assert "you removed it" in r["message"]
    assert pick(client, sid, "").status_code == 200  # removing again: MAL says 404, that's fine

    client.patch(f"/api/series/{sid}", json={"reading_status": "completed"})  # changed here by hand
    idle()
    assert mal.patches == [(111, "completed")]


def test_dropdown_edge_cases(client, mal):
    from app.db import SessionLocal
    from app.models import Series
    from app.plugins import Context, manager
    with SessionLocal() as db:  # what version 1.0 saved
        Context(manager.get(PID), db).set_field(db.get(Series, mal.ids["h"]), "mal_status", "Plan To Read")
        db.commit()
    assert choice(client.get(f"/api/series/{mal.ids['h']}").json())["value"] == "plan_to_read"

    set_series(mal.ids["h"], mal_id=None)
    assert not [c for c in client.get(f"/api/series/{mal.ids['h']}").json()["choices"] if c["plugin"] == PID]

    client.post(f"/api/plugins/{PID}/actions/disconnect")
    r = pick(client, mal.ids["b"], "reading")
    assert r.status_code == 500 and "not connected" in r.json()["detail"]
    assert mal.patches == []


def test_connect_flow(client, mal):
    client.put(f"/api/plugins/{PID}/settings", json={"client_id": "my-client"})
    cid = next(f for f in info(client)["settings"] if f["key"] == "client_id")
    assert cid["secret"] and cid["value"] == "" and cid["saved"]  # never sent back to the browser
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


def test_lists_its_permissions():
    from mal_profile_sync import MalProfileSync
    perms = MalProfileSync.permissions
    assert perms and all(isinstance(x, str) and x.strip() for x in perms)
    assert any("api.myanimelist.net" in x for x in perms)
