"""MyAnimeList profile sync for MangaVibe.

Drop this file into MangaVibe's plugins folder (docker-data/plugins on the NAS) and press
Settings -> Reload plugins. Needs MangaVibe with plugin API v2.

What it does (status only, no chapter counts). With Sync: Automatic (the default):
- You start reading a series            -> Reading on MyAnimeList
- You finish the last book you have:
    the series has ended (Finished)      -> Completed on MyAnimeList
    it's still coming out                -> stays Reading
- You set "Completed" by hand            -> Completed on MyAnimeList
- You set "Dropped" by hand              -> Dropped on MyAnimeList (even if it was Completed there)
- You reset it (back to unread)          -> removed from your MyAnimeList list
With Sync: Manual, nothing is sent by itself; use the dropdown or the sync buttons.
- "Sync all" (Settings) / "Sync with MyAnimeList" (series page):
    Completed on MyAnimeList             -> Completed in MangaVibe
    Reading on MyAnimeList               -> Reading in MangaVibe (if you hadn't started it)
    Dropped on MyAnimeList               -> Dropped in MangaVibe
    and anything you read in MangaVibe that MyAnimeList doesn't know yet is sent there.
- The "MyAnimeList" dropdown on the series page shows the status on MyAnimeList and changes it:
    Reading / Completed / On hold / Dropped / Plan to read -> set there, and here too
    Not on my list                       -> removed there; here stays as it is, and
                                            Sync all doesn't send it back until you change
                                            its status here yourself
Series need a MyAnimeList id, which they get when they are matched through AniList.
A series that is Completed on MyAnimeList is never set back to Reading by itself (the dropdown can).
On hold and Plan to read are never changed by themselves, only with the dropdown.
The dropdown needs MangaVibe 1.2 or newer; on older versions the rest still works.
"""
import secrets
import time
from urllib.parse import urlencode

from app.models import Series
from app.plugins import Action, FieldDef, OpenUrl, Plugin, SettingDef

try:
    from app.plugins import Choice
except ImportError:  # MangaVibe before 1.2: everything works, there's just no dropdown
    Choice = None

AUTH_URL = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
API = "https://api.myanimelist.net/v2"
PUSHED = ("reading", "completed")  # sent by themselves, plus "dropped" set by hand (the dropdown sends any)
# MyAnimeList's statuses; MangaVibe uses the same names. "" = not on your list.
STATUS_OPTIONS = [("reading", "Reading"), ("completed", "Completed"), ("on_hold", "On hold"),
                  ("dropped", "Dropped"), ("plan_to_read", "Plan to read"), ("", "Not on my list")]
STATUS_LABELS = dict(STATUS_OPTIONS)


def _sync_mode_setting():
    """The Sync dropdown in Settings, or None on MangaVibe before 1.2 (no dropdown settings: Automatic)."""
    try:
        return SettingDef("sync_mode", "Sync", options=[("auto", "Automatic"), ("manual", "Manual")],
                          default="auto",
                          help="Automatic: starting, finishing or resetting a series here changes it on "
                               "MyAnimeList. Manual: only the dropdown on the series page and the sync buttons do")
    except TypeError:
        return None


class NotConnected(Exception):
    pass


class MalProfileSync(Plugin):
    id = "mal-profile-sync"
    name = "MyAnimeList sync"
    version = "1.2.0"
    description = "Sends Reading / Completed / Dropped to your MyAnimeList list and pulls them back."
    url = "https://myanimelist.net"
    repo = "ivokolarov0/manga-vibe-ext-mal-profile-sync"  # MangaVibe 1.2+ updates it from the releases
    fields = [FieldDef("mal_status", "MyAnimeList")]  # last known status there; the dropdown shows it
    choices = [Choice("mal_status", "MyAnimeList", options=STATUS_OPTIONS)] if Choice else []
    settings_fields = [
        SettingDef("client_id", "Client ID", secret=True,
                   help="From myanimelist.net/apiconfig (App type: other)"),
        SettingDef("client_secret", "Client secret", secret=True,
                   help="Only if your MAL app type is 'web'; leave empty for 'other'"),
        *filter(None, [_sync_mode_setting()]),
    ]
    global_actions = [Action("connect", "Connect MyAnimeList"),
                      Action("sync", "Sync all"),
                      Action("disconnect", "Disconnect", confirm="Forget the MyAnimeList login?")]
    actions = [Action("sync", "Sync with MyAnimeList")]

    # ------------------------------------------------------------------ Settings page
    def status(self, ctx):
        s = ctx.settings
        callback = ctx.web_url("callback")
        if not s.get("client_id"):
            return f"Add your Client ID. Redirect URL to register on MAL: {callback}"
        if not s.get("refresh_token"):
            return f"Not connected. Redirect URL on MAL must be: {callback}"
        text = f"Connected as {s.get('user') or '?'}"
        if s.get("last_sync"):
            text += f" · {s['last_sync']}"
        return text

    def run_global_action(self, ctx, key):
        if key == "connect":
            return self._start_login(ctx)
        if key == "disconnect":
            s = ctx.settings
            for k in ("access_token", "refresh_token", "expires_at", "user", "login", "last_sync"):
                s.pop(k, None)
            ctx.save_settings(s)
            return "Disconnected from MyAnimeList"
        if key == "sync":
            try:
                self._client(ctx)  # say so now if not connected
            except NotConnected as e:
                return str(e)
            self._save(ctx, last_sync="Sync running…")
            ctx.run_in_background(self._sync_all)
            return "Sync started. It runs in the background; the result shows here when done."
        return "Unknown action"

    def run_action(self, ctx, key, series):
        if not series.mal_id:
            return "This series has no MyAnimeList id. Match it (via AniList) first."
        try:
            mal = self._client(ctx)
            entry = mal.get(f"/manga/{series.mal_id}", fields="my_list_status")
            remote = (entry.get("my_list_status") or {}).get("status")
            return self._reconcile(ctx, mal, series, remote)
        except (NotConnected, ctx.http.ProviderError) as e:
            return str(e)

    # ------------------------------------------------------------------ series page dropdown
    def get_choice(self, ctx, key, series):
        if not series.mal_id:
            return False  # nothing to point at on MyAnimeList
        value = ctx.get_field(series, "mal_status")
        if value is None:
            return None  # not synced yet
        value = str(value).strip().lower().replace(" ", "_")  # version 1.0 saved e.g. "Plan To Read"
        return value if value in STATUS_LABELS else None

    def set_choice(self, ctx, key, series, value):
        if not series.mal_id:
            return "This series has no MyAnimeList id. Match it (via AniList) first."
        try:
            mal = self._client(ctx)
            path = f"/manga/{series.mal_id}/my_list_status"
            if value:
                mal.patch(path, retries=1, status=value)
            else:
                mal.delete(path)
        except NotConnected as e:
            raise RuntimeError(str(e)) from e
        except ctx.http.ProviderError as e:
            if value or e.status != 404:  # a 404 on removing: it wasn't on the list anyway
                raise
        ctx.set_field(series, "mal_status", value)
        self._flag(ctx, "pulled_reading", series, False)  # picked by hand now
        self._flag(ctx, "removed", series, not value)
        ctx.log.info("MyAnimeList: %s -> %s (picked by hand)", series.folder_name, value or "removed")
        if not value:
            return "Removed from your MyAnimeList list"
        if ctx.reading_status(series) != value:
            ctx.set_reading_status(series, value)
        return f"{STATUS_LABELS[value]} on MyAnimeList and here"

    # ------------------------------------------------------------------ login (OAuth2 + PKCE)
    def _start_login(self, ctx):
        s = ctx.settings
        if not s.get("client_id"):
            return "Enter your MyAnimeList Client ID first."
        verifier = secrets.token_urlsafe(64)[:128]  # MAL only supports the "plain" method
        state = secrets.token_urlsafe(16)
        redirect = ctx.web_url("callback")
        s["login"] = {"state": state, "verifier": verifier, "redirect": redirect, "at": time.time()}
        ctx.save_settings(s)
        q = urlencode({"response_type": "code", "client_id": s["client_id"], "state": state,
                       "code_challenge": verifier, "code_challenge_method": "plain",
                       "redirect_uri": redirect})
        return OpenUrl(f"{AUTH_URL}?{q}", "Opening MyAnimeList…")

    def handle_web(self, ctx, path, params):
        if path != "callback":
            return None
        s = ctx.settings
        login = s.get("login") or {}
        if params.get("error"):
            return f"MyAnimeList said: {params.get('error_description') or params['error']}"
        if not login or params.get("state") != login.get("state"):
            return "This login link is old or unknown. Press Connect MyAnimeList again."
        tokens = self._token_request(ctx, {
            "grant_type": "authorization_code", "code": params.get("code", ""),
            "code_verifier": login["verifier"], "redirect_uri": login["redirect"]})
        s = ctx.settings
        s.pop("login", None)
        s.update(tokens)
        ctx.save_settings(s)
        user = self._client(ctx).get("/users/@me")
        self._save(ctx, user=user.get("name"))
        return f"Connected to MyAnimeList as {user.get('name')}. You can close this page."

    def _token_request(self, ctx, form):
        s = ctx.settings
        form = {**form, "client_id": s.get("client_id", "")}
        if s.get("client_secret"):
            form["client_secret"] = s["client_secret"]
        r = ctx.http.request("POST", TOKEN_URL, data=form, retries=1).json()
        return {"access_token": r["access_token"], "refresh_token": r["refresh_token"],
                "expires_at": time.time() + int(r.get("expires_in", 3600))}

    def _client(self, ctx):
        s = ctx.settings
        if not s.get("client_id") or not s.get("refresh_token"):
            raise NotConnected("MyAnimeList is not connected. Go to Settings -> Plugins.")
        if time.time() > s.get("expires_at", 0) - 86400:  # renew a day before it runs out
            try:
                s.update(self._token_request(ctx, {"grant_type": "refresh_token",
                                                   "refresh_token": s["refresh_token"]}))
            except ctx.http.ProviderError as e:
                raise NotConnected(f"MyAnimeList login expired, press Connect again ({e})") from e
            ctx.save_settings(s)
        return Mal(ctx, s["access_token"])

    @staticmethod
    def _save(ctx, **values):
        ctx.save_settings({**ctx.settings, **values})

    @staticmethod
    def _flag(ctx, name, series, on):
        """A list of series ids kept in the settings: "pulled_reading" (set to Reading from MAL)
        and "removed" (taken off the MAL list with the dropdown). Returns if it was there."""
        s = ctx.settings
        ids = set(s.get(name, []))
        was = series.id in ids
        (ids.add if on else ids.discard)(series.id)
        if ids != set(s.get(name, [])):
            s[name] = sorted(ids)
            ctx.save_settings(s)
        return was

    # ------------------------------------------------------------------ reading events
    @staticmethod
    def _automatic(ctx):
        return (ctx.settings.get("sync_mode") or "auto") == "auto"

    def on_series_status_changed(self, ctx, series, old, new):
        if not series.mal_id or not self._automatic(ctx) or new not in (*PUSHED, "dropped", "unread"):
            return
        try:
            mal = self._client(ctx)
        except NotConnected:
            return
        if new == "unread":  # reset here (marked unread / status cleared): reset there too
            self._remove(ctx, mal, series)
            return
        self._flag(ctx, "removed", series, False)  # you changed it here, so MAL may have it again
        if new == "dropped":  # the owner's choice: sent even over Completed
            self._send_dropped(ctx, mal, series)
            return
        self._push(ctx, mal, series, self._wanted(series, new), remote=None)

    def _send_dropped(self, ctx, mal, series):
        entry = mal.get(f"/manga/{series.mal_id}", fields="my_list_status")
        if (entry.get("my_list_status") or {}).get("status") != "dropped":
            mal.patch(f"/manga/{series.mal_id}/my_list_status", status="dropped")
            ctx.log.info("MyAnimeList: %s -> dropped (set here by hand)", series.folder_name)
        ctx.set_field(series, "mal_status", "dropped")
        self._flag(ctx, "pulled_reading", series, False)

    def _remove(self, ctx, mal, series):
        try:
            mal.delete(f"/manga/{series.mal_id}/my_list_status")
        except ctx.http.ProviderError as e:
            if e.status != 404:  # 404: it wasn't on the list anyway
                raise
        ctx.set_field(series, "mal_status", "")
        self._flag(ctx, "pulled_reading", series, False)
        ctx.log.info("MyAnimeList: %s removed (reset here)", series.folder_name)

    def on_book_completed(self, ctx, book, series):
        # A series this plugin set to "Reading" (from MAL) keeps that by hand, so finishing it
        # doesn't change its status by itself: hand it back to progress, then push the result.
        if self._flag(ctx, "pulled_reading", series, False):
            ctx.set_reading_status(series, None)
            new = ctx.reading_status(series)
            if new != "reading":
                self.on_series_status_changed(ctx, series, "reading", new)

    @staticmethod
    def _wanted(series, app_status):
        """What MAL should say for the app's status: Completed only when the story has ended,
        or when the owner picked Completed by hand."""
        if app_status == "completed" and (series.publication_status == "FINISHED"
                                          or series.reading_status == "completed"):
            return "completed"
        return "reading"

    def _push(self, ctx, mal, series, wanted, remote):
        """Send `wanted` unless MAL already has it, or has Completed (never downgrade)."""
        if remote is None:
            entry = mal.get(f"/manga/{series.mal_id}", fields="my_list_status")
            remote = (entry.get("my_list_status") or {}).get("status")
        if remote == wanted or remote == "completed":
            ctx.set_field(series, "mal_status", remote)
            return False
        mal.patch(f"/manga/{series.mal_id}/my_list_status", status=wanted)
        ctx.set_field(series, "mal_status", wanted)
        ctx.log.info("MyAnimeList: %s -> %s", series.folder_name, wanted)
        return True

    # ------------------------------------------------------------------ sync
    def _reconcile(self, ctx, mal, series, remote):
        """Bring one series in line. Returns a short message."""
        here = ctx.reading_status(series)
        if remote is None and series.id in ctx.settings.get("removed", []):
            ctx.set_field(series, "mal_status", "")
            return "Not on your MyAnimeList list (you removed it)"
        self._flag(ctx, "removed", series, False)  # back on the list (added on the website)
        if remote == "completed":
            ctx.set_field(series, "mal_status", "completed")
            if here != "completed":
                ctx.set_reading_status(series, "completed")
                self._flag(ctx, "pulled_reading", series, False)
                return "Marked Completed (from MyAnimeList)"
            return "Already Completed on both"
        if remote == "dropped":
            ctx.set_field(series, "mal_status", "dropped")
            self._flag(ctx, "pulled_reading", series, False)
            if here != "dropped":
                ctx.set_reading_status(series, "dropped")
                return "Marked Dropped (from MyAnimeList)"
            return "Already Dropped on both"
        if remote == "reading" and here == "unread":
            ctx.set_reading_status(series, "reading")
            self._flag(ctx, "pulled_reading", series, True)
            ctx.set_field(series, "mal_status", "reading")
            return "Marked Reading (from MyAnimeList)"
        if here in PUSHED:
            wanted = self._wanted(series, here)
            if self._push(ctx, mal, series, wanted, remote):
                return f"Sent {wanted.title()} to MyAnimeList"
        ctx.set_field(series, "mal_status", remote or "")  # "" = not on the list
        return "Nothing to change"

    def _sync_all(self, ctx):
        try:
            mal = self._client(ctx)
            remote = {}
            for item in mal.pages("/users/@me/mangalist", fields="list_status", limit=1000, nsfw="true"):
                remote[item["node"]["id"]] = (item.get("list_status") or {}).get("status")
            pulled = pushed = 0
            for series in ctx.db.query(Series).filter(Series.mal_id.is_not(None),
                                                      Series.missing_since.is_(None)):
                msg = self._reconcile(ctx, mal, series, remote.get(series.mal_id))
                pulled += "from MyAnimeList" in msg
                pushed += msg.startswith("Sent")
                ctx.db.commit()  # keep what's done if MAL fails halfway
            unmatched = ctx.db.query(Series).filter(Series.mal_id.is_(None),
                                                    Series.missing_since.is_(None)).count()
            text = f"Last sync {time.strftime('%Y-%m-%d %H:%M')}: {pulled} updated here, {pushed} sent"
            if unmatched:
                text += f", {unmatched} series without a MyAnimeList id skipped"
        except Exception as e:
            ctx.db.rollback()
            text = f"Last sync failed: {e}"
        self._save(ctx, last_sync=text)


class Mal:
    """Tiny MyAnimeList API v2 client."""

    def __init__(self, ctx, token):
        self.http, self.headers = ctx.http, {"Authorization": f"Bearer {token}"}

    def get(self, path, retries=4, **params):
        return self.http.request("GET", API + path, retries=retries, params=params,
                                 headers=self.headers).json()

    def patch(self, path, retries=4, **form):
        return self.http.request("PATCH", API + path, retries=retries, data=form,
                                 headers=self.headers).json()

    def delete(self, path, retries=1):
        self.http.request("DELETE", API + path, retries=retries, headers=self.headers)

    def pages(self, path, **params):
        page = self.get(path, **params)
        while True:
            yield from page.get("data", [])
            nxt = (page.get("paging") or {}).get("next")
            if not nxt:
                return
            page = self.http.request("GET", nxt, headers=self.headers).json()
